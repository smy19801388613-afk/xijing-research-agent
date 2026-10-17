"""Saved, reproducible sensitivity grids and explicitly unverified peer inputs.

All calculations reuse valuation.calculate. Nothing in this module applies a
multiple, changes the user's scenario, or turns a scenario envelope into a target.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import store, valuation

AXES={
    'pe':('growth_pct','预测增速（%）','multiple','PE（倍）'),
    'ps':('growth_pct','预测增速（%）','multiple','PS（倍）'),
    'pb':('book_equity_100m','归母净资产（亿元）','multiple','PB（倍）'),
    'dcf':('wacc_pct','WACC（%）','terminal_growth_pct','永续增长（%）'),
    'ev_ebitda':('ebitda_100m','预测 EBITDA（亿元）','multiple','EV / EBITDA（倍）'),
    'ddm':('cost_of_equity_pct','股权成本（%）','terminal_growth_pct','永续增长（%）'),
}
ENVELOPE_NOTE='所选轴值下有效情景的最小—最大包络；不是概率置信区间、投资目标价或市场报价。'


class StrictRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')


class SensitivityRequest(StrictRequest):
    base_scenario_id:str=Field(min_length=1,max_length=100)
    axis_x:list[str|int|float]=Field(min_length=2,max_length=9)
    axis_y:list[str|int|float]=Field(min_length=2,max_length=9)

    @field_validator('axis_x','axis_y',mode='before')
    @classmethod
    def reject_boolean(cls,value):
        if isinstance(value,list) and any(isinstance(v,bool) for v in value):
            raise ValueError('轴值必须是有限数字，不能使用布尔值')
        return value


class ComparableRow(StrictRequest):
    company:str=Field(min_length=1,max_length=120)
    ticker:str=Field(default='',max_length=40)
    multiple:str|int|float|None=None
    price:str|int|float|None=None
    denominator_per_share:str|int|float|None=None
    multiple_basis:str=Field(default='',max_length=2000)
    price_basis:str=Field(default='',max_length=2000)
    denominator_basis:str=Field(default='',max_length=2000)
    source_ids:list[str]=Field(default_factory=list,max_length=15)
    note:str=Field(default='',max_length=2000)
    method:Literal['pe','ps','pb']|None=None
    period_kind:Literal['TTM','FY','forward']|None=None
    period_label:str|None=None
    valuation_date:date|None=None
    currency:str|None=None

    @field_validator('multiple','price','denominator_per_share',mode='before')
    @classmethod
    def no_boolean(cls,value):
        if isinstance(value,bool): raise ValueError('数值不能使用布尔值')
        return value


class ComparableRequest(StrictRequest):
    method:Literal['pe','ps','pb']
    period_kind:Literal['TTM','FY','forward']
    period_label:str=Field(min_length=4,max_length=10)
    valuation_date:date
    currency:str=Field(pattern=r'^[A-Z]{3}$')
    rows:list[ComparableRow]=Field(min_length=1,max_length=30)


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def _number(value,label):
    try:
        if isinstance(value,bool) or len(str(value))>80: raise InvalidOperation()
        result=Decimal(str(value))
        if not result.is_finite(): raise InvalidOperation()
        if result and (result.adjusted() < -12 or result.adjusted() > 12): raise InvalidOperation()
        if abs(result)>Decimal('1e12'): raise InvalidOperation()
        return result
    except (ValueError,InvalidOperation): raise ValueError(label+'必须是有限数字，非零绝对值介于 1e-12 与 1e12，且输入不超过 80 字符') from None


def _text(value):
    return format(value.normalize(),'f')


def _axis(values,label):
    result=[_number(v,label) for v in values]
    if len(set(result))!=len(result): raise ValueError(label+'不能包含重复值')
    return [_text(v) for v in sorted(result)]


def _scenario(run,identity):
    base=next((s for s in run.get('valuation_history',[]) if s['id']==identity),None)
    if not base: raise ValueError('未找到本研究的基准估值情景，请先计算并保存情景')
    current=valuation.calculate(run,base['method'],base['assumptions'])
    if not valuation.same_result(base,current):
        raise ValueError('基准情景的财务基数或来源已变化，请先重新计算并保存估值情景，旧版本继续保留')
    return base,current


def _scenario_fingerprint(base):
    return _hash({k:base.get(k) for k in ('method','assumptions','fact_ids','output','formula')})


def calculate_sensitivity(run,request):
    req=request if isinstance(request,SensitivityRequest) else SensitivityRequest.model_validate(request)
    base,current=_scenario(run,req.base_scenario_id)
    x_key,x_label,y_key,y_label=AXES[base['method']]
    xs=_axis(req.axis_x,x_label);ys=_axis(req.axis_y,y_label)
    cells=[]
    for y in ys:
        row=[]
        for x in xs:
            inputs={**base['assumptions'],x_key:x,y_key:y}
            try:
                result=valuation.calculate(run,base['method'],inputs,origin='sensitivity')
                row.append({'x':x,'y':y,'valid':True,'output':result['output']})
            except ValueError as e:
                row.append({'x':x,'y':y,'valid':False,'error':str(e),'output':None})
        cells.append(row)
    valid=[c for row in cells for c in row if c['valid']]
    envelope={}
    for key in ('equity_value_100m','enterprise_value_100m','price'):
        values=[Decimal(c['output'][key]) for c in valid if c['output'].get(key) is not None]
        if values: envelope[key]={'min':_text(min(values)),'max':_text(max(values))}
    notes=[ENVELOPE_NOTE,'除两个轴参数外，其余参数均固定为基准情景；无效组合逐格排除。']
    if not base.get('basis_fingerprint'):
        notes.append('历史基准未记录来源指纹；已对当前基数重算并核对原结果，从本版起冻结来源。')
    if not valid: notes.append('所有组合均无效，未形成情景包络；请调整轴值。')
    if any(Decimal(c['output']['equity_value_100m'])<0 for c in valid if c['output'].get('equity_value_100m') is not None):
        notes.append('部分情景股权价值为负，需核查现金流与桥接项，不代表可交易的负股价。')
    return {'id':'sensitivity-'+uuid.uuid4().hex[:12],'artifact':'valuation_analysis','analysis_kind':'sensitivity',
        'created_at':store.now(),'method':base['method'],'base_scenario_id':base['id'],
        'base_fingerprint':current['basis_fingerprint'],'base_scenario_fingerprint':_scenario_fingerprint(base),
        'base_snapshot':deepcopy(base),'axes':{'x':{'key':x_key,'label':x_label,'values':xs},'y':{'key':y_key,'label':y_label,'values':ys}},
        'fixed_assumptions':{k:v for k,v in base['assumptions'].items() if k not in (x_key,y_key)},
        'cells':cells,'envelope':envelope,'valid_cells':len(valid),'invalid_cells':len(xs)*len(ys)-len(valid),'notes':notes}


def save_sensitivity(run_id,request):
    result=None
    def change(run):
        nonlocal result
        result=calculate_sensitivity(run,request)
        history=run.setdefault('valuation_sensitivity_history',[])
        result['revision']=len(history)+1
        history.append(result)
    store.update_run(run_id,change)
    return result


def _sources(run):
    return {s['id']:s for s in run.get('evidence',[])+run.get('research_sources',[]) if s.get('id')}


def calculate_comparables(run,request):
    req=request if isinstance(request,ComparableRequest) else ComparableRequest.model_validate(request)
    if run.get('state') in ('queued','running'): raise ValueError('请先完成财务研究')
    group=req.model_dump(mode='json',exclude={'rows'})
    if req.period_kind=='TTM':
        try: end=date.fromisoformat(req.period_label)
        except ValueError: raise ValueError('TTM 期间须填滚动截止日 YYYY-MM-DD') from None
        if end>req.valuation_date: raise ValueError('TTM 截止日不能晚于估值日')
    else:
        if not re.fullmatch(r'20\d{2}',req.period_label): raise ValueError('FY / forward 期间须填四位财年，例如 2025')
        if req.period_kind=='FY' and int(req.period_label)>req.valuation_date.year:
            raise ValueError('历史 FY 财年不能晚于估值日所属年份；预测口径请选 forward')
    cutoff=run.get('request',{}).get('as_of')
    if cutoff and req.valuation_date.isoformat()>str(cutoff)[:10]: raise ValueError('估值日不能晚于研究截止日')
    sources=_sources(run);rows=[];seen=set()
    for item in req.rows:
        row=item.model_dump(mode='json')
        row['company']=row['company'].strip();row['ticker']=row['ticker'].strip().upper()
        if not row['company']: raise ValueError('可比公司名称不能为空')
        for key in group:
            if row.get(key) is not None and row[key]!=group[key]:
                raise ValueError('同一可比组不能混合方法、期间、估值日或货币：'+row['company']+' 的 '+key+' 不一致')
        row.update(group)
        identity=row['ticker'] or re.sub(r'\s+','',row['company']).casefold()
        if identity in seen: raise ValueError('可比公司重复：'+row['company'])
        seen.add(identity)
        for sid in row['source_ids']:
            if sid not in sources: raise ValueError('来源不属于当前研究：'+sid)
            published=sources[sid].get('published_at') or sources[sid].get('announced_date')
            if published and re.sub(r'[^0-9]','',str(published))[:8]>req.valuation_date.strftime('%Y%m%d'):
                raise ValueError('来源披露日晚于估值日：'+sid)
        row['source_refs']=[{'id':sid,'title':sources[sid].get('title') or sources[sid].get('label') or sid,
            'url':sources[sid].get('url'),'sha256':_hash(sources[sid])} for sid in dict.fromkeys(row['source_ids'])]
        row['verification']='user_provided';row['verification_label']='用户提供，数值与可比性待核实'
        supplied=_number(row['multiple'],'倍数') if row['multiple'] not in (None,'') else None
        price=_number(row['price'],'股价') if row['price'] not in (None,'') else None
        denominator=_number(row['denominator_per_share'],'每股财务分母') if row['denominator_per_share'] not in (None,'') else None
        reason=None;calculated=None
        if row['ticker'] and row['ticker']==str(run.get('request',{}).get('ticker','')).upper(): reason='研究对象自身不纳入同业样本'
        elif denominator is not None and denominator<=0:
            reason=('亏损或零盈利，不适用 PE' if req.method=='pe' else '财务分母非正，不适用该倍数')
        elif price is not None and price<=0: reason='股价必须为正'
        elif supplied is not None and supplied<=0: reason='倍数非正，排除统计'
        elif supplied is None and (price is None or denominator is None): reason='缺少倍数或完整股价与每股分母，排除统计'
        elif supplied is not None and not row['multiple_basis'].strip(): reason='缺少倍数来源与口径说明，排除统计'
        elif (price is not None or denominator is not None) and (price is None or denominator is None): reason='股价与每股分母须成对提供，排除统计'
        elif price is not None and (not row['price_basis'].strip() or not row['denominator_basis'].strip()): reason='缺少股价或每股分母依据，排除统计'
        if not reason and price is not None:
            calculated=price/denominator
            if supplied is not None and abs(calculated-supplied)>max(Decimal('.01'),abs(calculated)*Decimal('.005')):
                reason='提供倍数与股价 / 每股分母不一致（超过 0.5% 或 0.01 倍容差），排除统计'
        value=calculated if calculated is not None else supplied
        row.update(included=reason is None,exclusion_reason=reason,multiple_value=_text(value) if value is not None else None,
                   calculation='股价 / 每股财务分母' if calculated is not None else '用户直接提供倍数')
        rows.append(row)
    values=sorted(Decimal(r['multiple_value']) for r in rows if r['included'])
    n=len(values)
    stats={'count':n,'excluded_count':len(rows)-n,'median':_text((values[(n-1)//2]+values[n//2])/2) if n else None,
           'min':_text(values[0]) if n else None,'max':_text(values[-1]) if n else None}
    notes=['可比数据由用户提供；关联已有来源只表示可追溯，不代表数值、期间及业务可比性已核实。',
           '中位数与范围仅供讨论，不自动采用倍数，不改动任何估值参数；同口径不等于经济可比。']
    if n<3: notes.append('有效样本少于 3 家，代表性不足；不可据此作稳健行业倍数结论。')
    if req.method=='pb': notes.append('PB 分母须为估值对应报告期末归母净资产 / 摊薄股数；TTM 标签仅作分组，不表示净资产可滚动相加。')
    return {'id':'comparables-'+uuid.uuid4().hex[:12],'artifact':'valuation_analysis','analysis_kind':'comparables','created_at':store.now(),
            **group,'rows':rows,'statistics':stats,'notes':notes,'input_fingerprint':_hash({'group':group,'rows':rows})}


def save_comparables(run_id,request):
    result=None
    def change(run):
        nonlocal result
        result=calculate_comparables(run,request)
        history=run.setdefault('comparable_analyses',[])
        result['revision']=len(history)+1
        history.append(result)
    store.update_run(run_id,change)
    return result


def summary(run):
    sensitivities=[]
    for item in run.get('valuation_sensitivity_history',[]):
        view=deepcopy(item)
        try:
            base,current=_scenario(run,item['base_scenario_id'])
            view['stale']=(current['basis_fingerprint']!=item['base_fingerprint'] or
                           _scenario_fingerprint(base)!=item['base_scenario_fingerprint'])
        except ValueError: view['stale']=True
        view['status_note']='基数或来源已变化，保留历史结果，请新建基准情景后重算' if view['stale'] else '当前基数与本版基准一致'
        sensitivities.append(view)
    sources=_sources(run)
    peers=[]
    for item in run.get('comparable_analyses',[]):
        view=deepcopy(item)
        view['source_changed']=any(ref['id'] not in sources or _hash(sources[ref['id']])!=ref['sha256'] for row in item['rows'] for ref in row.get('source_refs',[]))
        peers.append(view)
    return {'sensitivities':sensitivities,'comparables':peers,'available_sources':[
        {'id':s['id'],'title':s.get('title') or s.get('label') or s['id'],'url':s.get('url')} for s in sources.values()],
        'notes':[ENVELOPE_NOTE,'保存可比依据不会自动更新估值方案，需与用户讨论并确认。']}


def markdown_sections(run,audit=False):
    data=summary(run);lines=[]
    if data['sensitivities']:
        lines+=['## 估值敏感性与情景包络','',ENVELOPE_NOTE,'']
        for item in data['sensitivities'] if audit else data['sensitivities'][-1:]:
            lines += [f"### {item['method'].upper()} · 敏感性第 {item['revision']} 版",'',
                f"基准情景：{item['base_scenario_id']}；{item['status_note']}。",'',
                '| 指标 | 最小值 | 最大值 |','|---|---:|---:|']
            for key,label in (('equity_value_100m','股权价值（亿元）'),('enterprise_value_100m','企业价值（亿元）'),('price','每股情景值（元）')):
                if key in item['envelope']:
                    envelope=item['envelope'][key];lines.append(f"| {label} | {envelope['min']} | {envelope['max']} |")
            lines+=['',f"有效组合 {item['valid_cells']}，无效组合 {item['invalid_cells']}；固定其余参数。",
                f"横轴 {item['axes']['x']['label']}："+'、'.join(item['axes']['x']['values']),
                f"纵轴 {item['axes']['y']['label']}："+'、'.join(item['axes']['y']['values']),
                '固定参数：'+'；'.join(f'{k}={v}' for k,v in item['fixed_assumptions'].items()),
                f"来源指纹：{item['base_fingerprint']}",'']
            measure=next((k for k in ('price','equity_value_100m','enterprise_value_100m') if k in item['envelope']),None)
            if measure:
                lines += [f"矩阵指标：{measure}",'',
                    '| '+item['axes']['y']['label']+' / '+item['axes']['x']['label']+' | '+' | '.join(item['axes']['x']['values'])+' |',
                    '|---|'+'---:|'*len(item['axes']['x']['values'])]
                for y,row in zip(item['axes']['y']['values'],item['cells']):
                    lines.append('| '+y+' | '+' | '.join(_text(Decimal(c['output'][measure])) if c['valid'] else '无效：'+c['error'] for c in row)+' |')
                lines+=['']
    if data['comparables']:
        lines+=['## 可比公司依据','']
        for item in data['comparables'] if audit else data['comparables'][-1:]:
            stats=item['statistics']
            lines += [f"{item['method'].upper()} / {item['period_kind']} {item['period_label']} / {item['valuation_date']} / {item['currency']}",'',
                f"有效 {stats['count']} 家，排除 {stats['excluded_count']} 家；中位数 {stats['median'] or '缺失'} 倍，范围 {stats['min'] or '缺失'}—{stats['max'] or '缺失'} 倍。",'',
                '| 公司 | 倍数 | 纳入情况 | 依据 / 来源 ID |','|---|---:|---|---|']
            for row in item['rows']:
                safe=lambda x:str(x or '').replace('|',' / ').replace('\n',' ')
                basis=row.get('multiple_basis') or ('股价：'+row.get('price_basis','')+'；分母：'+row.get('denominator_basis',''))
                lines.append(f"| {safe(row['company'])} | {row['multiple_value'] or '缺失'} | {safe(row['exclusion_reason'] or '用户提供，待核实')} | {safe(basis)} / {safe(', '.join(row['source_ids']))} |")
            lines+=['',*item['notes'],'']
    return '\n'.join(lines)


router=APIRouter(prefix='/api/runs/{run_id}',tags=['valuation-analysis'])


@router.get('/valuation-analysis')
def get_analysis(run_id:str): return summary(store.get_run(run_id))


@router.post('/valuation-sensitivities')
def create_sensitivity(run_id:str,request:SensitivityRequest): return save_sensitivity(run_id,request)


@router.post('/valuation-comparables')
def create_comparables(run_id:str,request:ComparableRequest): return save_comparables(run_id,request)
