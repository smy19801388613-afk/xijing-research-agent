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


@pytest.mark.parametrize('total',[150000,500000,1000000,5000000])
def test_thinking_and_retry_obey_protocol_and_cumulative_ceiling(local_model,total):
    _,calls=local_model
    with budgets.scope(budgets.DEFAULTS|{'total_tokens':total}):
        result,meta=providers.model_json('report',{},7800,reasoning=True)
    assert result=={'ok':True} and len(calls)==2
    assert all(c['json']['max_tokens']<=65536 for c in calls)
    assert calls[-1]['json']['max_tokens']==65536
    assert meta['execution_generation_cap']==65536
    assert meta['execution_token_usage']['used_tokens']<=total
    assert meta['execution_token_usage']['request_count']==2
    assert budgets.current() is None


@pytest.mark.parametrize('provider,protocol,field',[
    ('openai','openai','max_completion_tokens'),('qwen','openai','max_tokens'),
    ('anthropic','anthropic','max_tokens')])
def test_all_protocols_bound_initial_and_recovery_generation(local_model,provider,protocol,field):
    config,calls=local_model
    config.update(provider=provider,protocol=protocol)
    with budgets.scope(budgets.DEFAULTS):
        providers.model_json('report',{},7800)
    assert [c['json'][field] for c in calls]==[7800,15600]


@pytest.mark.parametrize('provider,protocol,field',[
    ('openai','openai','max_completion_tokens'),('qwen','openai','max_tokens'),
    ('anthropic','anthropic','max_tokens')])
def test_non_deepseek_long_reports_can_recover_up_to_increased_ceiling(local_model,provider,protocol,field):
    config,calls=local_model
    config.update(provider=provider,protocol=protocol)
    with budgets.scope(budgets.DEFAULTS):
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


def test_useful_steps_can_continue_past_old_decision_limit(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    chat=store.new_conversation()
    store.begin_turn(chat['id'],'逐页读取 PDF 第1页至第35页。','fixture','documents',execution_budget=budgets.DEFAULTS)
    observed=[]
    def model(system,payload,*args,**kw):
        observed.append((system,payload,budgets.current()))
        if len(observed)<=35:
            return {'action':'tool','tool':'read_document','arguments':{'document_id':'annual','page':len(observed),'count':1}},{'model':'fixture'}
        return {'action':'respond','message':'不同页的原文已读取并保留。'},{'model':'fixture'}
    monkeypatch.setattr(agent,'model_json',model)
    monkeypatch.setattr(agent,'tool',lambda c,name,args,*rest:{'snippets':[{'document_id':'annual','page':args['page'],'text':'该页的独立原文。'}]})
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert len(observed)==36 and len(saved['steps'])==35
    assert all('decisions_remaining' not in item[1] for item in observed)
    assert all('max_decisions' not in item[2] for item in observed)
    assert all('最后一次决策' not in item[0] for item in observed)
    assert saved['state']=='idle'
    assert saved['budget_usage']['decisions_used']==36
    assert 'max_decisions' not in saved['budget_usage']


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
