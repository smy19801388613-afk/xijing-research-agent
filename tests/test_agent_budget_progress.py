"""Unlimited useful decisions must not permit repeated cached-page loops."""
import copy

import pytest

from app import agent, execution_budget as budgets, store


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    current=store.new_conversation()
    return store.begin_turn(current['id'],'请读取 PDF 第120页的原文。','stub','documents')


def page_decision(page=120):
    return {'action':'tool','tool':'read_document','arguments':{'document_id':'annual','page':page,'count':1}}


def test_repeated_cached_page_is_not_reintroduced_and_stops_without_progress(chat, monkeypatch):
    seen=[];reads=[]
    def model(system,payload,*args,**kwargs):
        seen.append(copy.deepcopy(payload))
        return page_decision(),{'model':'stub'}
    def read(*args,**kwargs):
        reads.append(1)
        return {'snippets':[{'document_id':'annual','page':120,'text':'资本开支原文已经取得。',
                             'url':'/api/documents/annual/file#page=120'}]}
    monkeypatch.setattr(agent,'model_json',model)
    monkeypatch.setattr(agent,'tool',read)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert len(reads)==1 and len(seen)==4
    assert saved['state']=='failed' and saved['stop_reason']=='no_progress'
    assert saved['messages'][-1]['delivery']['can_resume']
    original=saved['steps'][0]['id']
    assert all(s['reused_from_step_id']==original for s in saved['steps'][1:])
    assert len([r for r in seen[-1]['tool_results'] if r.get('tool')=='read_document'])==1
    assert any('已完成' in r.get('validation_feedback','') for r in seen[-1]['tool_results'])
    assert 'Token' in saved['messages'][-1]['content']


def test_new_pages_reset_repeat_detection(chat, monkeypatch):
    decisions=iter([page_decision(120),page_decision(120),page_decision(121),page_decision(121),
                    page_decision(122),page_decision(122),{'action':'respond','message':'不同页的原文均已取得。'}])
    reads=[]
    monkeypatch.setattr(agent,'model_json',lambda *a,**k:(next(decisions),{'model':'stub'}))
    def read(c,name,args,*rest):
        reads.append(args['page'])
        return {'snippets':[{'document_id':'annual','page':args['page'],'text':'原文已取得。'}]}
    monkeypatch.setattr(agent,'tool',read)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='idle' and reads==[120,121,122]
    assert not saved.get('stop_reason')


def test_cached_page_can_be_refocused_with_its_full_tail(chat, monkeypatch):
    seen=[];reads=[]
    decisions=iter([page_decision(120),page_decision(128),page_decision(120),
                    {'action':'respond','message':'两个页码的原文已对照。'}])
    def model(system,payload,*args,**kwargs):
        seen.append(copy.deepcopy(payload))
        return next(decisions),{'model':'stub'}
    def read(c,name,args,*rest):
        page=args['page'];reads.append(page)
        return {'snippets':[{'document_id':'annual','page':page,
                             'text':'原文内容。'*1000+f'PAGE_{page}_TAIL'}]}
    monkeypatch.setattr(agent,'model_json',model)
    monkeypatch.setattr(agent,'tool',read)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='idle' and reads==[120,128]
    observations=[r for r in seen[-1]['tool_results'] if r.get('tool')=='read_document']
    matching=[r for r in observations if r['result']['snippets'][0]['page']==120]
    assert len(matching)==1
    assert matching[0]['full_read_observation']
    assert matching[0]['result']['snippets'][0]['text'].endswith('PAGE_120_TAIL')


def test_different_cached_pages_can_be_compared_without_no_progress_stop(chat, monkeypatch):
    pages=[120,121,122,120,121,122,120,121,122]
    decisions=iter([*[page_decision(page) for page in pages],
                    {'action':'respond','message':'已完成三个页码的交叉核对。'}])
    reads=[]
    monkeypatch.setattr(agent,'model_json',lambda *a,**k:(next(decisions),{'model':'stub'}))
    def read(c,name,args,*rest):
        reads.append(args['page'])
        return {'snippets':[{'document_id':'annual','page':args['page'],'text':'该页原文。'}]}
    monkeypatch.setattr(agent,'tool',read)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='idle' and not saved.get('stop_reason')
    assert reads==[120,121,122] and len(saved['model_calls'])==10


def test_distinct_failed_pages_do_not_count_as_repeated_action(chat, monkeypatch):
    decisions=iter([page_decision(120),page_decision(121),page_decision(122),page_decision(123),
                    {'action':'respond','message':'这些页目前无法读取，已保留具体缺口。'}])
    monkeypatch.setattr(agent,'model_json',lambda *a,**k:(next(decisions),{'model':'stub'}))
    monkeypatch.setattr(agent,'tool',lambda *a,**k:{'error':'这一页无法读取'})
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='idle' and len(saved['steps'])==4
    assert not saved.get('stop_reason')


def test_shared_token_budget_stops_before_next_call_and_preserves_prior_result(chat, monkeypatch):
    store.update_conversation(chat['id'],lambda c:c['turn_contexts'][c['turn_id']].update(
        execution_budget={'total_tokens':50000,'time_minutes':15}))
    attempts=[]
    def model(*args,**kwargs):
        reservation=budgets.reserve(20000,5000)
        attempts.append(1)
        budgets.settle(reservation,{'prompt_tokens':25000,'completion_tokens':5000,'total_tokens':30000})
        return page_decision(),{'model':'stub'}
    monkeypatch.setattr(agent,'model_json',model)
    monkeypatch.setattr(agent,'tool',lambda *a,**k:{'snippets':[{'page':120,'text':'已经取得原文'}]})
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert attempts==[1] and len(saved['steps'])==1
    assert saved['state']=='failed' and saved['stop_reason']=='token_limit'
    assert saved['steps'][0]['state']=='completed'
    assert saved['budget_usage']['used_tokens']==30000
    assert saved['budget_exhausted']['usage']['remaining_tokens']==20000
    assert saved['messages'][-1]['delivery']['can_resume']


def test_nested_token_exhaustion_is_not_swallowed_as_tool_failure(chat, monkeypatch):
    monkeypatch.setattr(agent,'model_json',lambda *a,**k:(page_decision(),{'model':'stub'}))
    def read(*args,**kwargs):
        raise budgets.BudgetExhausted()
    monkeypatch.setattr(agent,'tool',read)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['stop_reason']=='token_limit' and len(saved['model_calls'])==1
    assert saved['steps'][0]['state']=='failed'


def test_unexpected_exception_finalizes_outstanding_token_reservation(chat, monkeypatch):
    def model(*args,**kwargs):
        budgets.reserve(1500,500)
        raise RuntimeError('audit storage failed after reservation')
    monkeypatch.setattr(agent,'model_json',model)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='failed'
    usage=saved['budget_usage']
    assert usage['reserved_tokens']==0 and usage['estimated_tokens']==2000
    assert usage['used_tokens']==2000 and usage['accounting_status']=='interrupted_estimate'
    assert saved['turn_contexts'][saved['turn_id']]['budget_usage']==usage

