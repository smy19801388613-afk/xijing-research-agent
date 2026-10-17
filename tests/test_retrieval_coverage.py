"""Retrieval observations survive failure of their enclosing report task."""
import copy

import pytest
from recommendation_fixture import watch_recommendation

from app import deep_research as deep, store
from app.providers import ProviderError
from app.turn_constraints import coverage, enforce, constraints


def body(url='https://example.com/filing', **overrides):
    return {'url':url, 'text':'已经实际取得的披露正文', 'read_status':'body_read',
            'published_at':'2026-09-01', **overrides}


def event(identity, kind, result=None, state='completed', **arguments):
    return {'id':identity, 'kind':kind, 'state':state, 'arguments':arguments, 'result':result or {}}


def nested(events, state='failed', **extra):
    return {'tool':'build_investment_memo', 'state':state, 'retrieval_events_schema':1,
            'retrieval_events':events, 'result':{'error':'后续报告生成失败'}, **extra}


@pytest.mark.parametrize('outer_state', ['failed', 'cancelled', 'running', 'completed'])
def test_completed_retrieval_survives_report_state_and_is_not_double_counted(outer_state):
    events = [event('search', 'search', {'results':[{'url':body()['url'], 'snippet':'摘要'}]}),
              event('read', 'read_webpage', body())]
    step = nested(events, outer_state, result={'search_runs':[{'result_count':9}],
                                             'observed_sources':[body()]})
    observed = coverage([step, copy.deepcopy(step)], '阅读原文')
    assert (observed['search_calls'], observed['search_results'], observed['body_sources']) == (1, 1, 1)
    assert observed['status'] == 'observed' and not observed['telemetry_incomplete']


def test_failed_and_unfinished_reads_are_not_bodies():
    step = nested([event('failed', 'read_webpage', {'error':'读取失败'}, 'failed', url='https://example.com/bad'),
                   event('waiting', 'read_webpage', body(), 'running', url='https://example.com/wait'),
                   event('search', 'search', {'error':'搜索失败'}, 'failed')], 'cancelled')
    observed = coverage([step], '阅读原文')
    assert observed['body_sources'] == 0 and observed['failed_sources'] == 1
    assert observed['search_calls'] == 0 and observed['search_attempts'] == 1
    assert observed['status'] == 'partial'


def test_local_and_public_pdf_cutoff_and_page_counts_are_distinct():
    local = {'url':'/api/documents/annual/file#page=3', 'text':'本地原文',
             'read_status':'pdf_text', 'published_at':'2026-09-01'}
    future = dict(local, url='/api/documents/future/file#page=1', published_at='2026-11-01')
    public = body('https://example.com/report.pdf#page=4', read_status='public_pdf_text')
    step = nested([event('local', 'read_document', {'snippets':[local, future]}),
                   event('public', 'read_public_pdf', {'results':[public]}),
                   event('snippets', 'search_documents', {'snippets':[dict(local, url='/api/documents/annual/file#page=7')]})])
    observed = coverage([step], '核对原文', as_of='2026-10-01')
    assert observed['document_pages'] == 1 and observed['body_sources'] == 1
    assert observed['status'] == 'observed'


def test_legacy_failed_build_is_unknown_not_false_zero():
    step = {'tool':'build_investment_memo', 'state':'failed', 'progress':[{'label':'读取联网原文并保留引用'}],
            'result':{'error':'模型网络请求失败'}}
    observed = coverage([step], '阅读原文')
    assert observed['telemetry_incomplete'] and observed['status'] == 'unknown'
    assert '不能据此断言' in observed['gaps'][0]
    assert coverage([nested([], 'failed')], '阅读原文')['status'] == 'partial'


def test_legacy_build_local_sources_do_not_prove_page_reading():
    step = {'tool':'build_investment_memo','state':'completed','result':{'observed_sources':[
        body('/api/documents/annual/file#page=15',read_status='pdf_text')]}}
    observed = coverage([step], '阅读原文')
    assert observed['document_pages'] == 0 and observed['telemetry_incomplete']


def test_local_page_citations_do_not_double_count_new_read_or_match_page_prefix():
    saved = [body('/api/documents/annual/file#page='+str(page),read_status='pdf_text') for page in (1, 15, 16)]
    step = nested([event('read','read_document',{'snippets':[saved[1]]})])
    observed = coverage([step], '阅读原文', saved,
                        '[本轮](/api/documents/annual/file#page=15) [已有](/api/documents/annual/file#page=16)')
    assert observed['document_pages'] == observed['reused_cited_sources'] == 1


def test_inventory_does_not_satisfy_current_reading_or_count_future_snippets():
    saved = [body(), body('http://example.com/filing/?utm_source=copy'),
             body('https://example.com/unknown', published_at=None),
             body('https://example.com/ocr', read_status='windows_ocr'),
             body('https://example.com/future', published_at='2026-11-01'),
             body('https://example.com/snippet', read_status='search_snippet')]
    observed = coverage([], '阅读原文', saved, as_of='2026-10-01')
    assert observed['saved_body_sources'] == 3 and observed['saved_verified_body_sources'] == 1
    assert observed['body_sources'] == observed['reused_cited_sources'] == 0
    assert observed['status'] == 'partial'


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    run = store.create_run({'ticker':'300271.SZ','year':2025,'as_of':'2026-10-01',
                            'question':'生成报告','mode':'sample'})
    run.update(company='华宇软件', state='completed_with_gaps', memo={'title':'旧报告'})
    store.save_run(run)
    created = store.new_conversation(run['id'])
    current = store.begin_turn(created['id'], '联网搜索并阅读原文，生成报告', 'stub', 'documents')
    store.update_conversation(current['id'], lambda c:c['steps'].append({
        'id':'build-step', 'tool':'build_investment_memo','turn_id':current['turn_id'],'state':'running'}))
    return store.get_conversation(current['id'])


def test_failed_build_persists_actual_search_and_read_before_model_error(chat, monkeypatch):
    monkeypatch.setattr(deep.documents, 'attached_documents', lambda _:[])
    monkeypatch.setattr(deep.disclosures, 'fetch_annual', lambda *a:(_ for _ in ()).throw(ValueError('无年报')))
    monkeypatch.setattr(deep.web_research, 'search', lambda qs, **kw:{'queries':qs,
        'results':[{'url':body()['url'], 'snippet':'线索', 'published_at':'2026-09-01'}]})
    monkeypatch.setattr(deep.web_research, 'read', lambda *a, **kw:body())
    calls = []
    def model(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1: return {'queries':['华宇软件 财务'], 'questions':[]}, {'model':'stub'}
        raise ProviderError('模型网络请求失败')
    monkeypatch.setattr(deep, 'model_json', model)
    with pytest.raises(ProviderError, match='网络请求失败'):
        deep.build(chat['run_id'], '研究', chat)
    saved = store.get_conversation(chat['id'])
    step = saved['steps'][0]
    step['state'] = 'failed'
    observed = coverage([step], '阅读原文')
    assert observed['search_calls'] == observed['search_results'] == observed['body_sources'] == 1
    assert [e['kind'] for e in step['retrieval_events']] == ['search','read_webpage']
    run = store.get_run(chat['run_id'])
    assert run['memo']['title'] == '旧报告'
    assert run['deep_research_attempts'][-1]['searches'][0]['result_count'] == 1


def test_recorder_preserves_completed_pages_when_conversation_cancelled(chat):
    store.put_document({'id':'annual','announced_date':'2026-09-01'})
    recorder = deep.RetrievalRecorder(chat)
    snippet = {'document_id':'annual','page':3,'title':'年报','url':'/api/documents/annual/file#page=3',
               'text':'已读取的年报原文','extraction':'pdf_text'}
    recorder.retrieve('read_document', {'document_id':'annual','page':3}, lambda:{'snippets':[snippet]})
    recorder.begin('read_webpage', {'url':'https://example.com/wait'})
    stopped = store.cancel_conversation(chat['id'], chat['turn_id'])
    observed = coverage(stopped['steps'], '阅读原文')
    assert observed['document_pages'] == 1 and observed['body_sources'] == 0
    assert stopped['steps'][0]['retrieval_events'][0]['result']['snippets'][0]['published_at'] == '2026-09-01'


def test_reused_tool_does_not_recount_previous_retrieval():
    original = nested([event('read','read_webpage',body())], 'completed')
    reused = dict(original, reused_from_step_id='old-step')
    assert coverage([reused], '阅读原文')['body_sources'] == 0
    assert coverage([original,reused], '阅读原文')['body_sources'] == 1


def limited_chat(steps, turn_ids=('first',), limit=2):
    tid = turn_ids[-1]
    text = f'最多用{limit}次搜索，阅读关键原文'
    return {'turn_id':tid, 'messages':[{'role':'user','content':text}], 'steps':steps,
            'turn_contexts':{tid:{'user_text':text,'rules':constraints(text),'task_turn_ids':list(turn_ids)}}}


def test_search_limit_combines_outer_and_nested_requests_without_reused_calls():
    outer = {'id':'outer','turn_id':'first','tool':'web_search','state':'completed','result':{'results':[]}}
    inner = dict(nested([event('nested-search','search',{'results':[]})]), turn_id='first')
    reused = dict(inner, reused_from_step_id='original')
    chat = limited_chat([outer, reused])
    assert enforce(chat,'web_search')['search_limit'] == 2
    chat['steps'].append(inner)
    with pytest.raises(ValueError,match='搜索次数'): enforce(chat,'web_search')


def test_undispatched_search_does_not_consume_limit_or_supply_results():
    pending = {'turn_id':'first','tool':'web_search','state':'running','search_dispatched':False}
    refused = dict(pending,state='failed',result={'error':'已达到搜索次数'})
    chat = limited_chat([pending,refused],limit=1)
    assert enforce(chat,'web_search')['search_limit'] == 1
    assert coverage(chat['steps'])['search_attempts'] == 0
    chat['steps'][0]['search_dispatched'] = True
    with pytest.raises(ValueError,match='搜索次数'): enforce(chat,'web_search')


@pytest.mark.parametrize('kind', ['outer','nested'])
def test_failed_search_attempts_consume_user_limit(kind):
    step = ({'tool':'web_search','state':'failed','result':{'error':'网络失败'}} if kind == 'outer'
            else nested([event('failed-search','search',{'error':'网络失败'},'failed')]))
    chat = limited_chat([dict(step,turn_id='first')], limit=1)
    with pytest.raises(ValueError,match='搜索次数'): enforce(chat,'web_search')


def test_continuing_task_does_not_reset_search_limit():
    old = dict(nested([event('old-search','search',{'results':[]})]), turn_id='first')
    previous = {'tool':'web_search','state':'completed','turn_id':'second','result':{'results':[]}}
    chat = limited_chat([old,previous], ('first','second','third'))
    with pytest.raises(ValueError,match='搜索次数'): enforce(chat,'web_search')
    chat['turn_contexts']['third']['task_turn_ids'] = ['third']
    assert enforce(chat,'web_search')['search_limit'] == 2


def test_deep_followup_gather_respects_task_search_limit_and_retains_existing_body(chat, monkeypatch):
    text = '最多用1次搜索，阅读原文并生成报告'
    def set_limit(current):
        current['messages'][-1]['content'] = text
        current['turn_contexts'][current['turn_id']].update(user_text=text, original_request=text, rules=constraints(text))
    store.update_conversation(chat['id'], set_limit)
    chat = store.get_conversation(chat['id'])
    monkeypatch.setattr(deep.documents,'attached_documents',lambda _:[])
    monkeypatch.setattr(deep.disclosures,'fetch_annual',lambda *a:(_ for _ in ()).throw(ValueError('无年报')))
    searches = []
    def search(qs, **kwargs):
        searches.append(qs)
        return {'queries':qs,'results':[{'url':body()['url'],'snippet':'线索'}]}
    monkeypatch.setattr(deep.web_research,'search',search)
    monkeypatch.setattr(deep.web_research,'read',lambda *a,**kw:body())
    payloads = []
    def model(system,payload,*args,**kwargs):
        payloads.append(payload)
        if len(payloads) == 1: return {'queries':['华宇软件 财务'],'questions':[]},{'model':'stub'}
        if len(payloads) == 2: return {'queries':['华宇软件 后续风险'],'pdf_queries':[]},{'model':'stub'}
        if len(payloads) == 3:
            sid = deep.source_id(body()['url'])
            draft = {'title':'受限检索报告','summary':'依据已取得的正文讨论，保留未查证问题',
                     'stance':'继续跟踪','questions':['待取得新增资料'],'limitations':['搜索次数有限'],
                     'theses':[{'title':title,'body':'原文支持有限，需要继续验证', 'evidence_ids':[sid],
                        'status':'inference','counter_evidence':'尚缺独立证据','invalidate_if':'后续披露与当前材料冲突'}
                        for title in ('经营研究','现金研究')],
                     'sections':[{'key':key,'body':'根据已取得的原文分析，尚未形成确定判断',
                                  'evidence_ids':[sid],'status':'inference'} for key in deep.SECTION_TITLES],
                     'tracking_metrics':[{'id':'cash-followup','name':'现金回收','definition':'同口径经营现金流与应收回款',
                         'thesis':'现金研究','baseline':{'status':'missing','value':'已有正文不足以确认财务基线','period':''},
                         'frequency':'取得新增定期报告后','next_check':'下一次查阅财务披露时',
                         'support_condition':'原始回款证据支持经营改善','invalidate_condition':'新增披露与当前判断冲突',
                         'threshold_basis':'qualitative','next_action':'核查现金流附注','status':'needs_baseline'}]}
            draft['investment_recommendation'] = watch_recommendation()
            return draft, {'model':'stub'}
        return {'issues':[],'requires_revision':False}, {'model':'stub'}
    monkeypatch.setattr(deep,'model_json',model)
    result = deep.build(chat['run_id'],text,chat)
    assert result['artifact'] == 'report' and result['revision'] == 2
    assert len(searches) == 1
    assert any('搜索次数' in item for item in payloads[2]['search_limitations'])
    observed = coverage(store.get_conversation(chat['id'])['steps'],'阅读原文')
    assert observed['search_attempts'] == observed['body_sources'] == 1


def test_execute_allows_first_search_at_limit_one_then_rejects_second(chat, monkeypatch):
    from app import agent
    text = '最多1次搜索，请联网搜索公司资本开支'
    def prepare(current):
        current['messages'][-1]['content'] = text
        current['steps'] = []
        current['turn_contexts'][current['turn_id']].update(user_text=text,original_request=text,rules=constraints(text))
    store.update_conversation(chat['id'], prepare)
    decisions = []
    def model(*args,**kwargs):
        decisions.append(1)
        if len(decisions) <= 2:
            return {'action':'tool','tool':'web_search','arguments':{
                'queries':['华宇软件 资本开支' if len(decisions) == 1 else '华宇软件 并购支出']}}, {'model':'stub'}
        return {'action':'respond','message':'已完成一次搜索，第二次达到指定次数限制，保留当前检索结果。'}, {'model':'stub'}
    requests = []
    def search(qs,**kwargs):
        requests.append(qs)
        return {'queries':qs,'results':[{'url':'https://example.com/capex','snippet':'资本开支披露线索'}]}
    monkeypatch.setattr(agent,'model_json',model)
    monkeypatch.setattr(agent.web_research,'search',search)
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    searches = [s for s in saved['steps'] if s['tool'] == 'web_search']
    assert len(requests) == 1
    assert [(s['state'],s['search_dispatched']) for s in searches] == [('completed',True),('failed',False)]
    assert '搜索次数' in searches[1]['result']['error']
    observed = coverage(searches)
    assert observed['search_attempts'] == observed['search_calls'] == observed['search_results'] == 1
