import {useEffect,useState} from 'react';
import {Calculator,MessageSquare,ArrowRight,History,Info,LoaderCircle} from 'lucide-react';
import {api,post,decimal} from './api';
import ValuationAnalysis from './ValuationAnalysis';
const inputLabels:any={growth_pct:['预测增速','%'],multiple:['估值倍数','倍'],shares_100m:['摊薄股本','亿股'],book_equity_100m:['归母净资产','亿元'],base_fcff_100m:['基期 FCFF','亿元'],years:['预测期','年'],wacc_pct:['WACC','%'],terminal_growth_pct:['永续增长率','%'],equity_bridge_100m:['股权桥接扣减','亿元'],ebitda_100m:['预测 EBITDA','亿元'],dividend_per_share:['基期每股股利','元/股'],cost_of_equity_pct:['股权成本','%']};
export function AssumptionChips({values}:any){return <div className="assumption-chips">{Object.entries(values).map(([k,v]:any)=><span key={k}>{inputLabels[k]?.[0]||k}：<b>{decimal(v)} {inputLabels[k]?.[1]}</b></span>)}</div>}
export function ValuationResult({result,compact=false}:any){
  const o=result.output;
  return <div className={'valuation-output '+(compact?'compact':'')}><div className="valuation-output-title"><span>{result.method_name}</span><small>{new Date(result.created_at).toLocaleString('zh-CN')}</small></div>
    <div className="valuation-numbers">{o.enterprise_value_100m!=null&&<div><span>企业价值</span><strong>{decimal(o.enterprise_value_100m)}<small>亿元</small></strong></div>}{o.equity_value_100m!=null&&<div><span>股权价值</span><strong>{decimal(o.equity_value_100m)}<small>亿元</small></strong></div>}{o.price!=null&&<div><span>情景每股价值</span><strong>{decimal(o.price)}<small>元 / 股</small></strong></div>}</div>
    <p className="valuation-formula">{result.formula}</p>
    {!compact&&<><AssumptionChips values={result.assumptions}/>
      {result.comparison&&<div className="valuation-comparison"><span className="eyebrow">与同方法上一版相比</span>{Object.entries(result.comparison.changed_assumptions).map(([k,v]:any)=><p key={k}>{inputLabels[k]?.[0]||k}：{decimal(v.before)} → {decimal(v.after)} {inputLabels[k]?.[1]}</p>)}{Object.entries(result.comparison.output_changes).map(([k,v]:any)=><p key={k}>{({equity_value_100m:'股权价值',enterprise_value_100m:'企业价值',price:'每股情景值'} as any)[k]}变化：{Number(v.delta)>0?'+':''}{decimal(v.delta)} {k==='price'?'元/股':'亿元'}{v.delta_pct!=null?'（'+decimal(v.delta_pct)+'%）':''}</p>)}</div>}
      {o.rows&&<div className="projection-table"><table><thead><tr><th>年度</th><th>FCFF（亿元）</th><th>现值（亿元）</th></tr></thead><tbody>{o.rows.map((r:any)=><tr key={r.year}><td>{r.year}</td><td>{decimal(r.fcff_100m)}</td><td>{decimal(r.present_value_100m)}</td></tr>)}</tbody></table><p>终值现值 {decimal(o.terminal_pv_100m)} 亿元 · 占企业价值 {decimal(o.terminal_weight_pct)}%</p></div>}
    </>}
    <div className="valuation-notes">{result.notes?.map((n:string)=><p key={n}>{n}</p>)}</div>
  </div>
}
export default function ValuationLab({run,onUpdated,onDiscuss}:any){
  const [methods,setMethods]=useState<any[]>([]),[method,setMethod]=useState('pe'),[inputs,setInputs]=useState<any>({}),[busy,setBusy]=useState(false),[error,setError]=useState(''),[chosen,setChosen]=useState<string|null>(null);
  useEffect(()=>{api('/runs/'+run.id+'/valuation-options').then(setMethods).catch(e=>setError(e.message))},[run.id,run.updated_at]);
  const spec=methods.find(m=>m.id===method),history=run.valuation_history||[],result=history.find((r:any)=>r.id===chosen)||history.at(-1);
  function select(id:string){setMethod(id);const previous=[...history].reverse().find((r:any)=>r.method===id);setInputs(previous?.assumptions||{});setError('')}
  async function calculate(e:any){e.preventDefault();setBusy(true);setError('');try{const r=await post('/runs/'+run.id+'/valuations',{method,assumptions:Object.fromEntries(Object.entries(inputs).filter(([_,v])=>v!==''))});setChosen(r.id);await onUpdated(run.id)}catch(e:any){setError(e.message)}finally{setBusy(false)}}
  return <div className="content-page valuation-page"><div className="section-heading"><div><span className="eyebrow">VALUATION STUDIO</span><h1>先选方法，再谈价值</h1><p className="small-muted">参数或基数变化会保存新情景；相同输入沿用该方法已有结果。</p></div><button className="primary" onClick={()=>onDiscuss('请结合当前公司的财务数据，比较适合的估值方法，并和我讨论需要补充哪些假设。先不要直接估值。')}><MessageSquare size={15}/>与 Agent 讨论方法</button></div>
    {run.report_valuation_status&&['changed','unlinked'].includes(run.report_valuation_status.state)&&<p className="basis-note" role="status"><Info size={15}/><span>{run.report_valuation_status.message}</span></p>}
    <div className="method-tabs">{methods.map(m=><button key={m.id} className={method===m.id?'selected':''} onClick={()=>select(m.id)}>{m.name.split(' · ')[0]}</button>)}</div>
    {spec&&<div className="valuation-layout"><form className="panel valuation-form" onSubmit={calculate}><h2>{spec.name}</h2><p>{spec.description}</p><div className="basis-note"><Info size={15}/><span>{spec.basis_note}{spec.base_fact&&<b>{spec.base_fact.label}：{decimal(Number(spec.base_fact.value)/1e8)} 亿元 · {spec.base_fact.year}</b>}</span></div>
      <div className="valuation-fields">{spec.fields.map((f:any)=><label key={f.key}>{f.label}<span>{f.unit}{!f.required?' · 可留空':''}</span><input aria-label={f.label} type="number" step={f.key==='years'?'1':'any'} min={f.min??undefined} max={f.max??undefined} required={f.required} placeholder={f.required?'请填入你确认的假设':'未提供'} value={inputs[f.key]??''} onChange={e=>setInputs({...inputs,[f.key]:e.target.value})}/></label>)}</div>
      {['dcf','ev_ebitda'].includes(method)&&<p className="small-muted form-help">桥接扣减项 = 净债务 + 少数股东权益 + 优先股 − 其他非经营性资产。未填时只给企业价值。</p>}
      {error&&<p className="form-error" role="alert">{error}</p>}<div className="button-row"><button className="primary" disabled={busy||['queued','running'].includes(run.state)}>{busy?<LoaderCircle className="spin" size={16}/>:<Calculator size={16}/>}计算并保存情景</button><button type="button" className="text-link" onClick={()=>onDiscuss('我想讨论 '+spec.name+' 是否适合当前公司，以及这些参数应该如何确定。')}>讨论参数<ArrowRight size={14}/></button></div>
    </form><div>{result?<section className="panel"><ValuationResult result={result}/></section>:<section className="panel empty-valuation"><Calculator size={28}/><h2>等待一组有依据的假设</h2><p>可以直接填写，也可以先与 Agent 讨论。缺失参数会明确保留，不会自动填入看似合理的数字。</p></section>}
      <section className="panel valuation-history"><header><History size={17}/><h2>情景记录</h2><span>{history.length} 份</span></header>{[...history].reverse().map((v:any)=><button key={v.id} className={result?.id===v.id?'selected':''} onClick={()=>{setChosen(v.id);setMethod(v.method);setInputs(v.assumptions)}}><span><strong>{v.method_name}</strong><small>{v.origin!=='form'?'通过研究对话':'手动输入'} · {new Date(v.created_at).toLocaleTimeString('zh-CN')}</small></span><b>{decimal(v.output.equity_value_100m??v.output.enterprise_value_100m??v.output.price)}<small>{v.method==='ddm'?'元/股':'亿元'}</small></b></button>)}{!history.length&&<p className="small-muted pad">尚未保存估值情景</p>}</section>
    </div></div>}
    <ValuationAnalysis key={run.id} run={run} currentScenario={result} onUpdated={onUpdated} onDiscuss={onDiscuss}/>
  </div>
}
