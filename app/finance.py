from decimal import Decimal, InvalidOperation
import re

METRICS = {
    "revenue": {"label":"营业收入","aliases":["营业收入"],"api":"income"},
    "n_income_attr_p": {"label":"归母净利润","aliases":["归属于上市公司股东的净利润","归属于母公司所有者的净利润"],"api":"income"},
    "n_cashflow_act": {"label":"经营现金流净额","aliases":["经营活动产生的现金流量净额"],"api":"cashflow"},
}

def number(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        d = Decimal(str(value).replace(",", ""))
        return d if d.is_finite() else None
    except InvalidOperation:
        return None

def growth(current, previous):
    c,p = number(current),number(previous)
    if c is None or p is None:
        return {"value":None,"reason":"缺少同口径本期或上期数值"}
    if p <= 0:
        return {"value":None,"change":str(c-p),"reason":"基期为零或负值，使用金额变化，不套用增长率"}
    return {"value":str((c/p-1)*100),"change":str(c-p),"reason":None}

def eligible_record(rows, year, as_of):
    eligible=[]
    for r in rows:
        announced=r.get("f_ann_date") or r.get("ann_date")
        if (r.get("end_date")==f"{year}1231" and str(r.get("report_type"))=="1"
                and announced and str(announced)<=as_of.replace("-", "")):
            eligible.append(r)
    if not eligible:
        return None
    eligible.sort(key=lambda r:(r.get("f_ann_date") or r.get("ann_date"),str(r.get("update_flag",""))),reverse=True)
    latest=eligible[0]
    # Conflicting records at the same known announcement date need review.
    same=[r for r in eligible if (r.get("f_ann_date") or r.get("ann_date"))==(latest.get("f_ann_date") or latest.get("ann_date"))]
    for field in METRICS:
        values={str(number(r.get(field))) for r in same if number(r.get(field)) is not None}
        if len(values)>1:
            raise ValueError("同一公告日期存在冲突版本，需人工核对")
    return latest

def compute(facts, year):
    result=[]
    for metric in METRICS:
        current=next((f for f in facts if f["metric"]==metric and f["year"]==year),None)
        previous=next((f for f in facts if f["metric"]==metric and f["year"]==year-1),None)
        g=growth(current.get("value") if current else None, previous.get("value") if previous else None)
        if any(f and f.get("status")=="conflict" for f in (current,previous)):
            g={"value":None,"reason":"本期或基期存在来源冲突，核实后再计算同比"}
        amount_only = g.get('value') is None and number(g.get('change')) is not None
        result.append({"id":f"yoy-{metric}","metric":metric,"label":METRICS[metric]["label"],"kind":"yoy","unit":"元" if amount_only else "%",
                       "formula":"本期值 - 上期值" if amount_only else "(本期值 / 上期值 - 1) × 100","inputs":[f["id"] for f in (current,previous) if f],**g})
    return result

def comparison_check(calculations):
    """A nonpositive baseline has a valid amount comparison, not a failed ratio."""
    return {"kind":"calculation", "passed":bool(calculations) and all(
        number(c.get('value')) is not None or number(c.get('change')) is not None for c in calculations),
        "label":"核心同比计算", "detail":"Decimal 计算；正基期计算增长率，零或负基期计算金额变化"}


def comparison_view(run):
    """Correct legacy presentation metadata without rewriting stored audit records."""
    calculations = [dict(c) for c in run.get('calculations', [])]
    amount_ids = set()
    for calc in calculations:
        if calc.get('kind') == 'yoy' and calc.get('value') is None and number(calc.get('change')) is not None:
            calc.update(formula='本期值 - 上期值', unit='元')
            amount_ids.add(calc.get('id'))
    checks = [comparison_check(calculations) if c.get('kind') == 'calculation' and c.get('label') == '核心同比计算'
              else dict(c) for c in run.get('checks', [])]
    return {**run, 'calculations':calculations, 'checks':checks,
            'gaps':[g for g in run.get('gaps', []) if g.get('code') not in amount_ids]}


def scenario(profit, growth_pct, pe, shares=None):
    p,g,m=number(profit),number(growth_pct),number(pe)
    if p is None or p<=0 or g is None or g<=-100 or m is None or m<=0:
        raise ValueError("PE 情景需要正的归母净利润和有效的增长假设、估值倍数")
    future=p*(1+g/100)
    cap=future*m
    count=number(shares)
    if shares is not None and (count is None or count<=0):
        raise ValueError("股数必须大于零")
    return {"forecast_profit_100m":str(future/Decimal(100000000)),"equity_value_100m":str(cap/Decimal(100000000)),
            "price":str(cap/(count*Decimal(100000000))) if count else None,
            "formula":"预测归母净利润 = 历史归母净利润 × (1 + 假设增长率)；股权价值 = 预测归母净利润 × 假设 PE",
            "assumption_source":"用户设置的探索情景，非一致预期、非目标价建议"}

def audit_text(text, facts, calculations, year):
    """Conservative checks for supported numeric claims; no broad semantic promises."""
    issues=[]
    checked_amounts=set()
    current={f["metric"]:number(f["value"]) for f in facts if f["year"]==year and f["value"] is not None}
    for metric, meta in METRICS.items():
        if metric not in current:
            continue
        aliases=meta["aliases"]+[meta["label"]]
        if metric=="n_cashflow_act": aliases += ["经营活动现金流量净额","经营现金流","现金流净额"]
        for alias in sorted(set(aliases),key=len,reverse=True):
            for match in re.finditer(re.escape(alias)+fr"(?:[为是：:\s]|达到|约|人民币|{year}年)*([+-]?[\d,]+(?:\.\d+)?)\s*(亿元|万元|千元|元)",text):
                # Aliases may overlap, but separate occurrences remain separate claims.
                location=(metric,match.start(1),match.end(2))
                if location in checked_amounts:
                    continue
                checked_amounts.add(location)
                context=re.split(r"[。；;\n]",text[max(0,match.start()-60):match.start()])[-1]
                years=re.findall(r"(20\d{2})\s*年",context)
                if years and int(years[-1])!=year:
                    continue
                multiplier={"亿元":Decimal(100000000),"万元":Decimal(10000),"千元":Decimal(1000),"元":Decimal(1)}[match[2]]
                stated=number(match[1])*multiplier
                # Tolerance follows the number of displayed decimal places.
                precision=len(match[1].split(".")[1]) if "." in match[1] else 0
                tolerance=multiplier*(Decimal(10)**(-precision))/2
                if abs(stated-current[metric])>tolerance:
                    issues.append({"type":"value_mismatch","claim":match[0],"message":f"{meta['label']}与当前结构化值不一致","expected_100m":str(current[metric]/Decimal(100000000)),"fact_id":f"{metric}-{year}","span":[match.start(),match.end()]})
    cash=current.get("n_cashflow_act")
    profit=current.get("n_income_attr_p")
    if cash is not None and profit is not None:
        if cash>profit and re.search(r"现金流[^。；\n]{0,20}(?:比|低于)[^。；\n]{0,10}利润[^。；\n]{0,5}(?:小|低)|现金流[^。；\n]{0,10}低于(?:归母净)?利润",text):
            issues.append({"type":"relation_mismatch","claim":"现金流小于利润","message":"结构化现金流大于归母净利润；原文大小关系相反"})
    cash_yoy=next((number(c["value"]) for c in calculations if c["metric"]=="n_cashflow_act"),None)
    if cash_yoy is not None and cash_yoy<0 and re.search(r"现金流[^。；，,\n]{0,20}(?:同比)?(?:增长|增加|上升)\s*\+?\d",text):
        issues.append({"type":"direction_mismatch","claim":"现金流同比增长","message":"结构化经营现金流同比下降，请检查符号和方向"})
    if any(s in text for s in ["盈利质量全面改善","盈利质量必然恶化","保证收益","稳赚"]):
        issues.append({"type":"unsupported_conclusion","claim":"强结论","message":"汇总财务数据不足以支持该判断，需要附注、营运资本或其他证据"})
    if re.search(r"(?:表明|说明|证明)[^。；\n]{0,8}(?:利润)?现金含量(?:尚可|较高|改善)|则利润质量恶化",text):
        issues.append({"type":"scope_mismatch","claim":"以不同口径的现金流和利润直接推断利润质量","message":"合并经营现金流和归母净利润的股东归属口径不同，金额比较不足以支持该质量判断"})
    unique={json_key(i):i for i in issues}
    return list(unique.values())

def json_key(issue):
    return issue["type"]+issue["claim"]+str(issue.get("span", ""))
