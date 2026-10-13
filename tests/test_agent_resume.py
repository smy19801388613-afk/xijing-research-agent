import copy

import pytest

from app import agent, store, deep_research, questions
from app.turn_constraints import constraints, effective_rules, enforce, coverage


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    run=store.create_run({'ticker':'300271.SZ','year':2025,'as_of':'2026-10-02','mode':'sample','question':'研究业务'})
    run.update(state='completed_with_gaps',memo={'title':'旧报告','summary':'旧结论','theses':[]},research_sources=[])
    store.save_run(run)
    return store.new_conversation(run['id'])


def begin(chat,text,**kw):
    return store.begin_turn(chat['id'],text,'stub','documents',**kw)


def idle(chat):
    store.update_conversation(chat['id'],lambda c:c.update(state='idle'))


@pytest.mark.parametrize('phrase',['不要用任何工具','别调用工具','不需要调用任何工具','无需使用任何工具'])
def test_common_no_tools_phrases_block_dispatch(chat,monkeypatch,phrase):
    current=begin(chat,phrase)
    monkeypatch.setattr(agent.web_research,'search',lambda *a,**kw:pytest.fail('network executed'))
    assert current['effective_constraints']['rules']['no_tools']
    with pytest.raises(ValueError,match='不使用工具'):
        agent.tool(current,'web_search',{'queries':['公司']})


@pytest.mark.parametrize('text,tool',[
    ('不联网','peer_comparison'),('不计算','fcff_bridge'),('不做估值','fcff_bridge'),
    ('只读','plan_research_questions'),('不修改研究问题','update_research_question'),
])
def test_capability_registry_covers_new_and_nested_tools(chat,text,tool):
    current=begin(chat,text)
    with pytest.raises(ValueError): enforce(current,tool)


def test_turn_limits_do_not_become_persistent(chat):
    current=begin(chat,'本轮不联网，不改报告。')
    assert current['effective_constraints']['rules']['no_network']
    assert current['persistent_constraints']=={}
    idle(chat)
    fresh=begin(chat,'研究一下公司的经营情况。')
    assert not effective_rules(fresh)['no_network']
    assert not effective_rules(fresh)['no_report']


def test_standing_preference_temporary_override_and_explicit_clear(chat):
    first=begin(chat,'以后不要联网，本轮不改报告。')
    assert first['persistent_constraints']=={'no_network':True}
    idle(chat)
    override=begin(chat,'本轮联网查证产品。')
    assert not effective_rules(override)['no_network']
    assert override['persistent_constraints']['no_network']
    assert not effective_rules(override)['no_report']
    idle(chat)
    following=begin(chat,'讨论一下产品。')
    assert effective_rules(following)['no_network']
    idle(chat)
    cleared=begin(chat,'取消不联网限制。')
    assert not effective_rules(cleared)['no_network']
    assert cleared['persistent_constraints']=={}


def test_resume_inherits_task_limits_but_next_independent_task_does_not(chat):
    original=begin(chat,'只用已有材料，不改报告，解释经营情况。')
    agent.finish_error(chat['id'],original['turn_id'],'模拟超时','failed')
    continued=begin(chat,'继续未完成部分',resume_from_turn_id=original['turn_id'])
    assert continued['effective_constraints']['scope']=='continuation'
    assert effective_rules(continued)['no_network'] and effective_rules(continued)['no_report']
    assert agent.task_request(continued)==original['messages'][-1]['content']
    idle(chat)
    fresh=begin(chat,'联网并更新报告。')
    assert not effective_rules(fresh)['no_network'] and not effective_rules(fresh)['no_report']


def test_cannot_resume_stale_or_different_run(chat):
    original=begin(chat,'分析经营情况')
    agent.finish_error(chat['id'],original['turn_id'],'超时','failed')
    with pytest.raises(ValueError,match='最近一轮'):
        begin(chat,'继续未完成部分',resume_from_turn_id='older-turn')
    store.update_conversation(chat['id'],lambda c:c.update(run_id=None))
    with pytest.raises(ValueError,match='上下文已变化'):
        begin(chat,'继续未完成部分',resume_from_turn_id=original['turn_id'])


def test_failure_reports_real_saved_revision_and_resume_reuses_results(chat,monkeypatch):
    original_text='联网查证产品并生成报告。'
    first=begin(chat,original_text)
    calls={'search':0,'report':0}
    def search(*a,**kw):
        calls['search']+=1
        return {'results':[{'url':'https://example.com/product','title':'产品','snippet':'检索线索'}]}
    def refine(rid,*a,**kw):
        calls['report']+=1
        store.update_run(rid,lambda r:r.update(memo={'title':'新报告','summary':'新结论','theses':[]},report_revision=2))
        return {'artifact':'report','revision':2}
    monkeypatch.setattr(agent.web_research,'search',search)
    monkeypatch.setattr(deep_research,'refine',refine)
    decisions=iter([{'action':'tool','tool':'web_search','arguments':{'queries':['产品']}},
                    {'action':'tool','tool':'refine_investment_memo','arguments':{}}])
    def model(*a,**kw):
        try: return next(decisions),{'model':'stub'}
        except StopIteration: raise agent.ProviderError('模拟超时')
    monkeypatch.setattr(agent,'model_json',model)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id']); last=saved['messages'][-1]
    assert last['delivery']['report_changed'] and last['delivery']['report_revision_after']==2
    assert last['delivery']['can_resume'] and '第 2 版' in last['content']
    assert '均未因此修改' not in last['content']
    begin(chat,'继续未完成部分',resume_from_turn_id=first['turn_id'])
    resumed_decisions=iter([{'action':'tool','tool':'web_search','arguments':{'queries':['产品']}},
                           {'action':'tool','tool':'refine_investment_memo','arguments':{'question':'再改一下'}},
                           {'action':'respond','message':'研究结果已经保留；现有搜索仅提供线索。'}])
    seen=[]
    def resumed_model(system,payload,*a,**kw):
        seen.append(copy.deepcopy(payload))
        return next(resumed_decisions),{'model':'stub'}
    monkeypatch.setattr(agent,'model_json',resumed_model)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert calls=={'search':1,'report':1}
    assert seen[0]['original_request']==original_text and seen[0]['continuation']
    assert len(seen[0]['tool_results'])==2
    assert len([s for s in saved['steps'] if s.get('reused_from_step_id')])==2
    assert saved['messages'][-1]['delivery']['status']=='completed'
    assert not saved['messages'][-1]['delivery']['can_resume']
    assert saved['messages'][-1]['coverage']['search_calls']==0


def test_denied_tool_after_success_does_not_claim_report_unchanged(chat,monkeypatch):
    first=begin(chat,'不联网，请更新报告。')
    def refine(rid,*a,**kw):
        store.update_run(rid,lambda r:r.update(report_revision=2))
        return {'artifact':'report','revision':2}
    monkeypatch.setattr(deep_research,'refine',refine)
    decisions=iter([{'action':'tool','tool':'refine_investment_memo','arguments':{}},
                    {'action':'tool','tool':'web_search','arguments':{'queries':['不该联网']}}])
    def model(*a,**kw):
        try: return next(decisions),{'model':'stub'}
        except StopIteration: raise agent.ProviderError('模拟超时')
    monkeypatch.setattr(agent,'model_json',model)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['messages'][-1]['delivery']['report_changed']
    assert '第 2 版' in saved['messages'][-1]['content']
    assert '均未因此修改' not in saved['messages'][-1]['content']
    assert [s['tool'] for s in saved['steps']]==['refine_investment_memo']


def test_shared_eligibility_keeps_ocr_reading_distinct_from_confirmed_support():
    source={'url':'https://example.com/ocr.pdf','text':'扫描正文','read_status':'windows_ocr','published_at':'2026-01-01'}
    observed=coverage([{'tool':'read_public_pdf','state':'completed','result':{'results':[source]}}],
                      '阅读原文',as_of='2026-10-02')
    assert observed['body_sources']==1
    assert observed['sources'][0]['eligibility']['basis']=='ocr'
    assert not observed['sources'][0]['eligibility']['supports_claim']
    source['published_at']='2026-10-03'
    assert coverage([{'tool':'read_public_pdf','state':'completed','result':{'results':[source]}}],
                    '阅读原文',as_of='2026-10-02')['status']=='partial'


def report_with_answer(chat):
    source={'id':'web-contract','kind':'web','url':'https://example.com/contract','title':'合同披露',
            'text':'公司已签订付费合同，尚未披露实际回款。','read_status':'body_read','published_at':'2026-09-01'}
    store.update_run(chat['run_id'],lambda r:r.update(research_sources=[source]))
    run=store.get_run(chat['run_id']);qid=run['research_questions'][0]['id']
    answer={'question_id':qid,'status':'supported','answer':'已有付费合同原文，实际回款待进一步验证。',
            'bindings':[{'source_id':source['id'],'quote':'公司已签订付费合同','relation':'support','reason':'披露说明存在付费业务'}],
            'counter_evidence':'签订合同不等于已经收到款项','gaps':[],'next_action':'继续核对实际回款'}
    memo={'schema_version':2,'title':'业务与回款','summary':'付费业务存在，回款仍需核对','stance':'继续研究',
          'theses':[{'title':title,'body':'存在付费合同原文，仍需跟踪回款。','evidence_ids':[source['id']],
                     'counter_evidence':'合同不等于收入或现金流','invalidate_if':'取得新的原文反证','status':'inference'}
                    for title in ['商业化证据','回款缺口']],
          'sections':[{'key':key,'body':'根据合同原文作出有限推断，保持回款缺口。','evidence_ids':[source['id']],'status':'inference'}
                      for key in deep_research.SECTION_TITLES],
          'questions':['实际回款是否完成？'],'limitations':['尚未取得回款原文'],'question_answers':[answer]}
    return memo,qid


def test_revise_report_applies_question_answers_and_clears_internal_snapshot(chat):
    memo,qid=report_with_answer(chat)
    current=begin(chat,'请更新并保存报告。')
    result=agent.tool(current,'revise_report',{'memo':memo})
    saved=store.get_run(chat['run_id'])
    assert result['revision']==2
    answer=next(q for q in saved['research_questions'] if q['id']==qid)
    assert answer['status']=='supported' and answer['answer']==memo['question_answers'][0]['answer']
    assert questions.report_sync(saved)['status']=='current'
    assert '_question_input_snapshot' not in saved['memo']


def test_revise_report_can_save_without_modifying_question_records(chat):
    memo,qid=report_with_answer(chat)
    before=store.get_run(chat['run_id'])['research_questions']
    current=begin(chat,'不要修改研究问题，请更新报告。')
    result=agent.tool(current,'revise_report',{'memo':memo})
    saved=store.get_run(chat['run_id'])
    assert result['revision']==2 and '问题记录保持不变' in result['note']
    assert saved['research_questions']==before
    assert saved['memo']['question_answers']==[]


@pytest.mark.parametrize('change_kind',['question','source','no_report'])
def test_resume_updates_report_for_changed_inputs_once_unless_prohibited(chat,monkeypatch,change_kind):
    memo,qid=report_with_answer(chat)
    first=begin(chat,'请生成并保存报告。')
    calls=[]
    def refine(rid,*a,**kw):
        calls.append(1)
        def save(run):
            run.update(memo=questions.freeze_report(run,memo),report_revision=run['report_revision']+1)
        run=store.update_run(rid,save)
        return {'artifact':'report','revision':run['report_revision']}
    monkeypatch.setattr(deep_research,'refine',refine)
    decisions=iter([{'action':'tool','tool':'refine_investment_memo','arguments':{}}])
    def first_model(*a,**kw):
        try: return next(decisions),{'model':'stub'}
        except StopIteration: raise agent.ProviderError('模拟答复阶段失败')
    monkeypatch.setattr(agent,'model_json',first_model)
    agent.execute(chat['id'])
    def new_evidence(run):
        if change_kind=='question': run['research_questions'][0].update(answer='补充了一项新发现',status='partial')
        else: run['research_sources'][0]['text']+=' 新披露：客户已经支付首期款项。'
    store.update_run(chat['run_id'],new_evidence)
    assert questions.report_sync(store.get_run(chat['run_id']))['needs_update']
    text='继续未完成部分，本轮不要改报告。' if change_kind=='no_report' else '继续未完成部分'
    begin(chat,text,resume_from_turn_id=first['turn_id'])
    early_reply=[] if change_kind=='no_report' else [{'action':'respond','message':'之前报告已经保存，无需继续。'}]
    resumed=iter(early_reply+[{'action':'tool','tool':'refine_investment_memo','arguments':{}},
                  {'action':'tool','tool':'refine_investment_memo','arguments':{}},
                  {'action':'respond','message':'已整理现有研究材料。'}])
    monkeypatch.setattr(agent,'model_json',lambda *a,**kw:(next(resumed),{'model':'stub'}))
    agent.execute(chat['id'])
    saved=store.get_run(chat['run_id']);turn=store.get_conversation(chat['id'])
    if change_kind=='no_report':
        assert len(calls)==1 and saved['report_revision']==2
        assert len(turn['steps'])==1
    else:
        assert len(calls)==2 and saved['report_revision']==3
        assert len([s for s in turn['steps'] if s.get('reused_from_step_id')])==1
        assert not questions.report_sync(saved)['needs_update']


@pytest.mark.parametrize('tool,state,result',[
    ('web_search','failed',{'results':[{'url':'https://example.com'}]}),
    ('web_search','completed',{'status':'error','results':[]}),
    ('web_search','completed',{'status':'empty','results':[]}),
    ('web_search','completed',{'error':'timeout','results':[{'url':'https://example.com'}]}),
    ('read_webpage','completed',{'status':'ok','text':''}),
    ('read_public_pdf','completed',{'status':'needs_ocr','results':[]}),
])
def test_failed_or_unusable_retrieval_is_never_reused(tool,state,result):
    chat={'turn_id':'next','turn_contexts':{'next':{'resume_from_turn_id':'first','task_turn_ids':['first','next']}},
          'steps':[{'id':'step','turn_id':'first','tool':tool,'arguments':{},'state':state,'result':result}]}
    assert agent.reusable_step(chat,tool,{}) is None


def test_revise_report_rejects_atomic_question_change(chat,monkeypatch):
    memo,qid=report_with_answer(chat)
    current=begin(chat,'更新报告')
    validate=deep_research.validate_memo
    def validation_then_other_writer(candidate,run):
        draft=validate(candidate,run)
        store.update_run(chat['run_id'],lambda r:r['research_questions'][0].update(answer='另一对话已保存的最新答案',status='partial'))
        return draft
    monkeypatch.setattr(deep_research,'validate_memo',validation_then_other_writer)
    with pytest.raises(ValueError,match='生成期间问题或引用来源已变化'):
        agent.tool(current,'revise_report',{'memo':memo})
    saved=store.get_run(chat['run_id'])
    assert saved['report_revision']==1 and saved['memo']['title']=='旧报告'
    assert saved['research_questions'][0]['answer']=='另一对话已保存的最新答案'


@pytest.mark.parametrize('kind',['question','source'])
def test_revise_report_uses_pre_model_snapshot_when_another_writer_changes_context(chat,monkeypatch,kind):
    memo,qid=report_with_answer(chat)
    begin(chat,'更新报告')
    calls=[]
    def model(*args,**kwargs):
        if calls: raise agent.ProviderError('本次停止，保留并发修改')
        calls.append(1)
        def concurrent_change(run):
            if kind=='question': run['research_questions'][0].update(answer='其他对话新答案',status='partial')
            else: run['research_sources'][0]['text']+=' 后续更正：上述合同已撤销。'
        store.update_run(chat['run_id'],concurrent_change)
        return {'action':'tool','tool':'revise_report','arguments':{'memo':memo}},{'model':'stub'}
    monkeypatch.setattr(agent,'model_json',model)
    agent.execute(chat['id'])
    saved=store.get_run(chat['run_id']);turn=store.get_conversation(chat['id'])
    assert saved['report_revision']==1 and saved['memo']['title']=='旧报告'
    assert turn['steps'][0]['state']=='failed'
    assert '生成期间问题或引用来源已变化' in turn['steps'][0]['result']['error']
