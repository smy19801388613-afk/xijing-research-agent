"""Deterministic period math; sources and reports never touch production state."""
import copy
from decimal import Decimal
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from app import financial_periods as fp, store


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    value = store.create_run({'ticker':'600001.SH','year':2025,'as_of':'2026-09-30','mode':'live','use_model':False})
    store.update_run(value['id'], lambda r:r.update(state='completed_with_gaps',facts=[{'id':'original','metric':'revenue','label':'营业收入','year':2025,'value':'123','status':'api_only'}]))
    return store.get_run(value['id'])


def row(year=2025, period='FY', value='100', **kw):
    return {'metric':'revenue','year':year,'period':period,'period_type':'cumulative','value':value,'unit':'万元','currency':'CNY',
            'scope':'consolidated','source_type':'user_input','source_id':'annual-report-p10','quote':'原文记录金额 '+str(value)+' '+kw.get('unit','万元'),
            'announced_date':f'{year+1}-03-31',**kw}


def quarter(result, year, q, metric='revenue'):
    return next(n for n in result['analysis']['quarters'] if n['year']==year and n['quarter']==q and n['metric']==metric)


def comparison(result, year, q, typ='qoq', period_type='quarter'):
    return next(n for n in result['analysis']['comparisons'] if n['year']==year and n['quarter']==q and n['kind']==typ and n['period_type']==period_type)


def test_cumulative_quarter_split_and_cross_year_qoq(run):
    result=fp.save_records(run['id'],[row(2024,'9M','70'),row(2024,'FY','100'),row(2025,'Q1','45'),row(2025,'H1','100'),row(2025,'9M','180'),row(2025,'FY','260')])
    assert [quarter(result,2025,q)['value'] for q in (1,2,3,4)]==['450000','550000','800000','800000']
    first=comparison(result,2025,1)
    assert Decimal(first['value'])==50 and first['previous']=='300000'
    assert first['source_status']=='user_unverified'
    assert len(first['inputs'])==3
    assert store.get_run(run['id'])['facts']==run['facts']


def test_missing_cumulative_predecessor_does_not_treat_ytd_as_quarter(run):
    result=fp.save_records(run['id'],[row(period='9M',value='300')])
    point=quarter(result,2025,3)
    assert point['value'] is None and '上一累计' in point['reason']


def test_explicit_single_quarter_does_not_need_ytd(run):
    result=fp.save_records(run['id'],[row(period='Q3',period_type='single',value='70')])
    assert quarter(result,2025,3)['value']=='700000'


def test_direct_vs_derived_quarter_conflict_blocks(run):
    result=fp.save_records(run['id'],[row(period='Q1',value='30'),row(period='H1',value='70'),row(period='Q2',period_type='single',value='50')])
    assert quarter(result,2025,2)['value'] is None
    assert result['analysis']['issues'][0]['code']=='quarter_conflict'


def test_same_period_source_conflict_blocks_and_cannot_be_overridden_by_last_row(run):
    result=fp.save_records(run['id'],[row(period='Q1',value='30'),row(period='Q1',value='40')])
    assert quarter(result,2025,1)['value'] is None
    assert result['analysis']['issues'][0]['code']=='conflict'


@pytest.mark.parametrize('unit,value',[('元','100000000'),('千元','100000'),('万元','10000'),('亿元','1')])
def test_unit_normalization(run,unit,value):
    result=fp.save_records(run['id'],[row(period='Q1',value=value,unit=unit)])
    assert Decimal(quarter(result,2025,1)['value'])==100000000


def test_equivalent_values_different_units_not_conflict(run):
    result=fp.save_records(run['id'],[row(period='Q1',value='1',unit='亿元'),row(period='Q1',value='10000',unit='万元')])
    assert quarter(result,2025,1)['value']=='100000000'
    assert not result['analysis']['issues']


@pytest.mark.parametrize('changes,message',[
    ({'unit':'百万'},'单位'),({'currency':'RMB'},'币种'),({'period_type':'annual'},'口径'),
    ({'value':'NaN'},'有限'),({'value':True},'有限'),({'year':True},'年度'),
    ({'period':'H1','period_type':'single'},'单季度'),({'scope':'attributable'},'报表范围'),
    ({'metric':'n_income_attr_p','scope':'parent'},'归母'),({'source_id':''},'来源 ID'),
    ({'quote':''},'原文摘录'),({'announced_date':'2026-10-30'},'未来'),
    ({'announced_date':'2025-03-31'},'早于报告期末'),({'restatement':'restated'},'重述'),
    ({'metric':'inventory','period_type':'cumulative'},'时点')])
def test_invalid_inputs_rejected_atomically(run,changes,message):
    before=store.get_run(run['id'])
    with pytest.raises(ValueError,match=message): fp.save_records(run['id'],[row(),row(**changes)])
    assert store.get_run(run['id'])==before


@pytest.mark.parametrize('change',[{'currency':'HKD'},{'scope':'parent'},{'comparability_key':'new-gaap'},
    {'restatement':'restated','comparability_key':'restated-2025','restatement_note':'同口径重述'}])
def test_incompatible_basis_not_compared_or_split(run,change):
    result=fp.save_records(run['id'],[row(2024,'Q1','10'),row(2025,'Q1','20',**change)])
    c=comparison(result,2025,1,'yoy')
    assert c['value'] is None and c['change'] is None and '同币种' in c['reason']


@pytest.mark.parametrize('previous',['0','-20'])
def test_nonpositive_baseline_yields_amount_change_not_growth(run,previous):
    result=fp.save_records(run['id'],[row(2024,'Q1',previous),row(2025,'Q1','30')])
    c=comparison(result,2025,1,'yoy')
    assert c['value'] is None and Decimal(c['change'])==(Decimal('30')-Decimal(previous))*10000
    assert '零或负' in c['reason']


def test_balance_is_point_never_subtracted_into_quarter_flow(run):
    result=fp.save_records(run['id'],[row(2024,'FY','10',metric='inventory',period_type='point'),row(2025,'Q1','15',metric='inventory',period_type='point')])
    assert not result['analysis']['quarters']
    assert len(result['analysis']['balances'])==2
    c=comparison(result,2025,1,'qoq','balance')
    assert c['current']=='150000' and Decimal(c['value'])==50


def test_nonrecurring_bridge_same_basis_only(run):
    result=fp.save_records(run['id'],[row(period='Q1',value='10',metric='n_income_attr_p'),row(period='Q1',value='7',metric='deducted_profit'),
        row(period='H1',value='25',metric='n_income_attr_p'),row(period='H1',value='20',metric='deducted_profit',currency='HKD')])
    good=next(b for b in result['analysis']['nonrecurring_bridges'] if 'Q1单季' in b['period_label'])
    assert good['value']=='30000' and good['source_status']=='user_unverified'
    bad=next(b for b in result['analysis']['nonrecurring_bridges'] if '上半年' in b['period_label'])
    assert bad['value'] is None and '同口径' in bad['reason']


def test_divergence_is_signal_not_causal_claim(run):
    result=fp.save_records(run['id'],[row(2024,'Q1','10',metric='n_income_attr_p'),row(2025,'Q1','20',metric='n_income_attr_p'),
        row(2024,'Q1','20',metric='n_cashflow_act'),row(2025,'Q1','10',metric='n_cashflow_act')])
    signal=result['analysis']['signals'][0]
    assert signal['profit_change']=='100000' and signal['cashflow_change']=='-100000'
    assert '不能据此认定原因' in signal['message'] and signal['source_status']=='user_unverified'


def test_model_cannot_invent_user_source_or_mark_verified(run):
    with pytest.raises(ValueError,match='模型只能绑定'):fp.save_records(run['id'],[row(source_status='verified')],origin='model')
    result=fp.save_records(run['id'],[row(source_status='verified')])
    assert result['records'][0]['source_status']=='user_unverified'


def test_quote_unit_mismatch_is_rejected(run):
    with pytest.raises(ValueError,match='单位与原文'):fp.save_records(run['id'],[row(unit='元',quote='营业收入 100 万元')])


def test_direct_quarter_cannot_override_conflicting_cumulative_inputs(run):
    result=fp.save_records(run['id'],[row(period='Q1',value='30'),row(period='H1',value='70'),row(period='H1',value='80'),row(period='Q2',period_type='single',value='40')])
    assert quarter(result,2025,2)['value'] is None
    assert '不能用直接单季' in quarter(result,2025,2)['reason']


def evidence(run,**changes):
    ev={'id':'doc-p18','kind':'document','quote':'2025 年第一季度营业收入 100 万元。','announced_date':'2025-04-30','read_status':'pdf_text',**changes}
    return store.update_run(run['id'],lambda r:r.update(evidence=[ev]))


def test_real_catalog_excerpt_matched_never_semantically_verified(run):
    evidence(run)
    result=fp.save_records(run['id'],[row(period='Q1',source_type='existing',source_id='doc-p18',quote='第一季度营业收入 100 万元',announced_date='2025-04-30',source_status='verified')],origin='model')
    assert result['records'][0]['source_status']=='source_matched'
    assert quarter(result,2025,1)['source_status']=='source_matched'
    assert '不代表' in result['note']


def test_model_cannot_silently_invent_amount_with_real_quote(run):
    evidence(run)
    with pytest.raises(ValueError,match='原始金额未在绑定摘录'):
        fp.save_records(run['id'],[row(period='Q1',value='999',source_type='existing',source_id='doc-p18',quote='第一季度营业收入 100 万元',announced_date='2025-04-30')],origin='model')


def test_accounting_parentheses_preserve_negative_amount(run):
    evidence(run,quote='2025 年第一季度归母净利润（100.00）万元。')
    result=fp.save_records(run['id'],[row(period='Q1',metric='n_income_attr_p',value='-100',source_type='existing',source_id='doc-p18',quote='第一季度归母净利润（100.00）万元',announced_date='2025-04-30')],origin='model')
    assert quarter(result,2025,1,'n_income_attr_p')['value']=='-1000000'


def test_unknown_date_and_unread_snippet_are_not_extraction_sources(run):
    evidence(run,announced_date='')
    with pytest.raises(ValueError,match='可用正文、日期'):
        fp.save_records(run['id'],[row(period='Q1',source_type='existing',source_id='doc-p18',quote='第一季度营业收入 100 万元',announced_date='2025-04-30')],origin='model')
    evidence(run,read_status='search_snippet')
    with pytest.raises(ValueError,match='可用正文、日期'):
        fp.save_records(run['id'],[row(period='Q1',source_type='existing',source_id='doc-p18',quote='第一季度营业收入 100 万元',announced_date='2025-04-30')],origin='model')


@pytest.mark.parametrize('changes,message',[({'source_id':'missing'},'不存在'),({'quote':'编造的数据 999 万元'},'未匹配'),({'announced_date':'2025-05-01'},'不一致')])
def test_existing_sources_must_be_real_and_date_match(run,changes,message):
    evidence(run)
    payload=row(period='Q1',source_type='existing',source_id='doc-p18',quote='第一季度营业收入 100 万元',announced_date='2025-04-30')
    with pytest.raises(ValueError,match=message):fp.save_records(run['id'],[{**payload,**changes}])


def test_changed_source_invalidates_derived_results_without_rewriting_record(run):
    evidence(run)
    fp.save_records(run['id'],[row(period='Q1',source_type='existing',source_id='doc-p18',quote='第一季度营业收入 100 万元',announced_date='2025-04-30')])
    previous=copy.deepcopy(store.get_run(run['id'])['financial_period_records'])
    evidence(run,quote='2025 年第一季度营业收入更正为 200 万元。')
    result=fp.snapshot(store.get_run(run['id']))
    assert result['records'][0]['source_status']=='source_changed' and quarter(result,2025,1)['value'] is None
    assert store.get_run(run['id'])['financial_period_records']==previous


def test_changed_source_date_invalidates_even_with_identical_text(run):
    evidence(run)
    fp.save_records(run['id'],[row(period='Q1',source_type='existing',source_id='doc-p18',quote='第一季度营业收入 100 万元',announced_date='2025-04-30')])
    evidence(run,announced_date='2025-05-01')
    assert fp.validated_records(store.get_run(run['id']))[0]['source_status']=='source_changed'


def test_update_history_delete_and_http_contract(run):
    app=FastAPI();app.include_router(fp.router)
    @app.exception_handler(ValueError)
    async def bad(request:Request,error):return JSONResponse({'detail':str(error)},400)
    client=TestClient(app)
    path='/api/runs/'+run['id']+'/financial-periods'
    assert client.get(path).json()['records']==[]
    first=client.post(path,json={'records':[row(period='Q1')]}).json()
    r=first['records'][0]
    assert first['artifact']=='financial_periods'
    updated=client.post(path,json={'records':[{**r,'value':'200'}]}).json()['records'][0]
    assert updated['history'][0]['value']=='100'
    assert client.delete(path+'/'+r['id']).json()['records']==[]
    assert store.get_run(run['id'])['financial_period_deleted'][0]['value']=='200'
