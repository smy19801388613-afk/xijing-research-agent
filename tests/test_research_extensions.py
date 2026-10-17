"""Integration boundaries: agent permissions, freshness, citations and delivery."""
from copy import deepcopy
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from app import agent, store, valuation, financial_periods as fp, valuation_analysis as va
from app import research_extensions as ext, report_state, evidence_policy, task_contract, exporting
from app.schemas import RunRequest


@pytest.fixture
def run(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    value=store.create_run(RunRequest(ticker='600519.SH',year=2025,as_of='2026-09-28',mode='live',data_source='documents').model_dump(mode='json'))
    value.update(company='合成验收公司',state='completed_with_gaps',facts=[
        {'id':'profit','metric':'n_income_attr_p','label':'归母净利润','year':2025,'value':'100000000','status':'api_only','evidence_ids':['income'],'announced_date':'2026-03-31'}],
        evidence=[{'id':'income','kind':'api','title':'合成接口','locator':'测试记录','quote':'归母净利润100000000','announced_date':'2026-03-31'}],
        research_sources=[{'id':'body','kind':'web','title':'合成财报','text':'2025年度合并营业收入100万元。2024年度合并营业收入80万元。','published_at':'2026-03-31','read_status':'body_read'}])
    store.save_run(value)
    return value


def record(**extra):
    return {'metric':'revenue','year':2025,'period':'FY','period_type':'cumulative','value':'100','unit':'万元','currency':'CNY',
        'scope':'consolidated','comparability_key':'reported','restatement':'original','source_type':'existing','source_id':'body',
        'quote':'2025年度合并营业收入100万元','announced_date':'2026-03-31',**extra}


def chat(run,text):
    return {'run_id':run['id'],'messages':[{'role':'user','content':text}]}


def test_original_records_and_derived_citations_do_not_recurse_or_promote(run):
    fp.save_records(run['id'],[record()],origin='model')
    saved=store.get_run(run['id']); sources=ext.sources(saved)
    source=next(iter(sources.values()))
    policy=evidence_policy.eligibility(source,saved)
    assert policy['supports_inference'] and not policy['supports_claim'] and not policy['supports_answer']
    assert ext.context(saved)['financial_periods']['records'][0]['source_status']=='source_matched'
    saved['research_sources'][0]['text']='来源内容已经改变'
    changed=next(iter(ext.sources(saved).values()))
    assert changed['status']=='conflict'
    assert not evidence_policy.eligibility(changed,saved)['supports_inference']


def test_new_analysis_changes_report_dependencies_without_rewriting_report(run):
    run['memo']={'title':'原报告','summary':'原判断'};store.save_run(run)
    before=report_state.dependency_fingerprint(run)
    fp.save_records(run['id'],[record()],origin='model')
    after=store.get_run(run['id'])
    assert before!=report_state.dependency_fingerprint(after)
    assert run['memo']==after['memo'] and 'tracking_metrics' not in after['memo']
    assert 'research_extensions' not in report_state.dependency_snapshot(run)
    previous=report_state.dependency_fingerprint(after)
    after['research_sources'][0]['text']='原来源被更正，已有期间输入需要重验'
    assert report_state.dependency_fingerprint(after)!=previous


def test_agent_sensitivity_requires_human_axis_numbers_and_retains_scenario(run):
    base=valuation.save_result(run['id'],'pe',{'growth_pct':'0','multiple':'10'})
    args={'base_scenario_id':base['id'],'axis_x':[0,10],'axis_y':[10,20]}
    human=chat(run,'请计算估值敏感性，增长0和10，倍数10和20。')
    with pytest.raises(ValueError,match='原话'):agent.tool(human,'calculate_sensitivity',args)
    args['axis_quotes']={'x':'增长0和10','y':'倍数10和20'}
    result=agent.tool(human,'calculate_sensitivity',args)
    assert result['valid_cells']==4
    assert len(store.get_run(run['id'])['valuation_history'])==1
    assert agent.completed_artifacts([{'tool':'calculate_sensitivity','result':result}]) >= {'sensitivity'}
    assert task_contract.requested_artifacts('请直接计算并保存估值敏感性')=={'sensitivity'}


@pytest.mark.parametrize('text,name,args',[
    ('不要计算','analyze_financial_periods',{}),
    ('不要计算','calculate_sensitivity',{}),
    ('不要估值','save_comparables',{}),
    ('不要修改研究问题','record_tracking_observation',{}),
    ('不要接入材料','record_financial_periods',{'records':[]}),
])
def test_new_agent_tools_honor_existing_permissions(run,text,name,args):
    with pytest.raises(ValueError):agent.tool(chat(run,text),name,args)


def test_no_per_share_applies_to_existing_sensitivity_base(run):
    base=valuation.save_result(run['id'],'pe',{'growth_pct':'0','multiple':'10','shares_100m':'2'})
    with pytest.raises(ValueError,match='每股'):
        agent.tool(chat(run,'不要计算每股价格，请算敏感性'),'calculate_sensitivity',{'base_scenario_id':base['id']})


def test_model_cannot_invent_peer_numbers_or_user_inputs(run):
    with pytest.raises(ValueError,match='模型只能'):
        agent.tool(chat(run,'登记财务期间'),'record_financial_periods',{'records':[record(source_type='user_input')]})
    with pytest.raises(ValueError,match='真实来源'):
        agent.tool(chat(run,'保存可比公司依据'),'save_comparables',{'rows':[{'company':'示例甲','multiple':10}]})


def test_period_write_ban_blocks_valid_input_and_analysis_cannot_fake_save(run):
    for text in ('不要接入材料','不要保存财务期间记录，只在对话里给我看看结果'):
        with pytest.raises(ValueError,match='材料'):
            agent.tool(chat(run,text),'record_financial_periods',{'records':[record()]})
        assert not store.get_run(run['id']).get('financial_period_records')
    result=agent.tool(chat(run,'分析财务期间'),'analyze_financial_periods',{})
    assert 'financial_period_records' not in agent.completed_artifacts([{'result':result}])
    assert task_contract.requested_artifacts('请保存财务期间记录')=={'financial_period_records'}


def test_sensitivity_quote_cannot_omit_human_negation(run):
    base=valuation.save_result(run['id'],'pe',{'growth_pct':'0','multiple':'10'})
    human=chat(run,'请计算敏感性，不要用增长0和10，改用增长5和15，倍数10和20。')
    with pytest.raises(ValueError,match='否定'):
        agent.tool(human,'calculate_sensitivity',{'base_scenario_id':base['id'],'axis_x':[0,10],'axis_y':[10,20],
            'axis_quotes':{'x':'增长0和10','y':'倍数10和20'}})
    assert not store.get_run(run['id']).get('valuation_sensitivity_history')


def test_real_app_routes_and_bundle_include_sidecars_without_mutating_history(run,tmp_path):
    from app.main import app
    client=TestClient(app)
    response=client.post(f"/api/runs/{run['id']}/financial-periods",json={'records':[record()]})
    assert response.status_code==200,response.text
    base=valuation.save_result(run['id'],'pe',{'growth_pct':'0','multiple':'10'})
    response=client.post(f"/api/runs/{run['id']}/valuation-sensitivities",json={'base_scenario_id':base['id'],'axis_x':[0,10],'axis_y':[10,20]})
    assert response.status_code==200,response.text
    assert client.get(f"/api/runs/{run['id']}/valuation-analysis").json()['sensitivities'][0]['valid_cells']==4
    assert client.get(f"/api/runs/{run['id']}/tracking").json()['metrics']==[]
    saved=store.get_run(run['id']);before=deepcopy(saved)
    md=exporting.markdown(saved);html=exporting.printable(saved)
    assert '财务期间工作底稿' in md and '敏感性' in html
    bundle=exporting.evidence_bundle(saved,store.get_document,tmp_path/'empty')
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert 'analysis/current-inputs.json' in archive.namelist()
        assert 'analysis/valuation_sensitivity_history.json' in archive.namelist()
        manifest=json.loads(archive.read('manifest.json'))
        assert any(r['path']=='analysis/current-inputs.json' for r in manifest['files'])
    assert saved==before and store.get_run(run['id'])==before


@pytest.mark.parametrize('name',[
    'record_financial_periods','analyze_financial_periods','calculate_sensitivity',
    'save_comparables','get_tracking_metrics','record_tracking_observation',
])
def test_dynamic_analysis_and_tracking_tools_never_reuse_completed_step_cache(name):
    # A prior successful result cannot stand in for current evidence/state or
    # make another requested write appear completed without executing it.
    assert agent.cacheable_step({'tool':name,'state':'completed','result':{'status':'ok','id':'saved'}}) is False


@pytest.mark.parametrize('name',['build_investment_memo','refine_investment_memo'])
def test_failed_report_attempt_rewording_does_not_reset_same_failure_fingerprint(name):
    before=agent.failed_action_fingerprint(name,{'question':'完成原报告，保留证据缺口'})
    after=agent.failed_action_fingerprint(name,{'question':'换一种措辞后再次完成原报告'})
    assert before==after
    other='refine_investment_memo' if name=='build_investment_memo' else 'build_investment_memo'
    assert before!=agent.failed_action_fingerprint(other,{'question':'完成原报告，保留证据缺口'})


def tracking_input(run):
    value=deepcopy(run)
    value['memo']={'title':'原始报告','summary':'收入增长的回款质量需要跟踪','theses':[{'title':'收入质量'}],
        'tracking_metrics':[{'id':'revenue-followup','name':'收入质量','definition':'年度合并营业收入与回款的关系',
            'thesis':'收入质量','baseline':{'status':'missing','value':'尚缺同口径回款数据','period':'2025年度','source_id':None,'quote':''},
            'frequency':'定期披露后','next_check':'下次披露回款明细时','support_condition':'收入和现金回收一致',
            'invalidate_condition':'收入增长而应收持续逾期','threshold_basis':'qualitative','threshold_confirmed':False,
            'next_action':'核查附注并讨论对原论点的影响','status':'needs_baseline'}]}
    store.save_run(value)
    args={'metric_id':'revenue-followup','report_identity':report_state.report_identity(value),'source_id':'body',
        'quote':'2025年度合并营业收入100万元','observation':'原文提供收入基数，但仍没有回款明细，暂不改变原论点。',
        'assessment':'inconclusive'}
    return value,args


def test_saved_observation_satisfies_task_postcondition_without_claiming_report_saved(run):
    before,args=tracking_input(run)
    conversation=store.new_conversation(run['id'])
    conversation=store.begin_turn(conversation['id'],'请保存跟踪观察。不要修改报告。','stub','documents')
    assert task_contract.required_artifacts(conversation)=={'tracking_observation'}
    read=agent.tool(conversation,'get_tracking_metrics',{})
    assert 'tracking_observation' not in agent.completed_artifacts([{'tool':'get_tracking_metrics','result':read}])
    result=agent.tool(conversation,'record_tracking_observation',args)
    assert result['artifact']=='tracking_observation'
    assert agent.completed_artifacts([{'tool':'record_tracking_observation','result':result}])=={'tracking_observation'}
    def completed(current):
        current['steps'].append({'id':'saved-observation','turn_id':current['turn_id'],
            'tool':'record_tracking_observation','state':'completed','arguments':args,'result':result})
    conversation=store.update_conversation(conversation['id'],completed)
    saved=store.get_run(run['id'])
    delivery=agent.delivery_state(conversation,saved)
    assert delivery['status']=='completed' and delivery['pending']==[]
    assert delivery['report_changed'] is False and delivery['report_note']=='本轮未保存新的报告版本'
    assert saved['memo']==before['memo'] and saved['report_revision']==before['report_revision']
    assert len(saved['tracking_observations'])==1
    assert saved['tracking_observations'][0]['origin']=='model'


@pytest.mark.parametrize('text',[
    '不要保存跟踪观察，只讨论已有资料。',
    '不要追加观察记录，只查看已有跟踪指标。',
])
def test_observation_write_ban_blocks_valid_payload_before_any_persistence(run,text):
    before,args=tracking_input(run)
    conversation=chat(run,text)
    assert 'tracking_observation' not in task_contract.required_artifacts(conversation)
    with pytest.raises(ValueError,match='禁止修改研究问题'):
        agent.tool(conversation,'record_tracking_observation',args)
    assert store.get_run(run['id'])==before
