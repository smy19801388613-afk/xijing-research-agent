"""Deterministic valuation: user assumptions remain separate from reported facts."""
from decimal import Decimal,InvalidOperation
import uuid
from . import store
from .fact_state import verification

def basis_fingerprint(run, fact_ids=()):
    """Bind a scenario to its inputs and provenance, not the mutable run timestamp."""
    import hashlib,json
    facts=[{k:v for k,v in f.items() if k!='verification'} for f in run.get('facts',[]) if f.get('id') in fact_ids]
    evidence_ids={i for f in facts for i in f.get('evidence_ids',[])}
    basis={'request':{k:run.get('request',{}).get(k) for k in ('ticker','year','as_of')},
           'facts':facts,'evidence':[e for e in run.get('evidence',[]) if e.get('id') in evidence_ids]}
    return hashlib.sha256(json.dumps(basis,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()

def field(key,label,unit="",required=True,minimum=None,maximum=None):
    return {"key":key,"label":label,"unit":unit,"required":required,"min":minimum,"max":maximum}

GROWTH=field("growth_pct","下一期增长假设","%",True,-95,300)
SHARES=field("shares_100m","摊薄后总股本","亿股",False,0)
BRIDGE=field("equity_bridge_100m","企业价值转股权价值扣减项","亿元",False)
CATALOG=[
    {"id":"pe","name":"PE · 市盈率","description":"适合利润为正、盈利口径可比较的公司。倍数需要可比公司或历史区间支持。",
     "formula":"下一期归母净利润 × PE","metric":"n_income_attr_p",
     "fields":[GROWTH,field("multiple","PE 倍数","倍",True,0,150),SHARES]},
    {"id":"ps","name":"PS · 市销率","description":"收入有意义而盈利尚不稳定时可作辅助；必须同时讨论利润率。此处 PS 对应股权价值。",
     "formula":"下一期营业收入 × PS","metric":"revenue",
     "fields":[GROWTH,field("multiple","PS 倍数","倍",True,0,100),SHARES]},
    {"id":"pb","name":"PB · 市净率","description":"适合资产与回报率分析。必须输入归属于母公司股东的净资产，不能混用含少数股东权益的总权益。",
     "formula":"归母净资产 × PB","fields":[field("book_equity_100m","归母净资产","亿元",True,0),field("multiple","PB 倍数","倍",True,0,50),SHARES]},
    {"id":"dcf","name":"DCF · 企业自由现金流","description":"预测 FCFF 并按 WACC 折现。经营现金流不等于 FCFF；终值增长率必须低于 WACC。",
     "formula":"预测期 FCFF 现值 + 终值现值 − 股权桥接扣减项",
     "fields":[field("base_fcff_100m","基期 FCFF（不可直接用 CFO）","亿元",True,0),GROWTH,
               field("years","预测期","年",True,1,10),field("wacc_pct","WACC","%",True,0,50),
               field("terminal_growth_pct","永续增长率","%",True,-10,10),BRIDGE,SHARES]},
    {"id":"ev_ebitda","name":"EV / EBITDA","description":"适合经营与资本结构对比。先得到企业价值，再扣除净债务等桥接项得到股权价值。",
     "formula":"EBITDA × EV/EBITDA − 股权桥接扣减项",
     "fields":[field("ebitda_100m","预测 EBITDA","亿元",True,0),field("multiple","EV/EBITDA 倍数","倍",True,0,100),BRIDGE,SHARES]},
    {"id":"ddm","name":"DDM · 稳定增长股利","description":"适合股利政策稳定的公司。此版本为 Gordon 永续增长模型，成长阶段变化应改用多阶段模型。",
     "formula":"下一期每股股利 / (股权成本 − 永续增长率)",
     "fields":[field("dividend_per_share","基期每股股利","元/股",True,0),field("cost_of_equity_pct","股权成本","%",True,0,50),
               field("terminal_growth_pct","永续增长率","%",True,-10,10)]},
]

def options(run):
    from .financial_basis import conflicts
    blocked={(i['metric'],i['year']):i['message'] for i in conflicts(run)}
    result=[]
    for method in CATALOG:
        item=dict(method);metric=method.get("metric")
        fact=next((f for f in run["facts"] if f["metric"]==metric and f["year"]==run["request"]["year"] and f["status"]!="conflict"),None) if metric else None
        item["base_fact"]=fact
        issue=blocked.get((metric,run['request']['year']))
        item["ready_base"]=bool(fact and Decimal(fact["value"])>0 and not issue) if metric else False
        item["basis_note"]=("历史基数已取得；预测增速与倍数仍需确认" if item["ready_base"] else "需要补充对应指标与假设")
        if issue: item['basis_note']=issue
        result.append(item)
    return result

def calculate(run,method,inputs,origin="form",quotes=None):
    spec=next((m for m in CATALOG if m["id"]==method),None)
    if not spec: raise ValueError("不支持该估值方法")
    if run["state"] in ("queued","running"): raise ValueError("请先完成财务研究")
    allowed={f["key"] for f in spec["fields"]}
    if set(inputs)-allowed: raise ValueError("当前方法包含不支持的参数："+", ".join(set(inputs)-allowed))
    values={};missing=[]
    for f in spec["fields"]:
        raw=inputs.get(f["key"])
        if raw is None or raw=="":
            if f["required"]: missing.append(f["label"])
            continue
        try:
            if isinstance(raw,bool): raise InvalidOperation()
            n=Decimal(str(raw))
            if not n.is_finite(): raise InvalidOperation()
            if abs(n)>Decimal("1e12"): raise InvalidOperation()
        except (InvalidOperation,ValueError): raise ValueError(f["label"]+"必须是有限数字") from None
        if f["min"] is not None and (n<Decimal(str(f["min"])) or (f["min"]==0 and n==0)):
            raise ValueError(f["label"]+"低于允许范围")
        if f["max"] is not None and n>Decimal(str(f["max"])): raise ValueError(f["label"]+"超过允许范围")
        values[f["key"]]=n
    if missing: raise ValueError("需要补充："+ "、".join(missing))
    fact_ids=[];base=None;rows=[];notes=[]
    out={"equity_value_100m":None,"enterprise_value_100m":None,"price":None}
    if method in ("pe","ps"):
        from .financial_basis import assert_usable
        assert_usable(run,spec['metric'],run['request']['year'])
        fact=next((f for f in run["facts"] if f["metric"]==spec["metric"] and f["year"]==run["request"]["year"] and f["status"]!="conflict"),None)
        if not fact or Decimal(fact["value"])<=0: raise ValueError("缺少无冲突且为正的历史基数，不能计算该方法")
        if not verification(fact,run.get("evidence",[]),run["request"].get("as_of"))["usable_for_calculation"]:
            raise ValueError("历史基数缺失、来源未确认或OCR待复核，不能采用")
        notes.append("基数来源状态："+verification(fact,run.get("evidence",[]))["label"]+"；确定性计算不升级来源可信度。")
        base=Decimal(fact["value"])/Decimal(100000000);fact_ids=[fact["id"]]
        forecast=base*(1+values["growth_pct"]/100)
        out.update(base_100m=base,forecast_100m=forecast,equity_value_100m=forecast*values["multiple"])
    elif method=="pb":
        out["equity_value_100m"]=values["book_equity_100m"]*values["multiple"]
        notes.append("归母净资产由用户提供，尚未接入自动资产负债表核验。")
    elif method=="dcf":
        years=values["years"]
        if years!=int(years): raise ValueError("预测期必须为整数年")
        r=values["wacc_pct"]/100;g=values["terminal_growth_pct"]/100
        if r<=g: raise ValueError("WACC 必须大于永续增长率")
        cf=values["base_fcff_100m"];pv=Decimal(0)
        for year in range(1,int(years)+1):
            cf*=1+values["growth_pct"]/100
            discounted=cf/(1+r)**year;pv+=discounted
            rows.append({"year":run["request"]["year"]+year,"fcff_100m":cf,"present_value_100m":discounted})
        terminal=cf*(1+g)/(r-g);terminal_pv=terminal/(1+r)**int(years)
        out.update(enterprise_value_100m=pv+terminal_pv,forecast_pv_100m=pv,terminal_pv_100m=terminal_pv,
                   terminal_weight_pct=terminal_pv/(pv+terminal_pv)*100,rows=rows)
        notes.append("FCFF 为用户假设；需核验 EBIT、税率、折旧、资本支出与营运资本。")
    elif method=="ev_ebitda":
        out["enterprise_value_100m"]=values["ebitda_100m"]*values["multiple"]
        notes.append("EBITDA 为用户输入；需确认预测期、租赁会计口径与可比倍数。")
    elif method=="ddm":
        r=values["cost_of_equity_pct"]/100;g=values["terminal_growth_pct"]/100
        if r<=g: raise ValueError("股权成本必须大于永续增长率")
        out.update(price=values["dividend_per_share"]*(1+g)/(r-g))
    if out["enterprise_value_100m"] is not None:
        if "equity_bridge_100m" in values:
            out["equity_value_100m"]=out["enterprise_value_100m"]-values["equity_bridge_100m"]
        else: notes.append("未提供净债务等桥接扣减项，仅报告企业价值，暂不计算股权价值或每股价值。")
        notes.append("桥接扣减项 = 净债务 + 少数股东权益 + 优先股 − 其他非经营性资产；负值表示净加回。")
    if out["equity_value_100m"] is not None:
        if "shares_100m" in values: out["price"]=out["equity_value_100m"]/values["shares_100m"]
        else: notes.append("未提供摊薄股本，不计算每股价值。")
    def strings(x):
        if isinstance(x,Decimal): return format(x,"f")
        if isinstance(x,dict): return {k:strings(v) for k,v in x.items()}
        if isinstance(x,list): return [strings(v) for v in x]
        return x
    return strings({"id":uuid.uuid4().hex[:12],"method":method,"method_name":spec["name"],"created_at":store.now(),
                    "assumptions":values,"assumption_quotes":quotes or {},"origin":origin,"fact_ids":fact_ids,
                    "basis_fingerprint":basis_fingerprint(run,fact_ids),
                    "formula":spec["formula"],"output":out,"notes":notes+["情景计算是输入假设的结果，并非投资评级或市场报价。"]})

def save_result(run_id,method,inputs,origin="form",quotes=None):
    result=None
    def change(run):
        nonlocal result
        result=calculate(run,method,inputs,origin,quotes)
        history=run.get("valuation_history",[])
        previous=next((v for v in reversed(history) if v["method"]==method),None)
        if previous and same_result(previous,result):
            result=previous
            return
        if previous:
            changes={}
            for key in ("equity_value_100m","enterprise_value_100m","price"):
                a=previous["output"].get(key);b=result["output"].get(key)
                if a is not None and b is not None:
                    delta=Decimal(b)-Decimal(a)
                    changes[key]={"before":a,"after":b,"delta":str(delta),
                                  "delta_pct":str(delta/abs(Decimal(a))*100) if Decimal(a)!=0 else None}
            result["comparison"]={"previous_id":previous["id"],"output_changes":changes,
                "changed_assumptions":{k:{"before":previous["assumptions"].get(k),"after":result["assumptions"].get(k)}
                    for k in set(previous["assumptions"])|set(result["assumptions"])
                    if previous["assumptions"].get(k)!=result["assumptions"].get(k)}}
        run.setdefault("valuation_history",[]).append(result)
    store.update_run(run_id,change)
    return result


def same_result(previous,current):
    """Repeated submits reuse the last scenario; changed bases still create new versions."""
    def canonical(value):
        if isinstance(value,dict): return {k:canonical(v) for k,v in value.items()}
        if isinstance(value,list): return [canonical(v) for v in value]
        if isinstance(value,(str,int,float)) and not isinstance(value,bool):
            try:
                number=Decimal(str(value))
                if number.is_finite(): return number
            except InvalidOperation: pass
        return value
    return all(canonical(previous.get(k))==canonical(current.get(k))
               for k in ("method","assumptions","fact_ids","output","formula")) and (
        not previous.get('basis_fingerprint') or not current.get('basis_fingerprint') or
        previous['basis_fingerprint']==current['basis_fingerprint'])
