"""Question-level answers bound to observed excerpts, never source counts.

An excerpt match proves provenance, not semantic support. Support judgments keep
model/human origin and are explicitly subject to review.
"""
import hashlib
import json
import re
from copy import deepcopy
from . import store
from .fact_state import assert_claims
from .evidence_policy import eligibility, source_fingerprint

LABELS={'pending':'待处理','partial':'部分回答','supported':'有证据回答 · 待复核','conflict':'证据冲突','unverifiable':'无法验证'}
PRIORITIES={'high','medium','low'}
SEMANTIC_REVIEW='研究问题或完成条件已修改，原答案与证据需重新核对'


def text(value,label,limit=2000,required=True):
    if not isinstance(value,str) or len(value)>limit or required and not value.strip(): raise ValueError(label+'需为有效文本')
    return value.strip()


def question_key(question):
    return re.sub(r'[\W_]+','',question.casefold())


def create(question,criteria='给出对应问题的回答，并绑定原文证据、说明反证及缺口。',priority='medium'):
    question=text(question,'研究问题',1000);criteria=text(criteria,'完成条件',1500)
    if not isinstance(priority,str) or priority not in PRIORITIES: raise ValueError('问题优先级应为high、medium或low')
    return {'id':'q-'+hashlib.sha256(question.encode()).hexdigest()[:12],'question':question,'completion_criteria':criteria,
        'status':'pending','answer':'','bindings':[],'counter_evidence':'','gaps':[],'next_action':'','assessment_origin':'none',
        'priority':priority,'archived':False,'needs_review':False,'revision':1}


def inventory(run):
    values=deepcopy(run.get('research_questions') or [create(run['request'].get('question','当前研究还有哪些证据缺口？'))])
    for item in values:
        for key,value in {'priority':'medium','archived':False,'needs_review':False,'revision':1}.items(): item.setdefault(key,value)
    return values


def plan(run_id,items):
    if not isinstance(items,list) or not 1<=len(items)<=20: raise ValueError('每次提出1—20项研究问题')
    prepared=[create(i['question'],i.get('completion_criteria','给出问题答案并绑定支持与反方原文，保留缺口。'),i.get('priority','medium')) if isinstance(i,dict) and 'question' in i else create(i) for i in items]
    def change(run):
        current=inventory(run);known={question_key(i['question']) for i in current}
        for item in prepared:
            key=question_key(item['question'])
            if key not in known:
                # Edited questions retain their original ID, so reuse neither it nor its history.
                used={q['id'] for q in current}
                if item['id'] in used: item['id']='q-'+hashlib.sha256((item['question']+str(len(current))).encode()).hexdigest()[:12]
                current.append(item);known.add(key)
        if len(current)>50: raise ValueError('研究最多保存50项问题，请收敛任务范围')
        run['research_questions']=current
    saved=store.update_run(run_id,change)
    return snapshot(saved)


def record_change(old,new):
    ignored={'history','updated_at','label','revision'}
    comparable=lambda q:{k:v for k,v in q.items() if k not in ignored}
    if comparable(old)==comparable(new): return
    history=old.get('history',[])+[{k:v for k,v in old.items() if k!='history'}]
    revision=old.get('revision',1)+1
    old.update(new,history=history,updated_at=store.now(),revision=revision)


def edit(run_id,question_id,changes,origin='human'):
    if not isinstance(changes,dict) or set(changes)-{'question','completion_criteria','priority','archived'}:
        raise ValueError('只支持编辑问题、完成条件、优先级与归档状态')
    def change(run):
        current=inventory(run);old=next((q for q in current if q['id']==question_id),None)
        if not old: raise ValueError('问题ID不存在')
        new=deepcopy(old)
        for field,limit,label in (('question',1000,'研究问题'),('completion_criteria',1500,'完成条件')):
            if field in changes: new[field]=text(changes[field],label,limit)
        if any(q['id']!=question_id and question_key(q['question'])==question_key(new['question']) for q in current):
            raise ValueError('已存在相同问题，请编辑原问题或恢复已归档的问题')
        if 'priority' in changes:
            if not isinstance(changes['priority'],str) or changes['priority'] not in PRIORITIES: raise ValueError('问题优先级应为high、medium或low')
            new['priority']=changes['priority']
        if 'archived' in changes:
            if not isinstance(changes['archived'],bool): raise ValueError('归档状态必须为布尔值')
            new['archived']=changes['archived']
        if any(question_key(new[f])!=question_key(old[f]) for f in ('question','completion_criteria')):
            new.update(status='partial' if old.get('answer') or old.get('bindings') else 'pending',needs_review=True)
            new['gaps']=list(dict.fromkeys(new.get('gaps',[])+[SEMANTIC_REVIEW]))
        if new!=old: new['edited_by']=origin
        record_change(old,new);run['research_questions']=current
    return snapshot(store.update_run(run_id,change))


def catalog(run):
    from .deep_research import reference_catalog
    refs=reference_catalog(run)
    # Original evidence is also addressable at its exact document/row locator.
    for source in run.get('evidence',[]):
        day=str(source.get('announced_date') or '').replace('-','')[:8]
        if day and day>run['request']['as_of'].replace('-',''): continue
        refs[source['id']]={**source,'text':source.get('quote',''),'url':source.get('source_url'),
            'published_at':source.get('announced_date'),'read_status':source.get('read_status') or source.get('extraction') or 'original_record'}
    for f in run.get('facts',[]):
        if f['id'] not in refs: continue
        refs[f['id']]['text']='\n'.join(s.get('quote','') for s in run.get('evidence',[]) if s['id'] in f.get('evidence_ids',[]))
        refs[f['id']]['locator']=f.get('label',f['id'])+' '+str(f.get('year'))+'年'
    return {k:{**s,'eligibility':eligibility(s,run)} for k,s in refs.items()
            if eligibility(s,run)['historical_eligibility']!='after_cutoff'}


def normal(value): return re.sub(r'\s+','',value)


def assess(run,item,origin='model',reassess=False):
    current=next((q for q in inventory(run) if q['id']==item.get('question_id',item.get('id'))),None)
    if not current: raise ValueError('问题ID不存在，请先建立研究问题')
    if current.get('archived') and not reassess: raise ValueError('请先恢复已归档问题，再更新答案')
    status=item.get('status','partial')
    if status not in LABELS: raise ValueError('问题状态无效：'+str(current['id'])+'，仅允许 pending、partial、supported、conflict、unverifiable；不充分时用partial，不要使用章节的inference或gap')
    answer=text(item.get('answer',''),'答案',8000,False)
    counter=text(item.get('counter_evidence',''),'反方说明',3000,False)
    gaps=item.get('gaps',[])
    if not isinstance(gaps,list) or len(gaps)>20: raise ValueError('缺口需为最多20项文本列表')
    gaps=[text(g,'证据缺口',1000) for g in gaps]
    bindings=item.get('bindings',[])
    if not isinstance(bindings,list) or len(bindings)>20: raise ValueError('证据绑定最多20项')
    refs=catalog(run);checked=[];usable=0;has_conflict=False;source_changed=False
    for binding in bindings:
        if not isinstance(binding,dict): raise ValueError('证据绑定需为对象')
        sid=binding.get('source_id');source=refs.get(sid)
        if not source: raise ValueError('引用来源不存在或晚于截止日')
        quote=text(binding.get('quote'),'原文摘录',2500)
        if len(normal(quote))<4 or normal(quote) not in normal(source.get('text','')):
            raise ValueError('引用摘录未在实际取得的原文中匹配：问题 '+current['id']+'，来源 '+str(sid)+'。请从该来源复制短的连续原文，不改写、不拼接；无法找到时删除该绑定并保留缺口')
        relation=binding.get('relation','support')
        if relation not in ('support','counter'): raise ValueError('关系必须为support或counter')
        reason=text(binding.get('reason'),'证据与问题的关系',1500)
        digest=hashlib.sha256(source.get('text','').encode()).hexdigest()
        if binding.get('source_hash') and binding['source_hash']!=digest:
            source_changed=True;gaps.append('所用来源内容已变化，需重新确认摘录语境')
        state=source.get('read_status');policy=eligibility(source,run)
        qualifying=policy['supports_answer']
        if not qualifying: gaps.extend(policy['reasons'])
        if policy['basis']=='conflict': has_conflict=True
        if qualifying and relation=='support': usable+=1
        checked.append({'source_id':sid,'quote':quote,'relation':relation,'reason':reason,'source_hash':digest,
            'title':source.get('title') or source.get('locator') or sid,'url':source.get('url'),
            'page':source.get('page'),'locator':source.get('locator'),'published_at':source.get('published_at'),
            'read_status':state,'qualifying':qualifying,'eligibility':policy})
    if status=='supported' and not counter: gaps.append('尚未记录反方或替代解释')
    if status=='supported' and (not answer or not usable or gaps):
        status='partial'
        if not usable: gaps.append('尚无具备日期与原文的支持证据')
        if not answer: gaps.append('尚未给出问题答案')
    if has_conflict: status='conflict';gaps.append('关联财务事实存在来源冲突')
    assert_claims(answer,run,[b['source_id'] for b in checked])
    return {**current,'status':status,'answer':answer,'bindings':checked,'counter_evidence':counter,
        'gaps':list(dict.fromkeys(gaps)),'next_action':text(item.get('next_action',''),'下一步',1500,False),'assessment_origin':origin,
        'needs_review':bool(current.get('needs_review') and reassess) or SEMANTIC_REVIEW in gaps or source_changed,
        'semantic_validation':'excerpt_match_only; support_judgment_requires_review'}


def validate_updates(run,updates,origin='model'):
    if not isinstance(updates,list) or len(updates)>50: raise ValueError('问题答案需为最多50项列表')
    if any(not isinstance(i,dict) for i in updates): raise ValueError('问题答案需为对象')
    ids=[i.get('question_id',i.get('id')) for i in updates]
    if len(ids)!=len(set(ids)): raise ValueError('同一批次问题不能重复更新')
    return [assess(run,i,origin) for i in updates]


def apply(run,updates):
    current=inventory(run);lookup={q['id']:q for q in current}
    for answer in updates:
        old=lookup[answer['id']]
        record_change(old,answer)
    run['research_questions']=current


def update(run_id,updates,origin='model'):
    def change(run):apply(run,validate_updates(run,updates,origin))
    return snapshot(store.update_run(run_id,change))


def snapshot(run):
    items=[]
    for q in inventory(run):
        try:
            item=assess(run,q,q.get('assessment_origin','none'),reassess=True) if q.get('bindings') else q
        except ValueError as e:
            item={**q,'status':'partial','needs_review':True,'gaps':list(dict.fromkeys(q.get('gaps',[])+['来源需重新核对：'+str(e)]))}
        if item.get('status')=='supported' and not item.get('bindings'):item={**item,'status':'partial','gaps':['缺少原文绑定']}
        items.append({**item,'label':LABELS[item['status']]})
    active=[q for q in items if not q.get('archived')]
    counts={s:sum(q['status']==s for q in active) for s in LABELS}
    return {'items':items,'total':len(active),'archived_count':len(items)-len(active),'counts':counts,'complete':bool(active) and counts['supported']==len(active),
        'note':'问题状态由答案及逐条原文绑定约束；摘录匹配不代表因果/投资判断已经独立验证。'}


def bounded_catalog(run,budget=65000):
    result=[]
    for source in catalog(run).values():
        if budget<=0: break
        value=dict(source);value['text']=source.get('text','')[:min(8000,budget)]
        budget-=len(value['text']);result.append(value)
    return result


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def _question_snapshot(run,memo):
    refs=catalog(run);view=snapshot(run);items={}
    for q in view['items']:
        content={k:q.get(k) for k in ('question','completion_criteria','status','answer','counter_evidence',
                                     'gaps','next_action','archived','needs_review')}
        content['bindings']=[{k:b.get(k) for k in ('source_id','quote','relation','reason')} for b in q.get('bindings',[])]
        sources={b['source_id']:source_fingerprint(refs[b['source_id']],run) if b['source_id'] in refs else None
                 for b in q.get('bindings',[])}
        items[q['id']]={'fingerprint':_fingerprint(content),'revision':q.get('revision',1),
                        'source_fingerprints':sources,'archived':q.get('archived',False)}
    cited={sid for section in memo.get('theses',[])+memo.get('sections',[]) for sid in section.get('evidence_ids',[])}
    return {'schema_version':1,'as_of':run.get('request',{}).get('as_of'),'questions':items,
            'report_sources':{sid:source_fingerprint(refs[sid],run) if sid in refs else None for sid in sorted(cited)}}


def freeze_report(run,memo):
    """Bind this report version to question/evidence state without changing research."""
    result=deepcopy(memo)
    result['question_snapshot']={**_question_snapshot(run,result),'captured_at':store.now()}
    result['question_coverage']=snapshot(run)
    return result


def assert_report_input(run,memo):
    """A draft validated against a frozen model input may not overwrite later edits."""
    expected=memo.get('_question_input_snapshot')
    if not expected: return
    current=_question_snapshot(run,memo)
    def meaningful(value):
        return {'as_of':value.get('as_of'),'report_sources':value.get('report_sources',{}),
                'questions':{qid:{k:v for k,v in q.items() if k!='revision'} for qid,q in value.get('questions',{}).items()}}
    if meaningful(expected)!=meaningful(current):
        raise ValueError('报告生成期间问题或引用来源已变化，旧报告与当前答案均已保留；请依据最新证据重新更新报告')


def report_sync(run):
    memo=run.get('memo') or {};saved=memo.get('question_snapshot')
    if not saved or saved.get('schema_version')!=1:
        return {'status':'untracked','needs_update':False,'changed_question_ids':[],
                'source_changed_question_ids':[],'changed_source_ids':[],
                'message':'该历史报告尚未记录问题与证据版本；下次明确更新报告时建立关联。'}
    current=_question_snapshot(run,memo);before=saved.get('questions',{});after=current['questions']
    ids=sorted(set(before)|set(after))
    changed=[qid for qid in ids if before.get(qid,{}).get('fingerprint')!=after.get(qid,{}).get('fingerprint')]
    source_changed=[qid for qid in ids if before.get(qid,{}).get('source_fingerprints',{})!=after.get(qid,{}).get('source_fingerprints',{})]
    previous_sources=saved.get('report_sources',{});current_sources=current['report_sources']
    changed_sources=sorted(sid for sid in set(previous_sources)|set(current_sources) if previous_sources.get(sid)!=current_sources.get(sid))
    cutoff_changed=saved.get('as_of')!=current['as_of']
    stale=bool(changed or source_changed or changed_sources or cutoff_changed)
    return {'status':'stale' if stale else 'current','needs_update':stale,
            'changed_question_ids':changed,'source_changed_question_ids':source_changed,'changed_source_ids':changed_sources,
            'message':'问题、证据或研究截止日已变化，报告待同步；现有报告和估值情景尚未改写。' if stale else '报告已关联当前问题与证据版本。'}
