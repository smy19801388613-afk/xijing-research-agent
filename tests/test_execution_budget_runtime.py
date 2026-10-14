"""Chosen limits must reach the actual provider and agent execution paths."""
import json
import time

import httpx
import pytest

from app import agent, connections, deep_research, execution_budget as budgets, providers, store


@pytest.fixture
def local_model(monkeypatch):
    config={'provider':'deepseek','protocol':'openai','key':'fixture',
            'base':'https://model.example','id':'fixture','model':'fixture','json_mode':True}
    monkeypatch.setattr(connections,'resolve_model',lambda _:config)
    monkeypatch.setattr(providers.competition,'check_network',lambda *a:None)
    calls=[]
    def post(url,**kw):
        calls.append(kw)
        body={'choices':[{'finish_reason':'length' if len(calls)==1 else 'stop',
                         'message':{'content':'{"ok":true}'}}]}
        if config['protocol']=='anthropic':
            body={'stop_reason':'max_tokens' if len(calls)==1 else 'end_turn',
                  'content':[{'type':'text','text':'{"ok":true}'}]}
        return httpx.Response(200,json=body,request=httpx.Request('POST',url))
    monkeypatch.setattr(providers.httpx,'post',post)
    return config,calls


@pytest.mark.parametrize('cap',[4096,8192,49152,65536])
def test_thinking_and_retry_obey_selected_generation_ceiling(local_model,cap):
    _,calls=local_model
    with budgets.scope(budgets.DEFAULTS|{'generation_tokens':cap}):
        result,meta=providers.model_json('report',{},7800,reasoning=True)
    assert result=={'ok':True} and len(calls)==2
    assert all(c['json']['max_tokens']<=cap for c in calls)
    assert calls[-1]['json']['max_tokens']==cap
    assert meta['execution_generation_cap']==cap
    assert budgets.current() is None


@pytest.mark.parametrize('provider,protocol,field',[
    ('openai','openai','max_completion_tokens'),('qwen','openai','max_tokens'),
    ('anthropic','anthropic','max_tokens')])
def test_all_protocols_bound_initial_and_recovery_generation(local_model,provider,protocol,field):
    config,calls=local_model
    config.update(provider=provider,protocol=protocol)
    with budgets.scope(budgets.DEFAULTS|{'generation_tokens':4096}):
        providers.model_json('report',{},7800)
    assert [c['json'][field] for c in calls]==[4096,4096]


@pytest.mark.parametrize('provider,protocol,field',[
    ('openai','openai','max_completion_tokens'),('qwen','openai','max_tokens'),
    ('anthropic','anthropic','max_tokens')])
def test_non_deepseek_long_reports_can_recover_up_to_increased_ceiling(local_model,provider,protocol,field):
    config,calls=local_model
    config.update(provider=provider,protocol=protocol)
    with budgets.scope(budgets.DEFAULTS|{'generation_tokens':65536}):
        providers.model_json('report',{},7800,reasoning=True)
    assert [c['json'][field] for c in calls]==[7800,65536]


@pytest.mark.parametrize('explicit_seconds',[None,0.25,1000])
def test_model_timeout_uses_shortest_remaining_deadline(local_model,explicit_seconds):
    _,calls=local_model
    now=time.monotonic()
    explicit=now+explicit_seconds if explicit_seconds is not None else None
    with budgets.scope(budgets.DEFAULTS,deadline_at=now+2):
        providers.model_json('test',{},deadline=explicit)
    assert all(0<c['timeout']<=min(2,explicit_seconds or 2) for c in calls)


@pytest.mark.parametrize('count',[2,10])
def test_decision_limit_and_final_turn_prompt_use_selected_value(tmp_path,monkeypatch,count):
    monkeypatch.setattr(store,'DATA',tmp_path)
    chat=store.new_conversation()
    store.begin_turn(chat['id'],'不要用任何工具，讨论研究方法','fixture','documents',
                     execution_budget=budgets.DEFAULTS|{'max_decisions':count})
    observed=[]
    def model(system,payload,*args,**kw):
        observed.append((system,payload,budgets.current()))
        return {'action':'invalid'}, {'model':'fixture'}
    monkeypatch.setattr(agent,'model_json',model)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert len(observed)==count
    assert [item[1]['decisions_remaining'] for item in observed]==list(range(count,0,-1))
    assert all(item[2]['max_decisions']==count for item in observed)
    assert '最后一次决策' in observed[-1][0]
    assert all('最后一次决策' not in item[0] for item in observed[:-1])
    assert saved['state']=='failed'
    assert f'{count} 次模型决策上限' in saved['messages'][-1]['content']
    assert saved['budget_usage']=={'decisions_used':count,'max_decisions':count}
    assert saved['messages'][-1]['delivery']['can_resume']


@pytest.mark.parametrize('seconds',[20,1200])
def test_deep_report_inherits_turn_deadline_instead_of_hidden_ten_minutes(tmp_path,monkeypatch,seconds):
    monkeypatch.setattr(store,'DATA',tmp_path)
    run=store.create_run({'ticker':'601899.SH','year':2025,'as_of':'2026-10-14','question':'研究经营'})
    run.update(state='failed',research_sources=[{'id':'web-1','kind':'web','title':'经营披露',
        'url':'https://example.com','text':'原始经营资料','published_at':'2026-01-01','read_status':'body_read'}])
    store.save_run(run)
    chat=store.new_conversation(run['id'])
    deadline=time.monotonic()+seconds
    observed=[]
    def model(*args,**kw):
        observed.append(kw['deadline'])
        raise providers.ProviderError('fixture stops before any paid call')
    monkeypatch.setattr(deep_research,'model_json',model)
    with budgets.scope(budgets.DEFAULTS,deadline_at=deadline):
        for fn in (deep_research.build,deep_research.refine):
            with pytest.raises(providers.ProviderError,match='fixture'):
                fn(run['id'],'研究经营',chat)
    assert observed==[deadline,deadline]
