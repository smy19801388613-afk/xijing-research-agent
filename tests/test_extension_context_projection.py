"""New tool outputs must remain usable through both agent compaction passes."""
from copy import deepcopy
import json

import pytest
from app import agent_context as ctx, store, financial_periods as periods, valuation, valuation_analysis
from app import research_tracking as tracking
from app.schemas import RunRequest


@pytest.fixture
def results(tmp_path, monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    run=store.create_run(RunRequest(ticker='600519.SH',year=2025,as_of='2026-09-28',mode='live',data_source='documents').model_dump(mode='json'))
    body='2025年度合并营业收入100万元。2024年度合并营业收入80万元。'
    run.update(state='completed_with_gaps',company='合成投影测试公司',research_sources=[{
        'id':'source-body','kind':'web','title':'合成公司年度披露','url':'https://example.com/annual',
        'text':body,'published_at':'2026-03-31','read_status':'body_read'}])
    store.save_run(run)
    records=[{'metric':'revenue','year':year,'period':'FY','period_type':'cumulative','value':str(value),
        'unit':'万元','currency':'CNY','scope':'consolidated','comparability_key':'reported','restatement':'original',
        'source_type':'existing','source_id':'source-body','quote':f'{year}年度合并营业收入{value}万元',
        'announced_date':'2026-03-31'} for year,value in ((2024,80),(2025,100))]
    financial=periods.save_records(run['id'],records)
    base=valuation.save_result(run['id'],'pb',{'book_equity_100m':100,'multiple':10})
    sensitivity=valuation_analysis.save_sensitivity(run['id'],{'base_scenario_id':base['id'],'axis_x':[90,110],'axis_y':[8,12]})
    comparables=valuation_analysis.save_comparables(run['id'],{'method':'pb','period_kind':'FY','period_label':'2025',
        'valuation_date':'2026-09-28','currency':'CNY','rows':[
            {'company':'甲公司','multiple':2,'multiple_basis':'合成输入，待核实','source_ids':['source-body']},
            {'company':'乙公司','multiple':3,'multiple_basis':'合成输入，待核实','source_ids':['source-body']} ]})
    metrics=[{'id':key,'name':'跟踪 '+key,'definition':'对应业务口径与财务期间，不混用总价值和股价',
        'thesis':'收入质量','baseline':{'status':'observed','value':'100万元','period':'FY2025',
            'source_id':'source-body','quote':'2025年度合并营业收入100万元'},
        'frequency':'定期披露后','next_check':'下次取得同口径披露时','support_condition':'同口径回款支持收入增长',
        'invalidate_condition':'收入增长而回款持续逾期','threshold_basis':'qualitative','threshold_confirmed':False,
        'next_action':'核对原文并记录独立观察','status':'monitoring'}
        for key in ('revenue','cash','receivables','margin','market_price')]
    store.update_run(run['id'],lambda r:r.update(memo={'title':'合成报告','summary':'收入质量待核实',
        'theses':[{'title':'收入质量'}],'tracking_metrics':metrics},report_revision=2))
    current=store.get_run(run['id'])
    for i in range(5):
        tracking.record_observation(run['id'],'market_price',{'report_identity':tracking.snapshot(current)['report_identity'],
            'source_id':'source-body','quote':'2025年度合并营业收入100万元',
            'observation':f'第{i}条观察：仍需核对回款。','assessment':'inconclusive'})
    observed=tracking.snapshot(store.get_run(run['id']))
    return {'get_tracking_metrics':observed,'record_tracking_observation':{**observed,'artifact':'tracking_observation'},
        'record_financial_periods':{**financial,'operation':'saved'},'analyze_financial_periods':{**financial,'operation':'analyzed'},
        'calculate_sensitivity':sensitivity,'save_comparables':comparables}


def twice(name,result,target=500):
    rows=[{'tool':name,'result':result}]
    return ctx.compact_tool_results(ctx.compact_tool_results(rows),max_chars=target)[0]['result']


@pytest.mark.parametrize('name',list(ctx.EXTENSION_GROUPS))
def test_every_new_tool_retains_nonempty_contract_through_repeated_compaction(results,name):
    original=deepcopy(results[name]);projected=twice(name,results[name])
    assert results[name]==original
    assert projected and not projected.get('context_omitted')
    assert projected.get('artifact')==original.get('artifact')
    assert projected['context_note']


@pytest.mark.parametrize('name',['get_tracking_metrics','record_tracking_observation'])
def test_tracking_retains_exact_identity_all_metrics_and_last_market_price(results,name):
    raw=results[name];projected=twice(name,raw)
    assert projected['report_identity']==raw['report_identity']
    assert [m['id'] for m in projected['metrics']]==[m['id'] for m in raw['metrics']]
    last=projected['metrics'][-1]
    assert last['id']=='market_price' and last['definition'] and last['support_condition'] and last['invalidate_condition']
    assert last['baseline']['source_id']=='source-body'
    assert last['baseline']['quote']=='2025年度合并营业收入100万元'
    assert projected['observation_count']==5 and len(projected['observations'])==3
    assert projected['observations'][-1]['id']==raw['observations'][-1]['id']
    assert projected['observations'][-1]['source_snapshot']['read_status']=='body_read'
    assert projected['observations'][-1]['report_identity']==raw['report_identity']


def test_tracking_inventory_is_not_lost_to_long_prose_or_soft_budget(results):
    raw=deepcopy(results['get_tracking_metrics'])
    for metric in raw['metrics']:
        metric['definition']='财务指标完整口径说明。'*300
        metric['support_condition']='支持条件与反向解释。'*300
        metric['invalidate_condition']='推翻条件与下一次验证。'*300
        metric['baseline']['quote']='原文连续摘录。'*300
    projected=twice('get_tracking_metrics',raw)
    assert len(projected['metrics'])==5 and projected['metrics'][-1]['id']=='market_price'
    assert projected['report_identity']==raw['report_identity']
    assert ctx.size(projected)<11000
    assert all(m['baseline']['quote_excerpt_only'] for m in projected['metrics'])
    assert all(m['baseline']['quote'] in raw['metrics'][i]['baseline']['quote'] for i,m in enumerate(projected['metrics']))


@pytest.mark.parametrize('name',['record_financial_periods','analyze_financial_periods'])
def test_period_projection_keeps_analysis_amounts_formulas_and_original_source_locators(results,name):
    raw=results[name];projected=twice(name,raw)
    first=ctx.compact_tool_results([{'tool':name,'result':raw}])[0]['result']
    assert projected==first  # The second pass keeps order, totals and row lineage.
    assert projected['operation']==raw['operation']
    assert projected['record_count']==len(raw['records'])
    assert {r['id'] for r in projected['records']}=={r['id'] for r in raw['records']}
    for row in projected['records']:
        original=next(r for r in raw['records'] if r['id']==row['id'])
        for key in ('value','unit','currency','scope','year','period','source_id','source_status','quote'):
            assert row[key]==original[key]
    actual={r['id']:r for r in raw['analysis']['comparisons']}
    for row in projected['analysis']['comparisons']:
        for key in ('current','previous','value','change','formula','reason','inputs','source_status'):
            assert row.get(key)==actual[row['id']].get(key)


def test_sensitivity_projection_keeps_units_axes_envelope_fixed_inputs_and_cells(results):
    raw=results['calculate_sensitivity'];projected=twice('calculate_sensitivity',raw)
    for key in ('analysis_kind','base_scenario_id','axes','envelope','fixed_assumptions','valid_cells','invalid_cells'):
        assert projected[key]==raw[key]
    assert 'price' not in projected['envelope']
    assert projected['cell_count']==4
    assert projected['cell_preview']==[cell for row in raw['cells'] for cell in row]
    assert 'base_snapshot' not in projected


def test_comparables_keep_statistics_exclusions_period_and_source_caveats(results):
    raw=results['save_comparables'];projected=twice('save_comparables',raw)
    assert projected['statistics']==raw['statistics']
    assert projected['period_kind']=='FY' and projected['period_label']=='2025'
    assert projected['valuation_date']=='2026-09-28'
    assert projected['row_count']==2
    assert projected['rows'][0]['source_ids']==['source-body']
    assert projected['rows'][0]['verification']=='user_provided'
    assert projected['rows'][0]['included'] is True
    assert any('不代表' in note for note in projected['notes'])


def test_final_payload_second_pass_keeps_tracking_after_feedback_and_failed_unread_page(results):
    raw=results['get_tracking_metrics']
    rows=[{'tool':'get_tracking_metrics','result':deepcopy(raw)} for _ in range(12)]
    rows += [{'tool':'read_public_pdf','result':{'status':'needs_ocr','url':'https://example.com/scan.pdf','results':[]}},
             {'validation_feedback':'不要重复读取指标，请修正最后的市场价格条件。'}]
    payload={'tool_results':rows,'conversation':[],'context':{'memo':{'summary':'旧稿'*8000}},
        'research_plan':{},'original_request':'只修正跟踪指标，其他报告内容不变。',
        'user_constraints':{'no_network':True},'available_tools':['get_tracking_metrics','refine_investment_memo']}
    projected=ctx.compact_payload(payload,max_chars=7000)
    kept=next(r for r in projected['tool_results'] if r.get('tool')=='get_tracking_metrics')
    assert kept['result']['report_identity']==raw['report_identity']
    assert kept['result']['metrics'][-1]['id']=='market_price'
    assert kept['repeated_observations']==12
    assert not kept.get('full_read_observation') and not kept.get('read_inventory')
    assert not any(r.get('full_read_observation') for r in projected['tool_results'])
    assert projected['tool_results'][-1]['validation_feedback'].startswith('不要重复读取')
    assert payload['tool_results'][0]['result']==raw


def test_newer_dynamic_state_replaces_only_working_projection_not_side_effect_ledger(results):
    old=deepcopy(results['record_tracking_observation'])
    current=deepcopy(results['get_tracking_metrics'])
    current['metrics'][-1]['next_action']='最新检查动作'
    rows=[{'tool':'record_tracking_observation','result':old},
          {'tool':'read_webpage','result':{'error':'原文不可访问'}},
          {'tool':'get_tracking_metrics','result':current}]
    projected=ctx.compact_tool_results(ctx.compact_tool_results(rows),max_chars=500)
    latest=next(r for r in projected if r.get('tool')=='get_tracking_metrics')
    assert latest['result']['metrics'][-1]['next_action']=='最新检查动作'
    saved=next(r for r in projected if r.get('tool')=='record_tracking_observation')
    assert saved['result']['artifact']=='tracking_observation'
    assert latest['structured_analysis_observation']
    assert not saved.get('structured_analysis_observation')


def test_projection_of_extension_error_never_fabricates_success_fields():
    for tool in ctx.EXTENSION_GROUPS:
        result=twice(tool,{'error':'需要有效来源，未执行保存'})
        assert result['error']=='需要有效来源，未执行保存'
        assert not result.get('metrics') and not result.get('artifact')
