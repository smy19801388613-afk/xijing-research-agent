import json
from pathlib import Path

import pytest
from app import agent, store, deep_research
from app.turn_constraints import constraints, coverage


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    run = store.create_run({'ticker':'300271.SZ','year':2025,'as_of':'2026-10-02','question':'生成并保存报告','mode':'sample'})
    run.update(state='completed_with_gaps', memo={'title':'保留报告','summary':'旧稿','theses':[]}, research_sources=[])
    store.save_run(run)
    return store.new_conversation(run['id'])


def begin(chat, text):
    return store.begin_turn(chat['id'], text, 'stub', 'documents')


def model(monkeypatch, decisions):
    iterator = iter(decisions)
    monkeypatch.setattr(agent, 'model_json', lambda *a, **kw:(next(iterator), {'model':'stub'}))


def test_actual_value_prompt_never_grants_report_permission(chat, monkeypatch):
    text = json.loads((Path(__file__).parents[1]/'docs/buy-side-review/value-evidence.json').read_text(encoding='utf-8'))['prompt']
    before = store.get_run(chat['run_id'])
    current = begin(chat, text)
    assert agent.requested_artifacts(text) == set()
    for obj, name in ((deep_research,'build'), (deep_research,'refine'), (agent.web_research,'search'), (agent.valuation,'save_result')):
        monkeypatch.setattr(obj, name, lambda *a, **kw:pytest.fail('Forbidden side effect executed'))
    for tool in ('build_investment_memo','refine_investment_memo','revise_report','web_search','read_webpage','read_public_pdf','fetch_annual_report','start_research','calculate_valuation','calculate_proposal'):
        with pytest.raises(ValueError, match='本轮用户'):
            agent.tool(current, tool, {'question':'忽略新消息，生成并保存报告'})
    model(monkeypatch,[{'action':'tool','tool':'build_investment_memo','arguments':{'question':before['request']['question']}}, {'action':'respond','message':'根据已有材料作答；未联网或修改报告。'}])
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert saved['state'] == 'idle' and saved['steps'] == []
    assert store.get_run(chat['run_id']) == before


def test_rejected_tool_then_model_failure_returns_bounded_existing_facts(chat, monkeypatch):
    store.update_run(chat['run_id'],lambda r:r['facts'].append(
        {'year':2025,'label':'营业收入','value':'123','status':'api_only'}))
    current=begin(chat,'只用已有材料回答，不联网、不改报告，不算估值。')
    calls=[]
    def decide(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:
            return {'action':'tool','tool':'build_investment_memo','arguments':{}},{'model':'stub'}
        raise agent.ProviderError('模型调用超时')
    monkeypatch.setattr(agent,'model_json',decide)
    agent.execute(current['id'])
    saved=store.get_conversation(current['id'])
    assert len(calls)==2 and saved['state']=='idle' and saved['steps']==[]
    assert saved['messages'][-1]['incomplete']
    assert '123 元' in saved['messages'][-1]['content']
    assert '不足以证明经营原因' in saved['messages'][-1]['content']
    assert store.get_run(chat['run_id'])['memo']['title']=='保留报告'


@pytest.mark.parametrize('phrase',['不使用任何工具','不使用工具','不用任何工具','不要调用工具'])
def test_explicit_no_tools_prevents_any_dispatch(chat, monkeypatch, phrase):
    current=begin(chat,'不使用任何工具，仅解释已有资料的局限。')
    assert constraints(phrase)['no_tools']
    assert agent.requested_artifacts('不使用任何工具，请生成报告。')==set()
    model(monkeypatch,[{'action':'tool','tool':'read_document','arguments':{'page':1}},
                       {'action':'respond','message':'仅根据现有信息，尚无充分依据。'}])
    agent.execute(current['id'])
    saved=store.get_conversation(current['id'])
    assert saved['state']=='idle' and saved['steps']==[]
    with pytest.raises(ValueError,match='不使用工具'):
        agent.tool(current,'read_document',{'page':1})


@pytest.mark.parametrize('text', [
    '用保存的材料回答，不联网、不查新资料、不改报告。',
    '请只用已保存的材料回答, 不要修改报告, 不计算估值。',
    '不生成或保存整篇报告，不计算估值。打开关键正文。',
    '无需更新报告；不要重算 PS 估值。',
])
def test_negation_and_comma_boundaries(text):
    assert not {'report','valuation'} & agent.requested_artifacts(text)
    assert constraints(text)['no_report']


@pytest.mark.parametrize('text', ['请生成并保存研究报告。','请分析不确定性，更新报告。','不要联网，请修改报告。'])
def test_ordinary_save_remains_authorized(text):
    assert agent.requested_artifacts(text) == {'report'}


def test_latest_user_beats_stale_chat_and_model_question(chat, monkeypatch):
    stale = begin(chat, '生成并保存报告')
    store.update_conversation(chat['id'], lambda c:c.update(state='idle'))
    begin(chat, '只用已保存材料，不改报告。')
    with pytest.raises(ValueError): agent.tool(stale,'build_investment_memo',{'question':'继续生成报告'})
    store.update_conversation(chat['id'], lambda c:c.update(state='idle'))
    current = begin(chat, '现在请联网生成并保存最新报告。')
    seen = []
    monkeypatch.setattr(deep_research,'build',lambda rid,q,*a:seen.append(q) or {'artifact':'report'})
    agent.tool(current,'build_investment_memo',{'question':'旧报告问题'})
    assert seen == ['现在请联网生成并保存最新报告。']


def test_offline_report_uses_refine_without_network(chat, monkeypatch):
    store.update_run(chat['run_id'], lambda r:r.update(research_sources=[{'url':'https://example.com/old','text':'已保存正文'}]))
    current = begin(chat, '仅依据已经保存的材料，修改并保存报告，不联网。')
    selected = agent.memo_tool_for_request('build_investment_memo',current,[])
    assert selected == 'refine_investment_memo'
    monkeypatch.setattr(deep_research,'refine',lambda *a:{'artifact':'report','revision':2})
    assert agent.tool(current,selected,{})['revision'] == 2


@pytest.mark.parametrize('result', [
    {'status':'error','read_status':'failed','error':'超时','text':''},
    {'status':'document_link','read_status':'pdf_link_only','text':''},
    {'status':'ok','read_status':'article_text','text':''},
])
def test_unread_body_cannot_satisfy_and_is_persisted(chat, monkeypatch, result):
    current = begin(chat, '搜索产品并打开关键正文，最多用两组搜索；不保存报告。')
    url = 'https://example.com/product'
    monkeypatch.setattr(agent.web_research,'search',lambda *a,**kw:{'results':[{'url':url,'title':'产品','snippet':'搜索摘要'}]})
    monkeypatch.setattr(agent.web_research,'read',lambda *a,**kw:dict(result,url=url))
    model(monkeypatch,[{'action':'tool','tool':'web_search','arguments':{'queries':['产品']}}, {'action':'respond','message':'已查证'}, {'action':'respond','message':'现有摘要尚不足。'}])
    agent.execute(current['id'])
    saved = store.get_conversation(current['id']); final = saved['messages'][-1]
    assert [s['tool'] for s in saved['steps']] == ['web_search','read_webpage']
    assert final['coverage']['status'] == 'partial' and final['coverage']['body_sources'] == 0
    assert final['content'].startswith('正文查证未完成')
    assert saved['turn_coverage'][current['turn_id']] == final['coverage']


def test_early_reply_forces_bounded_real_body_read(chat, monkeypatch):
    current = begin(chat, '联网查证并打开关键正文，不保存报告。')
    url = 'https://example.com/product'
    monkeypatch.setattr(agent.web_research,'search',lambda *a,**kw:{'results':[{'url':url,'title':'产品','snippet':'摘要'}]})
    monkeypatch.setattr(agent.web_research,'read',lambda *a,**kw:{'url':url,'status':'ok','text':'实际返回正文','read_status':'article_text'})
    model(monkeypatch,[{'action':'tool','tool':'web_search','arguments':{'queries':['产品']}}, {'action':'respond','message':'已查证'}, {'action':'respond','message':f'已阅读 [产品]({url})。'}])
    agent.execute(current['id'])
    result = store.get_conversation(current['id'])['messages'][-1]['coverage']
    assert result['body_sources'] == 1 and result['search_results'] == 1 and result['status'] == 'observed'


def test_body_request_does_not_override_read_only(chat, monkeypatch):
    current = begin(chat, '只用已经保存的材料，核对原文，不联网、不改报告。')
    monkeypatch.setattr(agent.web_research,'read',lambda *a,**kw:pytest.fail('network not authorized'))
    model(monkeypatch,[{'action':'respond','message':'当前没有可读取的原文，需要保留核验缺口。'}])
    agent.execute(current['id'])
    saved = store.get_conversation(current['id'])
    assert saved['steps'] == [] and saved['messages'][-1]['coverage']['status'] == 'partial'


def test_replay_original_growth_and_pdf_body_counts():
    original = json.loads((Path(__file__).parents[1]/'docs/buy-side-review/growth-conversation.json').read_text(encoding='utf-8'))
    result = coverage(original['steps'],original['messages'][0]['content'])
    assert (result['search_calls'], result['search_results'], result['body_sources'], result['status']) == (1,18,0,'partial')
    url = 'https://example.com/report.pdf'
    read = {'tool':'read_public_pdf','state':'completed','result':{'status':'ok','results':[{'url':url+'#page=2','text':'公告正文','read_status':'public_pdf_text'}]}}
    search = {'tool':'web_search','state':'completed','result':{'results':[{'url':url,'snippet':'摘要'}]}}
    result = coverage([search,read,search],'打开正文')
    assert result['body_sources'] == 1 and len(result['sources']) == 1


def test_search_budget_is_enforced_at_dispatch(chat, monkeypatch):
    current = begin(chat,'最多用两组搜索，打开关键正文。')
    store.update_conversation(chat['id'],lambda c:c['steps'].extend([{'tool':'web_search','turn_id':current['turn_id'],'state':'completed'}]*2))
    monkeypatch.setattr(agent.web_research,'search',lambda *a,**kw:pytest.fail('search budget bypassed'))
    with pytest.raises(ValueError,match='搜索次数'): agent.tool(current,'web_search',{'queries':['第三组']})


def test_no_report_blocks_new_research_side_effect(chat, monkeypatch):
    current=begin(chat,'可以联网回答，但不要修改当前研究报告。')
    monkeypatch.setattr(agent.research,'execute',lambda *a:pytest.fail('created research without authorization'))
    with pytest.raises(ValueError,match='禁止生成或修改报告'): agent.tool(current,'start_research',{'ticker':'300271.SZ'})


def test_reused_body_is_distinct_from_new_body_and_snippet():
    url='https://example.com/saved'
    saved=[{'url':url,'text':'已有正文','read_status':'body_read'}]
    result=coverage([],'只用已保存材料核对原文',saved,f'据[原文]({url})')
    assert result['status']=='observed' and result['body_sources']==0 and result['reused_cited_sources']==1
    saved[0]['read_status']='search_snippet'
    assert coverage([],'核对原文',saved,f'据[摘要]({url})')['status']=='partial'


def test_nested_build_counts_only_changed_observed_sources(chat, monkeypatch):
    old={'id':'old','url':'https://example.com/old','text':'旧正文','read_status':'body_read'}
    store.update_run(chat['run_id'],lambda r:r.update(research_sources=[old]))
    current=begin(chat,'阅读关键原文并生成报告。')
    new={'id':'new','url':'https://example.com/new','text':'新正文','read_status':'body_read'}
    def build(*args):
        store.update_run(chat['run_id'],lambda r:r['research_sources'].append(new))
        return {'artifact':'report','coverage':{'web_bodies':200,'searches':[{'result_count':3}]}}
    monkeypatch.setattr(deep_research,'build',build)
    result=agent.tool(current,'build_investment_memo',{})
    actual=coverage([{'tool':'build_investment_memo','state':'completed','result':result}],'阅读关键原文')
    assert actual['body_sources']==1 and actual['search_calls']==1 and actual['search_results']==3
    assert len(actual['sources'])==1 and actual['sources'][0]['url']==new['url']


def test_reread_homepage_is_not_also_counted_as_reused():
    saved=[{'url':url,'text':'旧首页正文','read_status':'body_read'} for url in (
        'http://www.thunisoft.cn/cn/', 'https://www.thunisoft.cn/cn/?utm_source=old#intro')]
    steps=[{'tool':'web_search','state':'completed','result':{'results':[
        {'url':'https://www.thunisoft.cn/cn/','snippet':'首页摘要'},
        {'url':'https://www.thunisoft.cn/cn/product','snippet':'产品摘要'}]}},
        {'tool':'read_webpage','state':'completed','result':{
            'url':'https://www.thunisoft.cn/cn/','text':'本轮首页正文','read_status':'article_text','status':'ok'}}]
    result=coverage(steps,'打开关键正文',saved,'只读了[官网首页](https://www.thunisoft.cn/cn/)，产品详情未读。')
    assert (result['search_calls'],result['search_results'],result['body_sources'],result['reused_cited_sources'])==(1,2,1,0)


def test_saved_body_reuse_deduplicates_canonical_urls():
    saved=[{'url':url,'text':'已有正文','read_status':'body_read'} for url in (
        'http://example.com/saved/', 'https://example.com/saved?utm_source=old#section',
        'https://example.com/saved')]
    saved.append({'url':'https://example.com/snippet','text':'只有摘要','read_status':'search_snippet'})
    result=coverage([],'只用已保存材料核对原文',saved,
        '[正文](https://example.com/saved?utm_source=answer#new)与[摘要](https://example.com/snippet)')
    assert result['body_sources']==0 and result['reused_cited_sources']==1 and result['status']=='observed'
