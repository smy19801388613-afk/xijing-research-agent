"""Financial semantics, provenance boundaries and immutable analysis history."""
from copy import deepcopy
from decimal import Decimal
import json

import pytest
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import store,valuation,valuation_analysis as analysis
from app.schemas import RunRequest


@pytest.fixture
def run(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    value=store.create_run(RunRequest(mode='live',ticker='600519.SH',year=2025,as_of='2026-09-28',data_source='documents').model_dump(mode='json'))
    value.update(state='completed_with_gaps',facts=[
        {'id':'profit','metric':'n_income_attr_p','year':2025,'value':'100000000','status':'api_only','label':'归母净利润','evidence_ids':['income'],'announced_date':'2026-03-31'},
        {'id':'revenue','metric':'revenue','year':2025,'value':'500000000','status':'api_only','label':'营业收入','evidence_ids':['income'],'announced_date':'2026-03-31'}],
        evidence=[{'id':'income','kind':'api','label':'已保存接口记录','announced_date':'2026-03-31','value':'original'}],
        research_sources=[{'id':'web-peer','kind':'web','title':'同业披露','url':'https://example.org/disclosure','published_at':'2026-04-01','text':'A company disclosure'}])
    store.save_run(value)
    return value


def pe(run): return valuation.save_result(run['id'],'pe',{'growth_pct':10,'multiple':15,'shares_100m':2})


def grid(base,**kwargs): return {'base_scenario_id':base['id'],'axis_x':[0,10],'axis_y':[10,20],**kwargs}


def peers(**kwargs):
    return {'method':'pe','period_kind':'FY','period_label':'2025','valuation_date':'2026-09-28','currency':'CNY',
        'rows':[{'company':'同业甲','multiple':'10','multiple_basis':'年报 EPS 与估值日收盘价'},
                {'company':'同业乙','multiple':'20','multiple_basis':'用户整理，待核实'},
                {'company':'同业丙','multiple':'30','multiple_basis':'用户整理，待核实'}],**kwargs}


def test_pe_decimal_matrix_reuses_core_and_keeps_other_inputs_fixed(run):
    base=pe(run);before=deepcopy(store.get_run(run['id'])['valuation_history'])
    result=analysis.save_sensitivity(run['id'],grid(base))
    assert result['envelope']['equity_value_100m']=={'min':'10','max':'22'}
    assert result['envelope']['price']=={'min':'5','max':'11'}
    assert result['fixed_assumptions']=={'shares_100m':'2'}
    assert result['valid_cells']==4 and result['invalid_cells']==0
    assert result['artifact']=='valuation_analysis' and result['revision']==1
    assert store.get_run(run['id'])['valuation_history']==before
    assert '不是概率置信区间' in result['notes'][0]


@pytest.mark.parametrize('method,inputs,xs,ys',[
    ('ps',{'growth_pct':10,'multiple':3},[-5,5],[2,3]),
    ('pb',{'book_equity_100m':100,'multiple':1},[90,110],[.8,1.2]),
    ('ev_ebitda',{'ebitda_100m':10,'multiple':5,'equity_bridge_100m':2},[8,12],[4,6]),
    ('ddm',{'dividend_per_share':1,'cost_of_equity_pct':8,'terminal_growth_pct':2},[7,9],[1,3]),
])
def test_other_methods_match_existing_decimal_calculator(run,method,inputs,xs,ys):
    base=valuation.save_result(run['id'],method,inputs)
    current=store.get_run(run['id']);result=analysis.calculate_sensitivity(current,grid(base,axis_x=xs,axis_y=ys))
    for row in result['cells']:
        for cell in row:
            expected=valuation.calculate(current,method,{**base['assumptions'],result['axes']['x']['key']:cell['x'],result['axes']['y']['key']:cell['y']})
            assert cell['output']==expected['output']


def test_dcf_excludes_invalid_wacc_growth_combinations_and_no_equity_without_bridge(run):
    base=valuation.save_result(run['id'],'dcf',{'base_fcff_100m':10,'growth_pct':5,'years':3,'wacc_pct':10,'terminal_growth_pct':2})
    result=analysis.calculate_sensitivity(store.get_run(run['id']),grid(base,axis_x=[5,10],axis_y=[5,8]))
    assert result['valid_cells']==2 and result['invalid_cells']==2
    assert result['cells'][0][0]['error']=='WACC 必须大于永续增长率'
    assert set(result['envelope'])=={'enterprise_value_100m'}
    vals=[Decimal(row[1]['output']['enterprise_value_100m']) for row in result['cells']]
    assert Decimal(result['envelope']['enterprise_value_100m']['min'])==min(vals)


def test_all_invalid_grid_keeps_errors_without_fabricating_range(run):
    base=valuation.save_result(run['id'],'ddm',{'dividend_per_share':1,'cost_of_equity_pct':8,'terminal_growth_pct':2})
    result=analysis.save_sensitivity(run['id'],grid(base,axis_x=[1,2],axis_y=[3,4]))
    assert result['valid_cells']==0 and result['envelope']=={}
    assert result['invalid_cells']==4


@pytest.mark.parametrize('values',[[1],[*range(10)],[1,'1.00'],[1,'NaN'],[1,'Infinity'],[1,True],[1,'1e15'],[1,'1e-999999999'],[1,'0.'+'0'*100+'1']])
def test_axis_bounds_finite_values_uniqueness_and_boolean_rejection(run,values):
    base=pe(run)
    with pytest.raises((ValueError,ValidationError)): analysis.save_sensitivity(run['id'],grid(base,axis_x=values))
    assert not store.get_run(run['id']).get('valuation_sensitivity_history')


def test_fact_change_blocks_old_basis_preserves_history_and_new_scenario_works(run):
    base=pe(run);first=analysis.save_sensitivity(run['id'],grid(base));snapshot=json.dumps(first,sort_keys=True)
    store.update_run(run['id'],lambda r:r['facts'][0].update(value='200000000'))
    with pytest.raises(ValueError,match='基数或来源已变化'): analysis.save_sensitivity(run['id'],grid(base))
    saved=store.get_run(run['id']);assert json.dumps(saved['valuation_sensitivity_history'][0],sort_keys=True)==snapshot
    assert analysis.summary(saved)['sensitivities'][0]['stale'] is True
    newer=pe(saved);assert newer['id']!=base['id']
    second=analysis.save_sensitivity(run['id'],grid(newer))
    assert second['revision']==2 and second['envelope']['equity_value_100m']['max']=='44'


def test_source_change_without_numeric_change_is_also_a_changed_basis(run):
    base=pe(run)
    store.update_run(run['id'],lambda r:r['evidence'][0].update(value='corrected-source'))
    with pytest.raises(ValueError,match='基数或来源已变化'): analysis.save_sensitivity(run['id'],grid(base))
    assert pe(run)['id']!=base['id']


def test_legacy_scenario_requires_matching_output_then_binds_current_sources(run):
    base=pe(run)
    store.update_run(run['id'],lambda r:r['valuation_history'][0].pop('basis_fingerprint'))
    result=analysis.save_sensitivity(run['id'],grid(base))
    assert any('历史基准未记录' in note for note in result['notes'])
    assert not analysis.summary(store.get_run(run['id']))['sensitivities'][0]['stale']


def test_comparable_median_only_descriptive_no_scenario_or_assumption_mutation(run):
    pe(run);before=store.get_run(run['id'])['valuation_history']
    result=analysis.save_comparables(run['id'],peers())
    assert result['statistics']=={'count':3,'excluded_count':0,'median':'20','min':'10','max':'30'}
    assert all(r['verification']=='user_provided' for r in result['rows'])
    assert store.get_run(run['id'])['valuation_history']==before
    assert any('不自动采用' in note for note in result['notes'])


@pytest.mark.parametrize('field,value',[('method','ps'),('period_kind','TTM'),('period_label','2024'),('valuation_date','2026-09-27'),('currency','USD')])
def test_mixed_period_date_currency_or_method_cannot_enter_one_peer_distribution(run,field,value):
    request=peers();request['rows'][0][field]=value
    with pytest.raises(ValueError,match='不能混合'): analysis.calculate_comparables(run,request)


def test_negative_earnings_and_missing_basis_are_retained_but_excluded(run):
    request=peers(rows=[{'company':'亏损同业','price':10,'denominator_per_share':-2,'price_basis':'收盘价','denominator_basis':'EPS'},
                       {'company':'无依据同业','multiple':20},
                       {'company':'有效同业','multiple':15,'multiple_basis':'已记录口径'}])
    result=analysis.calculate_comparables(run,request)
    assert result['statistics']['count']==1 and result['statistics']['excluded_count']==2
    assert '亏损' in result['rows'][0]['exclusion_reason']
    assert '缺少倍数来源' in result['rows'][1]['exclusion_reason']
    assert any('少于 3 家' in note for note in result['notes'])


@pytest.mark.parametrize('multiple,included',[(None,True),('9.995',True),('12',False)])
def test_price_denominator_calculation_and_inconsistent_supplied_multiple(run,multiple,included):
    request=peers(rows=[{'company':'同业','multiple':multiple,'price':'20','denominator_per_share':'2',
         'multiple_basis':'声明倍数','price_basis':'2026-09-28 收盘价','denominator_basis':'2025 年摊薄 EPS'}])
    row=analysis.calculate_comparables(run,request)['rows'][0]
    assert row['included'] is included and row['multiple_value']=='10'


def test_links_existing_sources_freezes_fingerprint_without_claiming_verified(run):
    request=peers();request['rows'][0]['source_ids']=['web-peer','income']
    result=analysis.save_comparables(run['id'],request)
    row=result['rows'][0]
    assert {ref['id'] for ref in row['source_refs']}=={'web-peer','income'}
    assert row['verification']=='user_provided'
    assert not analysis.summary(store.get_run(run['id']))['comparables'][0]['source_changed']
    store.update_run(run['id'],lambda r:r['research_sources'][0].update(text='Revised disclosure'))
    assert analysis.summary(store.get_run(run['id']))['comparables'][0]['source_changed']
    assert store.get_run(run['id'])['comparable_analyses'][0]==result


def test_unknown_source_or_late_source_is_rejected(run):
    request=peers();request['rows'][0]['source_ids']=['unknown']
    with pytest.raises(ValueError,match='不属于当前研究'): analysis.calculate_comparables(run,request)
    request['rows'][0]['source_ids']=['web-peer'];request['valuation_date']='2026-03-31'
    with pytest.raises(ValueError,match='晚于估值日'): analysis.calculate_comparables(run,request)


@pytest.mark.parametrize('patch',[
    {'period_kind':'TTM','period_label':'2025'},
    {'period_kind':'TTM','period_label':'2026-12-31'},
    {'period_kind':'FY','period_label':'2027'},
    {'period_kind':'FY','period_label':'2025FY'},
    {'valuation_date':'2026-10-01'},
])
def test_period_and_research_cutoff_validation(run,patch):
    with pytest.raises(ValueError): analysis.calculate_comparables(run,peers(**patch))


def test_forward_year_allowed_and_pb_financial_scope_disclosed(run):
    result=analysis.calculate_comparables(run,peers(method='pb',period_kind='forward',period_label='2027'))
    assert result['statistics']['count']==3 and any('归母净资产' in n for n in result['notes'])


def test_duplicate_company_and_target_self_excluded(run):
    request=peers();request['rows'][1]['company']='同业甲'
    with pytest.raises(ValueError,match='重复'): analysis.calculate_comparables(run,request)
    request=peers();request['rows'][0]['ticker']='600519.SH'
    result=analysis.calculate_comparables(run,request)
    assert not result['rows'][0]['included'] and result['statistics']['count']==2


def test_repeat_save_adds_immutable_versions_and_markdown_has_traceable_basis(run):
    base=pe(run)
    analysis.save_sensitivity(run['id'],grid(base));analysis.save_sensitivity(run['id'],grid(base,axis_y=[20,30]))
    request=peers();request['rows'][0].update(company='同业|甲',source_ids=['web-peer'])
    analysis.save_comparables(run['id'],request);analysis.save_comparables(run['id'],peers())
    value=store.get_run(run['id']);text=analysis.markdown_sections(value,audit=True)
    assert len(value['comparable_analyses'])==2 and len(value['valuation_sensitivity_history'])==2
    assert base['id'] in text and 'web-peer' in text and '同业 / 甲' in text
    assert '不是概率置信区间' in text and '用户提供' in text


def test_api_payload_validation_and_persisted_get(run):
    app=FastAPI();app.include_router(analysis.router)
    @app.exception_handler(ValueError)
    async def handler(request:Request,error:ValueError):return JSONResponse(status_code=400,content={'detail':str(error)})
    base=pe(run);client=TestClient(app)
    prefix='/api/runs/'+run['id']
    response=client.post(prefix+'/valuation-sensitivities',json=grid(base));assert response.status_code==200
    response=client.post(prefix+'/valuation-comparables',json=peers());assert response.status_code==200
    data=client.get(prefix+'/valuation-analysis').json()
    assert len(data['sensitivities'])==1 and len(data['comparables'])==1
    assert client.post(prefix+'/valuation-sensitivities',json=grid(base,axis_x=[True,2])).status_code==422
    assert client.post(prefix+'/valuation-comparables',json={**peers(),'silently_ignore':'bad'}).status_code==422
