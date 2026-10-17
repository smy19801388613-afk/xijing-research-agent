import copy
import json
import pytest
from app import store,deep_research as deep,agent,providers,connections
from app.exporting import markdown,printable
from recommendation_fixture import watch_recommendation

@pytest.fixture
def run(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    r=store.create_run({'ticker':'300271.SZ','year':2025,'as_of':'2026-10-02','mode':'live','model_profile':'test','question':'业务、回款与估值'})
    r.update(company='华宇软件',state='completed_with_gaps',facts=[{'id':'revenue-2025','label':'营业收入','metric':'revenue','year':2025,'value':'1619404301.59','status':'api_only'}],memo={'title':'原报告','summary':'原结论','theses':[]})
    store.save_run(r)
    return r

def candidate():
    return {'title':'产品商业化与回款的分歧','summary':'业务增长要与回款共同验证','stance':'继续跟踪，现金约束未解除',
        'theses':[{'title':name,'body':'来源能证明产品存在，尚不能证明独立盈利','evidence_ids':['web-product'],'counter_evidence':'客户验收不等于收款','invalidate_if':'取得产品利润与实际回款明细','status':'inference'} for name in ('产品商业化','现金约束')],
        'sections':[{'key':key,'body':'具体分析，原始证据与推断分开。','evidence_ids':['web-product'],'status':'inference'} for key in deep.SECTION_TITLES],
        'questions':['按产品核对订单、验收与回款'],'limitations':['没有独立披露产品利润'],
        'investment_recommendation':watch_recommendation()}


def tracked_candidate():
    """New deep generations must supply a plan, including an honest missing baseline."""
    value=candidate()
    value['tracking_metrics']=[{'id':'product-cash','name':'产品商业化回款','definition':'具体产品订单对应的验收与实际收款',
        'thesis':'产品商业化','baseline':{'status':'missing','value':'未取得分产品回款明细','period':'','source_id':None,'quote':''},
        'frequency':'定期报告披露后','next_check':'下一次取得产品收入和回款明细时',
        'support_condition':'产品客户支付证据与收入确认一致','invalidate_condition':'客户验收后持续逾期',
        'threshold_basis':'qualitative','next_action':'查阅附注并更新研究判断','status':'needs_baseline'}]
    return value

def source_run(run):
    run['research_sources']=[{'id':'web-product','kind':'web','title':'产品发布','url':'https://example.com/product','text':'产品名称及客户使用场景','published_at':'2026-09-01','read_status':'body_read'}]
    store.save_run(run)
    return run

def test_memo_accepts_actual_business_evidence_without_financial_fact(run):
    r=source_run(run)
    memo=deep.validate_memo(candidate(),r)
    assert memo['theses'][0]['fact_ids']==[]
    assert memo['sources'][0]['url']=='https://example.com/product'
    result=deep.save_memo(r['id'],memo,'生成完整报告')
    updated=store.get_run(r['id'])
    assert result['revision']==2 and updated['memo_revisions'][0]['memo']==run['memo']
    for output in (markdown(updated),printable(updated)):
        assert '公司靠什么赚钱' in output and 'https://example.com/product' in output

@pytest.mark.parametrize('field,value',[('theses',['x','y']),('sections',['bad']),('summary',None)])
def test_malformed_model_shape_is_repairable_value_error(run,field,value):
    r=source_run(run);draft=candidate();draft[field]=value
    with pytest.raises(ValueError): deep.validate_memo(draft,r)

def test_unobserved_and_future_citations_rejected(run):
    r=source_run(run);draft=candidate()
    draft['theses'][0]['evidence_ids']=['made-up']
    with pytest.raises(ValueError): deep.validate_memo(draft,r)
    r['research_sources'][0]['published_at']='2026-10-03'
    with pytest.raises(ValueError): deep.validate_memo(candidate(),r)
    r['research_sources'][0]['published_at']=None
    assert deep.validate_memo(candidate(),r)['sources'][0]['published_at'] is None

@pytest.mark.parametrize('read_status,text',[
    ('search_snippet','搜索摘要'),('pdf_link_only','PDF 链接'),('failed',''),('body_read','')
])
def test_unread_sources_cannot_support_report_claims(run,read_status,text):
    r=source_run(run)
    r['research_sources'][0].update(read_status=read_status,text=text)
    with pytest.raises(ValueError,match='搜索摘要|未读正文'):
        deep.validate_memo(candidate(),r)


def test_ocr_can_support_inference_but_not_verified_claim(run):
    r=source_run(run)
    r['research_sources'][0].update(kind='pdf',read_status='windows_ocr')
    assert deep.validate_memo(candidate(),r)['theses'][0]['status']=='inference'
    draft=candidate()
    draft['theses'][0]['status']='supported'
    with pytest.raises(ValueError,match='扫描识别'):
        deep.validate_memo(draft,r)


def test_html_in_model_is_escaped_in_export(run):
    r=source_run(run);draft=candidate();draft['sections'][0]['body']='<script>alert(1)</script>'
    r['memo']=deep.validate_memo(draft,r)
    assert '<script>' not in printable(r) and '&lt;script&gt;' in printable(r)

def test_downloaded_report_links_resolve_local_pdf(run):
    r=source_run(run)
    r['research_sources'][0]['url']='/api/documents/annual/file#page=88'
    r['memo']=deep.validate_memo(candidate(),r)
    for output in (markdown(r),printable(r)):
        assert 'http://127.0.0.1:8920/api/documents/annual/file#page=88' in output

def test_observed_body_not_downgraded_to_search_snippet(run):
    chat={'run_id':run['id']};url='https://example.com/product'
    deep.register_sources(chat,{'url':url,'title':'产品','text':'实际正文','published_at':'2026-08-01'})
    deep.register_sources(chat,{'results':[{'url':url,'title':'片段','snippet':'摘要'}]})
    saved=store.get_run(run['id'])['research_sources'][0]
    assert saved['text']=='实际正文' and saved['read_status']=='body_read'

def test_source_records_cannot_be_injected_by_model(run):
    r=source_run(run);draft=candidate();draft['sources']=[{'url':'javascript:alert(1)'}]
    memo=deep.validate_memo(draft,r)
    assert memo['sources']==r['research_sources']

def test_repeated_memo_validation_does_not_duplicate_limitations(run):
    r=source_run(run);first=deep.validate_memo(candidate(),r)
    assert deep.validate_memo(first,r)['limitations']==first['limitations']

def test_model_cannot_contradict_actual_report_capabilities(run):
    r=source_run(run);draft=candidate();draft['limitations']=['无法实际保存文件和导出报告，只以JSON返回']
    with pytest.raises(ValueError,match='程序将实际保存'): deep.validate_memo(draft,r)

def test_report_generation_is_an_execution_postcondition():
    assert 'report' in agent.requested_artifacts('按照这个办法，生成研究结果吧。我应该在哪里看结果，导出报告？')
    assert 'report' in agent.requested_artifacts('请生成并保存投资研究备忘录')
    assert 'report' not in agent.requested_artifacts('不要生成报告，先讨论方法')

def test_webpage_read_requires_an_observed_search_result(run):
    with pytest.raises(ValueError,match='先联网搜索'):
        agent.tool({'run_id':run['id']},'read_webpage',{'url':'https://example.com/invented'})

def test_deepseek_research_enables_reasoning_without_exposing_it(monkeypatch):
    import httpx
    monkeypatch.setattr(connections,'resolve_model',lambda _: {'provider':'deepseek','protocol':'openai','key':'test','base':'https://example.com','id':'test','model':'demo','json_mode':True})
    seen={}
    def send(url,**kwargs):
        seen.update(kwargs)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}','reasoning_content':'internal'},'finish_reason':'stop'}]},request=httpx.Request('POST',url))
    monkeypatch.setattr(providers.httpx,'post',send)
    value,meta=providers.model_json('test',{},1000,reasoning=True)
    assert seen['json']['thinking']['type']=='enabled' and seen['json']['max_tokens']>1000
    assert 'internal' not in json.dumps((value,meta)) and meta['reasoning_enabled']

def test_build_repairs_then_saves_once_and_retains_old_memo(run,monkeypatch):
    source_run(run)
    monkeypatch.setattr(deep.documents,'attached_documents',lambda _: [{'ticker':'300271.SZ','year':2025,'title':'2025年年度报告','page_count':194}])
    monkeypatch.setattr(deep.documents,'ensure_index',lambda _: [])
    monkeypatch.setattr(deep.documents,'search_attached',lambda *args: {'snippets':[]})
    monkeypatch.setattr(deep.web_research,'search',lambda q,**kw: {'queries':q,'results':[],'limitations':['公开索引未找到新内容']})
    answers=iter([{'queries':['华宇软件 产品 客户'],'pdf_queries':['产品'],'focus':'商业化'},
        {'queries':[],'gaps':['收入贡献未知']},tracked_candidate(),{'issues':['补充回款依据'],'requires_revision':True},tracked_candidate()])
    monkeypatch.setattr(deep,'model_json',lambda *a,**kw: (next(answers),{'model':'stub'}))
    result=deep.build(run['id'],'保存深度报告')
    updated=store.get_run(run['id'])
    assert result['artifact']=='report' and len(updated['memo_revisions'])==1
    assert updated['memo']['review']['revision_performed'] and len(updated['deep_research_log']['calls'])==5


def test_tracking_composite_baseline_repair_receives_single_quantity_guidance(run, monkeypatch):
    current=source_run(run)
    current['research_sources'][0]['text']='合成披露：现金分红100元。另一段：回购50元。'
    store.save_run(current)
    monkeypatch.setattr(deep.documents,'attached_documents',lambda _: [{'ticker':'300271.SZ','year':2025,'title':'2025年年度报告','page_count':194}])
    monkeypatch.setattr(deep.documents,'ensure_index',lambda _: [])
    monkeypatch.setattr(deep.documents,'search_attached',lambda *args: {'snippets':[]})
    monkeypatch.setattr(deep.web_research,'search',lambda q,**kw: {'queries':q,'results':[]})
    invalid=tracked_candidate()
    invalid['tracking_metrics'][0]['baseline']={'status':'observed','value':'100元；50元','period':'2025年度',
        'source_id':'web-product','quote':'现金分红100元'}
    repaired=copy.deepcopy(invalid)
    repaired['tracking_metrics'][0]['baseline']['value']='100元'
    answers=iter([{'queries':['华宇软件 产品 客户'],'pdf_queries':['产品']}, {'queries':[]},
        invalid, {'issues':[],'requires_revision':False}, repaired])
    payloads=[]
    def model(system,payload,*a,**kw):
        payloads.append(payload)
        return next(answers),{'model':'stub'}
    monkeypatch.setattr(deep,'model_json',model)
    result=deep.build(run['id'],'完成研究')
    feedback=payloads[-1]['validation_error']
    assert '不匹配值 50' in feedback and 'baseline只保留一个原始量' in feedback
    assert '同一个source_id的连续quote' in feedback and '资本开支与经营现金流' in feedback
    saved=store.get_run(run['id'])
    assert result['revision']==2 and len(saved['memo_revisions'])==1
    assert saved['memo']['tracking_metrics'][0]['baseline']['value']=='100元'
    assert len(payloads)==5


def test_build_repair_cannot_adopt_concurrently_published_report_as_base(run,monkeypatch):
    source_run(run)
    monkeypatch.setattr(deep.documents,'attached_documents',lambda _: [{'ticker':'300271.SZ','year':2025,'title':'2025年年度报告','page_count':194}])
    monkeypatch.setattr(deep.documents,'ensure_index',lambda _: [])
    monkeypatch.setattr(deep.documents,'search_attached',lambda *args: {'snippets':[]})
    monkeypatch.setattr(deep.web_research,'search',lambda q,**kw: {'queries':q,'results':[]})
    answers=iter([{'queries':['华宇软件 产品 客户'],'pdf_queries':['产品']},
                  {'queries':[]},tracked_candidate(),{'issues':['需要补充反证'],'requires_revision':True},tracked_candidate()])
    calls=[]
    def model(*args,**kwargs):
        calls.append(1)
        if len(calls)==4:
            other=candidate();other['summary']='另一对话已保存的新判断'
            deep.save_memo(run['id'],deep.validate_memo(other,store.get_run(run['id'])),'其他对话')
        return next(answers),{'model':'stub'}
    monkeypatch.setattr(deep,'model_json',model)
    with pytest.raises(ValueError,match='其他修订'):
        deep.build(run['id'],'保存完整深度报告')
    saved=store.get_run(run['id'])
    assert saved['memo']['summary']=='另一对话已保存的新判断'
    assert saved['report_revision']==2 and len(saved['memo_revisions'])==1

def test_search_and_redirect_body_share_source_id_and_preserve_date(run):
    chat={'run_id':run['id']}
    deep.register_sources(chat,{'results':[{'url':'http://example.com/article','title':'披露报道','snippet':'片段','published_at':'2026-08-01'}]})
    deep.register_sources(chat,{'url':'https://example.com/article','title':'披露报道','text':'阅读正文','published_at':None})
    sources=store.get_run(run['id'])['research_sources']
    assert len(sources)==1 and sources[0]['published_at']=='2026-08-01' and sources[0]['read_status']=='body_read'

def test_public_pdf_pages_remain_distinct_and_never_replace_fiscal_attachment(run):
    before=copy.deepcopy(run)
    rows=[{'url':f'https://static.cninfo.com.cn/finalpage/2026-08-26/report.PDF#page={n}',
        'title':f'半年报第{n}页','text':f'原文{n}','page':n,'extraction':'public_pdf_text','published_at':'2026-08-26',
        'date_basis':'官方公告 URL 的 finalpage 日期'} for n in (7,8)]
    deep.register_sources({'run_id':run['id']},{'results':rows})
    saved=store.get_run(run['id']);sources=saved['research_sources']
    assert len(sources)==2 and len({s['id'] for s in sources})==2
    assert all(s['kind']=='pdf' and s['date_basis'] for s in sources)
    assert saved.get('document_id')==before.get('document_id') and saved['facts']==before['facts']

def test_equivalent_section_dictionary_normalized_without_inventing_analysis(run):
    r=source_run(run);draft=candidate()
    draft['sections']={s['key']:{k:v for k,v in s.items() if k!='key'} for s in draft['sections']}
    clean=deep.validate_memo(draft,r)
    assert len(clean['sections'])==7 and clean['sections'][0]['body']==draft['sections']['business']['body']

def test_context_keeps_new_official_filings_and_comparative_statements():
    refs={f'old-{n}':{'id':f'old-{n}','kind':'pdf','url':f'https://example.com/annual.pdf#page={n}',
        'text':'普通年报内容'*600,'retrieved_at':'2026-09-01'} for n in range(90)}
    refs['income']={'id':'income','kind':'pdf','url':'https://example.com/annual.pdf#page=91',
        'text':'合并利润表 信用减值损失 资产减值损失 34,215,806.93','retrieved_at':'2026-10-02'}
    refs['new-half']={'id':'new-half','kind':'pdf','url':'https://example.com/half.pdf#page=7',
        'text':'主要会计数据和财务指标 637,899,648.61 592,472,821.09','retrieved_at':'2026-10-02'}
    refs['correction']={'id':'correction','kind':'pdf','url':'https://example.com/correction.pdf#page=2',
        'text':'更正前 653,057,269.10 更正后 592,472,821.09','retrieved_at':'2026-10-02'}
    selected=deep.bounded_sources(refs)
    assert {'income','new-half','correction'} <= {s['id'] for s in selected}
    assert sum(len(s['text']) for s in selected)<=65000

def test_material_inventory_preserves_read_pages_without_claiming_full_document():
    refs={str(p):{'kind':'pdf','url':f'https://example.com/annual.pdf#page={p}',
        'title':'年报','page':p} for p in (88,20,172)}
    assert deep.material_inventory(refs)==[{'url':'https://example.com/annual.pdf','title':'年报','read_pages':[20,88,172]}]

def test_refine_reuses_read_sources_and_preserves_valuation_and_history(run,monkeypatch):
    r=source_run(run);r['valuation_history']=[{'id':'user-scenario','method':'ps','method_name':'PS','assumptions':{'growth':.01,'multiple':5},'output':{}}]
    store.save_run(r);chat=store.new_conversation(r['id']);seen={}
    def call(system,payload,*args,**kwargs):
        seen.update(payload)
        assert kwargs['reasoning'] is True
        return candidate(),{'model':'stub'}
    monkeypatch.setattr(deep,'model_json',call)
    def no_search(*args,**kwargs): raise AssertionError('Refinement must reuse observed sources')
    monkeypatch.setattr(deep.web_research,'search',no_search)
    result=deep.refine(r['id'],'根据已读材料修订',chat)
    saved=store.get_run(r['id'])
    assert result['revision']==2 and saved['valuation_history']==r['valuation_history']
    assert saved['memo_revisions'][0]['memo']==r['memo']
    assert 'web-product' in seen['allowed_reference_ids']
    assert saved['deep_research_attempts'][-1]['mode']=='source_refinement'
    assert saved['deep_research_attempts'][-1]['base_report_revision']==1
    assert saved['deep_research_attempts'][-1]['saved_report_revision']==2
    assert '未另行调用反方模型' in saved['memo']['review']['semantic_status']


def test_refine_resumes_failed_draft_with_revalidation_and_atomic_save_marker(run,monkeypatch):
    r=source_run(run)
    chat=store.new_conversation(r['id'])
    first=store.begin_turn(chat['id'],'修订并保存报告','stub','documents')
    attempt_id=store.now()
    broken=candidate();broken['sections'][0]['evidence_ids']=['missing-source']
    store.update_run(r['id'],lambda current: current.setdefault('deep_research_attempts',[]).append({
        'id':attempt_id,'started_at':attempt_id,'run_id':r['id'],'chat_id':chat['id'],
        'turn_id':first['turn_id'],'task_turn_id':first['turn_id'],'step_id':'failed-report',
        'base_report_revision':1,'question':'修订并保存报告','repaired_draft':broken,
        'review':{'issues':['先核对业务引文']}}))
    def failed(c):
        c['steps'].append({'id':'failed-report','turn_id':first['turn_id'],'tool':'refine_investment_memo',
            'state':'failed','started_at':attempt_id,'finished_at':store.now(),
            'result':{'error':'引用失效'}})
    store.update_conversation(chat['id'],failed)
    agent.finish_error(chat['id'],first['turn_id'],'引用失效，报告尚未保存','failed')
    continued=store.begin_turn(chat['id'],'继续未完成部分','stub','documents',resume_from_turn_id=first['turn_id'])
    seen=[]
    def call(system,payload,*args,**kwargs):
        seen.append(copy.deepcopy(payload))
        assert payload['working_draft']==broken
        assert payload['previous_report']['title']=='原报告'
        assert [i['text'] for i in payload['prior_review_issues']]==['先核对业务引文']
        assert 'missing-source' in payload['working_draft_validation_feedback']
        return candidate(),{'model':'stub'}
    monkeypatch.setattr(deep,'model_json',call)
    result=deep.refine(r['id'],'修订并保存报告',continued)
    saved=store.get_run(r['id'])
    assert result['revision']==2 and len(seen)==1
    assert saved['deep_research_attempts'][0]['repaired_draft']==broken
    newest=saved['deep_research_attempts'][-1]
    assert newest['resumed_from_attempt_id']==attempt_id
    assert newest['saved_report_revision']==saved['report_revision']==2

def test_cancelled_refinement_cannot_overwrite_report(run,monkeypatch):
    from app.research import Cancelled
    r=source_run(run);chat=store.new_conversation(r['id'])
    def call(*args,**kwargs):
        store.cancel_conversation(chat['id'])
        return candidate(),{}
    monkeypatch.setattr(deep,'model_json',call)
    with pytest.raises(Cancelled): deep.refine(r['id'],'修订',chat)
    assert store.get_run(r['id'])['memo']==r['memo']

def test_report_dispatch_respects_no_repeat_search_and_reuses_failed_build(run):
    source_run(run)
    chat={'run_id':run['id'],'messages':[{'role':'user','content':'根据已读原文修订报告，不需要重复联网'}]}
    assert agent.memo_tool_for_request('build_investment_memo',chat,[])=='refine_investment_memo'
    chat['messages'][0]['content']='请联网重新调查并生成报告'
    assert agent.memo_tool_for_request('build_investment_memo',chat,[])=='build_investment_memo'
    assert agent.memo_tool_for_request('build_investment_memo',chat,[{'tool':'build_investment_memo','result':{'error':'引用无效'}}])=='refine_investment_memo'


@pytest.mark.parametrize('operation',['build','refine'])
def test_permitted_report_preserves_question_ledger_despite_model_edits(run,monkeypatch,operation):
    from app import questions
    from app.turn_constraints import effective_rules
    r=source_run(run)
    original=copy.deepcopy(r['research_questions'])
    chat=store.new_conversation(r['id'])
    # Pass the pre-turn chat deliberately: the nested workflow must reload the
    # actual user's current restriction, rather than trust its caller's snapshot.
    current=store.begin_turn(chat['id'],'请生成并保存报告，不修改研究问题。','stub','documents')
    assert effective_rules(current)['no_question_writes'] and not effective_rules(current)['no_report']
    monkeypatch.setattr(questions,'plan',lambda *a,**kw:pytest.fail('question planning is forbidden'))
    monkeypatch.setattr(questions,'apply',lambda *a,**kw:pytest.fail('question updates are forbidden'))
    draft=tracked_candidate() if operation=='build' else candidate()
    draft['question_answers']=[{'question_id':'model-invented-id','answer':'不得采纳的新答案'}]
    replies=[draft]
    if operation=='build':
        monkeypatch.setattr(deep.documents,'attached_documents',lambda _: [{'ticker':'300271.SZ','year':2025,'title':'2025年年度报告','page_count':194}])
        monkeypatch.setattr(deep.documents,'ensure_index',lambda _: [])
        monkeypatch.setattr(deep.documents,'search_attached',lambda *args: {'snippets':[]})
        monkeypatch.setattr(deep.web_research,'search',lambda q,**kw: {'queries':q,'results':[],'limitations':[]})
        replies=[{'questions':[{'question':'模型擅自新增的问题','completion_criteria':'不应落库'}],
                  'queries':['华宇软件 产品 客户'],'pdf_queries':['产品'],'focus':'商业化'},
                 {'queries':[],'gaps':[]},draft,{'issues':[],'requires_revision':False}]
    else:
        monkeypatch.setattr(deep.web_research,'search',lambda *a,**kw:pytest.fail('refine should not search'))
    outputs=iter(replies)
    def model(system,payload,*a,**kw):
        assert deep.QUESTION_READ_ONLY in system
        return next(outputs),{'model':'stub'}
    monkeypatch.setattr(deep,'model_json',model)
    result=getattr(deep,operation)(r['id'],'允许更新报告，但保持研究问题',chat)
    saved=store.get_run(r['id'])
    assert result['artifact']=='report' and result['revision']==2
    assert '问题记录保持不变' in result['question_update_note']
    assert saved['research_questions']==original
    assert saved['memo']['question_answers']==[]
    assert saved['memo_revisions'][0]['memo']==r['memo']
    assert saved['valuation_history']==r['valuation_history']


def test_save_memo_can_explicitly_disallow_question_side_effects(run):
    r=source_run(run);original=copy.deepcopy(r['research_questions'])
    memo=deep.validate_memo(candidate(),r)
    # Guard the persistence boundary independently of prompt or validation use.
    memo['question_answers']=[{'question_id':'invented','answer':'不应写入'}]
    result=deep.save_memo(r['id'],memo,'只改报告',allow_question_writes=False)
    saved=store.get_run(r['id'])
    assert result['artifact']=='report' and saved['research_questions']==original
    assert saved['memo']['question_answers']==[]
