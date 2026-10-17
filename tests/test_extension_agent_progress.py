"""Fresh analysis reads remain current without permitting no-progress loops."""
from copy import deepcopy
import pytest
from app import agent, store


@pytest.mark.parametrize('name', ['get_tracking_metrics', 'analyze_financial_periods'])
def test_unchanged_dynamic_read_stops_after_feedback_but_is_never_cached(tmp_path, monkeypatch, name):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    chat=store.new_conversation()
    store.begin_turn(chat['id'],'查看目前的分析。','stub','documents')
    calls=[]
    def read(*args):
        calls.append(1)
        return {'report_identity':{'revision':2,'memo_hash':'same'},'metrics':[]}
    monkeypatch.setattr(agent,'tool',read)
    monkeypatch.setattr(agent,'model_json',lambda *a,**k:({'action':'tool','tool':name,'arguments':{}},{'model':'stub'}))
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['stop_reason']=='no_progress'
    assert len(calls)==4
    assert all(not s.get('reused_from_step_id') for s in saved['steps'])


def test_changed_report_identity_is_new_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    chat=store.new_conversation()
    store.begin_turn(chat['id'],'查看目前的跟踪计划。','stub','documents')
    decisions=iter([*[{'action':'tool','tool':'get_tracking_metrics','arguments':{}} for _ in range(7)],
                    {'action':'respond','message':'已检查最新跟踪计划，未改报告。'}])
    versions=iter([1,1,2,2,3,3,4])
    monkeypatch.setattr(agent,'model_json',lambda *a,**k:(next(decisions),{'model':'stub'}))
    monkeypatch.setattr(agent,'tool',lambda *a:{'report_identity':{'revision':next(versions),'memo_hash':'current'},'metrics':[]})
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='idle' and not saved.get('stop_reason')
    assert len(saved['steps'])==7


@pytest.mark.parametrize('artifact', ['financial_period_records','financial_period_analysis','sensitivity','comparables','tracking_observation'])
def test_new_required_artifact_cannot_disappear_or_raise_keyerror_on_early_response(tmp_path, monkeypatch, artifact):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    chat=store.new_conversation()
    store.begin_turn(chat['id'],'完成已授权的分析并保存。','stub','documents')
    monkeypatch.setattr(agent.task_contract,'required_artifacts',lambda *a:{artifact})
    seen=[]
    def model(system,payload,*a,**k):
        seen.append(deepcopy(payload))
        return {'action':'respond','message':'任务完成。'},{'model':'stub'}
    monkeypatch.setattr(agent,'model_json',model)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='failed' and saved['stop_reason']=='no_progress'
    assert len(seen)==3
    assert agent.ARTIFACT_LABELS[artifact] in saved['messages'][-1]['delivery']['pending']
    assert 'KeyError' not in saved['messages'][-1]['content']
