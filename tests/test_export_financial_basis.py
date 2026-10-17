"""Reading views suppress conflicted calculations; audit copies stay original."""
from copy import deepcopy

from app import exporting, financial_periods, research, store
from app.finance import compute


def test_normal_and_audit_exports_preserve_different_calculation_semantics(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    run=store.create_run({'ticker':'600000.SH','year':2025,'as_of':'2026-10-17','mode':'live','question':'核对财务口径。'})
    run.update(state='completed',memo={'title':'合成口径冲突','summary':'核对年度与期间记录。'})
    for year,value in ((2024,'100000000'),(2025,'150000000')):
        research.add_api_facts(run,'income',year,{'n_income_attr_p':value,'ann_date':f'{year+1}0420'},'synthetic')
    run['calculations']=compute(run['facts'],2025)
    run['research_sources']=[{'id':'conflict-source','kind':'pdf','title':'合成披露','published_at':'2026-04-20',
        'read_status':'pdf_text','text':'2025年合并归母净利润120000000元。'}]
    store.save_run(run)
    financial_periods.save_records(run['id'],[{'metric':'n_income_attr_p','year':2025,'period':'FY','period_type':'cumulative',
        'value':'120000000','unit':'元','currency':'CNY','scope':'consolidated','announced_date':'2026-04-20',
        'source_type':'existing','source_id':'conflict-source','quote':'2025年合并归母净利润120000000元。'}],origin='model')
    run=store.get_run(run['id']);before=deepcopy(run)
    for render in (exporting.markdown,exporting.printable):
        reading=render(run);audit=render(run,audit=True)
        assert '财务口径冲突' in reading and '同口径金额冲突' in reading
        assert '+50.00%' not in reading and '不适用/缺失' in reading
        assert '+50.00%' in audit
        assert '财务口径冲突' in audit and '审计版保留原始计算值' in audit
        assert '1.50' in reading and '1.00' in reading
    assert run==before and store.get_run(run['id'])==before


def test_missing_fact_amount_is_rendered_without_division_error(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    run=store.create_run({'ticker':'600000.SH','year':2025,'as_of':'2026-10-17','mode':'live','question':'核对缺失数据。'})
    run['facts']=[{'id':'profit','metric':'n_income_attr_p','label':'归母净利润','year':2025,'value':None,'status':'missing','evidence_ids':[]}]
    for render in (exporting.markdown,exporting.printable):
        assert '缺失' in render(run)
