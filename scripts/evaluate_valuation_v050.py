"""Offline financial acceptance examples, not an accuracy benchmark.

Run: .venv/Scripts/python.exe -X utf8 scripts/evaluate_valuation_v050.py
Optional: --output tmp/valuation-v050-evaluation.json
No credentials, network calls, saved research, or environment changes.
Expected numbers are independently specified financial examples, not snapshots
of implementation output. This intentionally does not benchmark model quality.
"""
import argparse
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import valuation,valuation_analysis


def evaluate():
    run={'id':'offline-valuation-evaluation','state':'completed','request':{
        'ticker':'000001.SZ','year':2025,'as_of':'2026-09-28'},'facts':[
        {'id':'synthetic-profit','metric':'n_income_attr_p','year':2025,'value':'2000000000',
         'status':'document_only','label':'合成归母净利润','evidence_ids':['synthetic-source']}],
         'evidence':[{'id':'synthetic-source','kind':'document','title':'明确标记的合成测试材料'}],
         'valuation_history':[]}
    checks=[]
    def check(name,actual,expected):
        checks.append({'case':name,'actual':actual,'expected':expected,'passed':actual==expected})

    # Profit units: 2,000,000,000 CNY = 20 * 100m CNY.
    # A 0% / 20% forecast, multiples 10x / 15x and 2bn diluted shares
    # produce equity envelopes 200..360 * 100m and prices 10..18 CNY.
    base=valuation.calculate(run,'pe',{'growth_pct':10,'multiple':12,'shares_100m':20})
    run['valuation_history']=[base]
    sensitivity=valuation_analysis.calculate_sensitivity(run,{
        'base_scenario_id':base['id'],'axis_x':[0,20],'axis_y':[10,15]})
    check('PE 股权价值包络（亿元）',sensitivity['envelope']['equity_value_100m'],{'min':'200','max':'360'})
    check('PE 每股价值包络（元）',sensitivity['envelope']['price'],{'min':'10','max':'18'})

    # FCFF is a perpetual constant 20 * 100m; discount rate 10%, g=0.
    # Regardless of the five-year partition, PV is 20 / 0.10 = 200.
    # Net debt/bridge 40 gives equity 160; 10 * 100m shares gives price 16.
    steady=valuation.calculate(run,'dcf',{'base_fcff_100m':20,'growth_pct':0,'years':5,
        'wacc_pct':10,'terminal_growth_pct':0,'equity_bridge_100m':40,'shares_100m':10})
    check('恒定现金流 DCF 企业价值（亿元）',str(Decimal(steady['output']['enterprise_value_100m']).quantize(Decimal('.000001'))),'200.000000')
    check('DCF 企业价值至股权桥接、每股价值（元）',str(Decimal(steady['output']['price']).quantize(Decimal('.000001'))),'16.000000')
    run['valuation_history'].append(steady)
    unstable=valuation_analysis.calculate_sensitivity(run,{'base_scenario_id':steady['id'],'axis_x':[4,6],'axis_y':[4,5]})
    check('WACC <= g 的组合排除',{'valid':unstable['valid_cells'],'invalid':unstable['invalid_cells']},{'valid':2,'invalid':2})

    # Two profitable peers provide 9x and 15x: the median is 12x.
    # A negative EPS company must remain visible but must not enter statistics.
    peers=valuation_analysis.calculate_comparables(run,{'method':'pe','period_kind':'FY','period_label':'2025',
        'valuation_date':'2026-09-28','currency':'CNY','rows':[
            {'company':'合成同业甲','price':18,'denominator_per_share':2,'price_basis':'合成收盘价','denominator_basis':'合成 FY2025 EPS'},
            {'company':'合成同业乙','price':45,'denominator_per_share':3,'price_basis':'合成收盘价','denominator_basis':'合成 FY2025 EPS'},
            {'company':'合成亏损同业','price':10,'denominator_per_share':-1,'price_basis':'合成收盘价','denominator_basis':'合成 FY2025 EPS'}]})
    check('同业 PE 中位数与亏损排除',peers['statistics'],{'count':2,'excluded_count':1,'median':'12','min':'9','max':'15'})
    check('同业数值不升级为已核验',sorted({r['verification'] for r in peers['rows']}),['user_provided'])

    # A corrected base must not silently recompute an old saved scenario grid.
    changed=deepcopy(run);changed['facts'][0]['value']='2500000000'
    rejected=False
    try:
        valuation_analysis.calculate_sensitivity(changed,{'base_scenario_id':base['id'],'axis_x':[0,20],'axis_y':[10,15]})
    except ValueError as error: rejected='基数或来源已变化' in str(error)
    check('财务基数改变后拒用旧基准',rejected,True)
    return {'suite':'v0.5.0 fixed financial examples','scope':'固定合成案例验收，不代表通用准确率或大模型投研质量',
        'paid_model_requests':0,'network_requests':0,'writes_research_history':False,
        'checks':checks,'passed':all(c['passed'] for c in checks),'passed_cases':sum(c['passed'] for c in checks),'total_cases':len(checks)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();result=evaluate()
    rendered=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(rendered+'\n',encoding='utf-8')
    print(rendered)
    raise SystemExit(0 if result['passed'] else 1)
