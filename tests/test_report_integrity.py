"""Report publication races and shared freshness/cache dependency semantics."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from app import store, deep_research as deep, questions, agent
from app.report_state import report_identity


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    value = store.create_run({'ticker':'600001.SH','year':2025,'as_of':'2026-10-16'})
    value.update(state='completed', company='测试公司', research_sources=[{
        'id':'official','kind':'web','title':'公司公告','url':'https://example.com/disclosure',
        'text':'产品已交付但回款仍待跟踪。','published_at':'2026-09-01','read_status':'body_read'}])
    store.save_run(value)
    return value


def draft(run, summary='交付尚不等于现金回收'):
    return deep.validate_memo({'title':'研究报告','summary':summary,'stance':'继续查证',
        'theses':[{'title':title,'body':'交付仍需跟踪回款','counter_evidence':'尚未披露实际到账',
                   'invalidate_if':'取得回款证据','evidence_ids':['official'],'status':'inference'}
                  for title in ('业务进展','现金回收')],
        'sections':[{'key':key,'body':'仍需原始资料进一步验证。','status':'inference','evidence_ids':['official']}
                    for key in deep.SECTION_TITLES], 'questions':['核对实际回款'],'limitations':['缺少明细']},run)


def publish(run):
    deep.save_memo(run['id'],draft(run),'初稿')
    return store.get_run(run['id'])


def test_two_validated_drafts_cannot_overwrite_each_other(run):
    first, second = draft(run,'研究员甲的修改'), draft(run,'研究员乙的修改')
    deep.save_memo(run['id'],first,'甲保存')
    before=store.get_run(run['id'])
    with pytest.raises(ValueError, match='其他修订'):
        deep.save_memo(run['id'],second,'乙保存')
    assert store.get_run(run['id'])==before


def test_atomic_concurrent_publication_has_exactly_one_winner(run):
    barrier=threading.Barrier(2)
    candidates=[draft(run,'甲的观点'),draft(run,'乙的观点')]
    def save(candidate):
        barrier.wait(timeout=5)
        try: return deep.save_memo(run['id'],candidate,'并发保存')['revision']
        except ValueError: return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(save,candidates))
    assert sorted(str(x) for x in results)==['2','conflict']
    assert len(store.get_run(run['id'])['memo_revisions'])==1


def test_same_revision_content_change_also_rejects_stale_draft(run):
    current=publish(run); candidate=draft(current)
    store.update_run(run['id'],lambda r:r['memo'].update(summary='另一个写入者的新结论'))
    with pytest.raises(ValueError,match='其他修订'):
        deep.save_memo(run['id'],candidate,'旧稿')
    assert store.get_run(run['id'])['memo']['summary']=='另一个写入者的新结论'


def test_unrelated_news_does_not_expire_report_or_cache(run):
    current=publish(run); before=agent.report_input_fingerprint(current)
    store.update_run(run['id'],lambda r:r['research_sources'].append({
        'id':'unused','kind':'web','url':'https://example.com/other','text':'无关消息',
        'published_at':'2026-09-02','read_status':'body_read'}))
    after=store.get_run(run['id'])
    assert questions.report_sync(after)['status']=='current'
    assert agent.report_input_fingerprint(after)==before


@pytest.mark.parametrize('kind',['source','fact','calculation','question','valuation'])
def test_shared_freshness_and_cache_track_meaningful_inputs(run,kind):
    current=publish(run); before=agent.report_input_fingerprint(current)
    def change(r):
        if kind=='source': r['research_sources'][0]['text']='产品未交付。'
        if kind=='fact': r['facts'].append({'id':'new','label':'营业收入','metric':'revenue','year':2025,'value':'100','status':'api_only'})
        if kind=='calculation': r['calculations'].append({'id':'calculation','value':'25','inputs':[]})
        if kind=='question': r['research_questions'][0]['question']='实际何时收到现金？'
        if kind=='valuation': r['valuation_history'].append({'id':'v','method':'pb','method_name':'PB','assumptions':{'multiple':'1.2'},'output':{'equity_value_100m':'12'}})
    store.update_run(run['id'],change)
    after=store.get_run(run['id'])
    assert questions.report_sync(after)['needs_update']
    assert agent.report_input_fingerprint(after)!=before


def test_equivalent_scenario_id_and_numeric_format_do_not_expire_dependencies(run):
    scenario={'id':'v1','method':'pb','method_name':'PB','assumptions':{'multiple':'1.20'},'output':{'equity_value_100m':'12'}}
    store.update_run(run['id'],lambda r:r['valuation_history'].append(scenario))
    current=publish(store.get_run(run['id'])); before=agent.report_input_fingerprint(current)
    duplicate=deepcopy(scenario);duplicate.update(id='v2',created_at=store.now(),assumptions={'multiple':'1.2'})
    store.update_run(run['id'],lambda r:r['valuation_history'].append(duplicate))
    after=store.get_run(run['id'])
    assert not questions.report_sync(after)['needs_update']
    assert agent.report_input_fingerprint(after)==before


def test_saved_step_requires_current_published_output_identity(run):
    current=publish(run); chat=store.new_conversation(run['id'])
    chat=store.begin_turn(chat['id'],'修订并保存报告','stub','documents')
    step={'id':'saved','turn_id':chat['turn_id'],'tool':'refine_investment_memo','state':'completed',
        'result':{'artifact':'report','revision':current['report_revision']},
        'report_input_fingerprint':agent.report_input_fingerprint(current),
        'report_request_fingerprint':agent.report_request_fingerprint(chat),
        'report_output_identity':report_identity(current)}
    chat['steps'].append(step)
    assert agent.report_step_for_input(chat,current)
    current['memo']['summary']='同一版本下被外部修改'
    assert agent.report_step_for_input(chat,current) is None
    current=store.get_run(run['id']);current['report_revision']+=1
    assert agent.report_step_for_input(chat,current) is None


def test_direct_revision_uses_frozen_model_input_report(run):
    initial=publish(run)
    chat=store.new_conversation(run['id'])
    chat=store.begin_turn(chat['id'],'完整重写报告','stub','documents')
    proposed=draft(initial,'模型根据旧版写的观点')
    deep.save_memo(run['id'],draft(initial,'另一个对话的新版观点'),'他人更新')
    with pytest.raises(ValueError,match='其他修订'):
        agent.tool(chat,'revise_report',{'memo':proposed},model_input_run=initial)
    assert store.get_run(run['id'])['memo']['summary']=='另一个对话的新版观点'
