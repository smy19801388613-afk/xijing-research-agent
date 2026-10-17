"""Synthetic period arithmetic benchmark; no network, model, or database writes.

Run: .venv/Scripts/python.exe -X utf8 scripts/evaluate_periods_v050.py
Optional: --output tmp/periods-v050-evaluation.json
This measures fixed financial calculation cases, not general extraction quality.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.financial_periods import analyze, normalize


def record(year, period, value, **kwargs):
    return {'metric':'revenue','year':year,'period':period,'period_type':'cumulative','value':str(value),
        'unit':'万元','currency':'CNY','scope':'consolidated','source_type':'user_input','source_id':'synthetic-evaluation',
        'quote':f'明确标注的合成测试数值 {value} {kwargs.get("unit","万元")}，不代表真实公司。',
        'announced_date':f'{year+1}-03-31',**kwargs}


def result(rows):
    run={'request':{'as_of':'2026-09-30'}}
    return analyze([normalize({**row,'id':f'synthetic-{i}'},run,refs={}) for i,row in enumerate(rows)])


def pick(data,section,**fields):
    return next(item for item in data[section] if all(item.get(k)==v for k,v in fields.items()))


def evaluate():
    cases=[]
    def check(name,description,observed,expected):
        cases.append({'case':name,'description':description,'expected':expected,'observed':observed,'passed':observed==expected})
    base=result([record(2024,'9M',70),record(2024,'FY',100),record(2025,'Q1',45),record(2025,'H1',100),record(2025,'9M',180),record(2025,'FY',260)])
    check('cumulative_to_quarters','累计依次拆成单季，Q4=全年−前三季度',
        [pick(base,'quarters',year=2025,quarter=q)['value'] for q in (1,2,3,4)],['450000','550000','800000','800000'])
    cross=pick(base,'comparisons',year=2025,quarter=1,kind='qoq',period_type='quarter')
    check('cross_year_qoq','Q1环比基期必须是上一年Q4',{'previous':cross['previous'],'percent':str(Decimal(cross['value']).normalize())},{'previous':'300000','percent':'5E+1'})
    missing=result([record(2025,'9M',180)])
    check('missing_predecessor','缺上半年累计时不得把前三季度数冒充Q3',pick(missing,'quarters',quarter=3)['value'],None)
    single=result([record(2025,'Q2',40,period_type='single')])
    check('explicit_quarter','明确单季无需依赖累计拆分',pick(single,'quarters',quarter=2)['value'],'400000')
    conflict=result([record(2025,'Q1',30),record(2025,'Q1',40)])
    check('same_period_conflict','同期间同口径多源冲突阻断',pick(conflict,'quarters',quarter=1)['value'],None)
    for name,previous in [('zero_baseline',0),('negative_baseline',-20)]:
        data=result([record(2024,'Q1',previous),record(2025,'Q1',30)])
        c=pick(data,'comparisons',year=2025,quarter=1,kind='yoy',period_type='quarter')
        check(name,'非正基期保留金额变化、不计算百分比',{'percent':c['value'],'change':c['change']},{'percent':None,'change':str((30-previous)*10000)})
    mixed=result([record(2024,'Q1',10),record(2025,'Q1',20,currency='HKD')])
    check('currency_isolation','不同币种不可直接比较',pick(mixed,'comparisons',year=2025,quarter=1,kind='yoy',period_type='quarter')['value'],None)
    point=result([record(2024,'FY',10,metric='inventory',period_type='point'),record(2025,'Q1',15,metric='inventory',period_type='point')])
    check('balance_not_flow','资产负债时点保留期末值、不得差额拆季',{'quarter_count':len(point['quarters']),'balance':pick(point,'balances',year=2025)['value']},{'quarter_count':0,'balance':'150000'})
    bridge=result([record(2025,'Q1',10,metric='n_income_attr_p'),record(2025,'Q1',7,metric='deducted_profit')])
    check('nonrecurring_bridge','同期间同口径归母−扣非归母',pick(bridge,'nonrecurring_bridges',period_type='single')['value'],'30000')
    unit=result([record(2025,'Q1',1,unit='亿元'),record(2025,'Q1',10000,unit='万元')])
    check('unit_normalization','等值跨单位输入不误报冲突',{'value':pick(unit,'quarters',quarter=1)['value'],'issues':len(unit['issues'])},{'value':'100000000','issues':0})
    check('provenance_inheritance','合成手工数据派生结果始终未核验',sorted({r['source_status'] for r in base['comparisons']}),['user_unverified'])
    return {'schema_version':1,'benchmark':'v0.5.0 固定合成财务期间用例','created_at':datetime.now(timezone.utc).isoformat(),
        'scope':'12 个合成算术与阻断规则用例；不测真实财报提取、归因或投资判断准确率。',
        'network_calls':0,'paid_model_calls':0,'database_writes':0,'passed':sum(c['passed'] for c in cases),'total':len(cases),'cases':cases}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args=parser.parse_args()
    report=evaluate();text=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(text+'\n',encoding='utf-8')
    print(text)
    raise SystemExit(0 if report['passed']==report['total'] else 1)
