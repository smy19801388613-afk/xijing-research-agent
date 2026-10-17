import {useEffect,useRef,useState} from 'react';
import {api,post} from './api';
import './research-tracking.css';

const assessmentLabels:Record<string,string>={supporting:'支持原论点',challenging:'挑战原论点',inconclusive:'仍无法判断'};

export default function ResearchTracking({run,onDiscuss}:any){
  const metrics=run?.memo?.tracking_metrics||[];
  const [snapshot,setSnapshot]=useState<any>(null);
  const [error,setError]=useState('');
  const [active,setActive]=useState('');
  const [saving,setSaving]=useState(false);
  const currentTarget=useRef('');
  currentTarget.current=`${run?.id}:${run?.report_revision}`;
  const [draft,setDraft]=useState({source_id:'',quote:'',observation:'',assessment:'inconclusive'});
  useEffect(()=>{
    let alive=true;
    setSnapshot(null);setActive('');setError('');
    if(run?.id)api(`/runs/${run.id}/tracking`).then(v=>{if(alive)setSnapshot(v)}).catch(e=>{if(alive)setError(e.message)});
    return()=>{alive=false};
  },[run?.id,run?.report_revision,run?.memo]);
  const sources=Array.from(new Map([...(run.research_sources||[]),...(run.memo.sources||[])].map((s:any)=>[s.id,s])).values()) as any[];
  const currentVersion=snapshot&&snapshot.report_identity?.revision===run.report_revision;
  const observations=(currentVersion?snapshot?.observations:snapshot?.previous_report_observations?.filter((o:any)=>o.report_identity?.revision===run.report_revision))||[];
  const pending=currentVersion?(snapshot?.pending_observations||[]):[];
  async function save(metric:any){
    if(!snapshot||!currentVersion)return;
    const target=currentTarget.current;
    setSaving(true);setError('');
    try{
      const next=await post(`/runs/${run.id}/tracking/${encodeURIComponent(metric.id)}/observations`,{...draft,report_identity:snapshot.report_identity});
      if(currentTarget.current!==target)return;
      setSnapshot(next);setActive('');setDraft({source_id:'',quote:'',observation:'',assessment:'inconclusive'});
    }catch(e:any){if(currentTarget.current===target)setError(e.message)}finally{setSaving(false)}
  }
  return <section className="memo-section research-tracking" aria-label="持续跟踪指标">
    <div className="tracking-heading"><h3>持续跟踪指标</h3><span>{metrics.length} 项 · 第 {run.report_revision||1} 版</span></div>
    <p className="tracking-explainer">更新安排是研究计划。新观察单独留存，原报告与论点保留。</p>
    {error&&<p role="alert" className="tracking-error">{error}</p>}
    {!!pending.length&&<div className="tracking-pending" role="note"><strong>{pending.length} 条观察尚未纳入整稿研究</strong><p>修改标题或局部文字不会处理这些观察。纳入研究后，仍需核对支持关系和未解决的问题。</p><details><summary>查看待研究的观察</summary>{pending.map((o:any)=><article key={o.id}><strong>{o.metric_snapshot?.name} · {assessmentLabels[o.assessment]}</strong><p>{o.observation}</p><blockquote>{o.quote}</blockquote><p>{o.source_snapshot?.title||o.source_id}</p><span>记录于报告第 {o.report_identity?.revision||'—'} 版</span></article>)}</details></div>}
    {!metrics.length&&<><p>此版本尚未建立跟踪指标。</p>{onDiscuss&&<button className="text-link" onClick={()=>onDiscuss('请基于当前报告和实际读过的证据，补充持续跟踪指标：关联论点、口径、基线及来源、更新条件、支持与推翻条件和后续动作。基线缺失明确说明，数值阈值未获我确认前标为假设；保存前检查与原报告的一致性。')}>讨论并建立跟踪计划</button>}</>}
    {!!metrics.length&&<div className="tracking-table-wrap"><table className="tracking-table"><thead><tr><th scope="col">指标与论点</th><th scope="col">当前基线</th><th scope="col">判断条件</th><th scope="col">更新与动作</th></tr></thead><tbody>{metrics.map((metric:any)=>{
      const baseline=metric.baseline||{};
      const source=sources.find((s:any)=>s.id===baseline.source_id);
      return <tr key={metric.id}><td><strong>{metric.name}</strong><p>{metric.definition}</p><small>{run.memo.theses?.some((t:any)=>t.title===metric.thesis)?'关联：':'原报告论点关联，待复核：'}{metric.thesis}</small></td>
        <td><span className={'tracking-status '+(baseline.status==='missing'?'is-missing':'')}>{baseline.status==='missing'?'基线待补':'已有基线'}</span><p>{baseline.value}</p>{baseline.period&&<small>{baseline.period}</small>}{baseline.quote&&<details><summary>查看原文依据</summary><blockquote>{baseline.quote}</blockquote>{source?.url?<a href={source.url} target="_blank" rel="noreferrer">{source.title||baseline.source_id}</a>:<small>{source?.title||baseline.source_id}</small>}</details>}</td>
        <td><p><b>支持：</b>{metric.support_condition}</p><p><b>推翻：</b>{metric.invalidate_condition}</p>{metric.threshold_basis==='unconfirmed_assumption'&&<small className="tracking-assumption">研究员建议阈值，未获用户确认</small>}</td>
        <td><strong>{metric.frequency}</strong><p>{metric.next_check}</p><p>{metric.next_action}</p>{onDiscuss&&<button className="text-link" onClick={()=>onDiscuss(`请继续核查跟踪指标“${metric.name}”（ID ${metric.id}，报告第 ${run.report_revision||1} 版）：${metric.definition}。查阅新证据并说明对原论点“${metric.thesis}”的影响，记录独立观察；不要自动覆盖已保存报告或确认数值阈值。`)}>继续核查</button>}<button className="text-link tracking-record" disabled={!currentVersion||saving} onClick={()=>{setActive(active===metric.id?'':metric.id);setError('');setDraft({source_id:'',quote:'',observation:'',assessment:'inconclusive'})}}>记录观察</button></td>
      </tr>
    })}</tbody></table></div>}
    {active&&<form className="tracking-observation-form" onSubmit={e=>{e.preventDefault();save(metrics.find((m:any)=>m.id===active))}}><h4>记录观察 · {metrics.find((m:any)=>m.id===active)?.name}</h4>
      <label>已查阅来源<select value={draft.source_id} required onChange={e=>setDraft({...draft,source_id:e.target.value,quote:''})}><option value="">选择来源</option>{sources.map((s:any)=><option value={s.id} key={s.id}>{s.title||s.id}</option>)}</select></label>
      <label>连续原文摘录<textarea required maxLength={2000} value={draft.quote} onChange={e=>setDraft({...draft,quote:e.target.value})} placeholder="复制实际查阅的原文，保留原值与单位"/></label>
      <label>观察与解释<textarea required maxLength={2000} value={draft.observation} onChange={e=>setDraft({...draft,observation:e.target.value})} placeholder="这条证据如何影响原论点？尚存哪些不确定性？"/></label>
      <label>对原论点的影响<select value={draft.assessment} onChange={e=>setDraft({...draft,assessment:e.target.value})}>{Object.entries(assessmentLabels).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>
      <div className="tracking-form-actions"><button type="submit" className="primary" disabled={saving}>{saving?'保存中…':'保存独立观察'}</button><button type="button" className="secondary" disabled={saving} onClick={()=>setActive('')}>取消</button></div>
    </form>}
    {!!observations.length&&<details className="tracking-observations"><summary>本报告版本的观察记录 · {observations.length} 条</summary>{observations.map((o:any)=><article key={o.id}><strong>{o.metric_snapshot?.name} · {assessmentLabels[o.assessment]}</strong><p>{o.observation}</p><blockquote>{o.quote}</blockquote><small>{o.source_snapshot?.title||o.source_id} · {new Date(o.recorded_at).toLocaleString('zh-CN')} · 支持关系待复核</small></article>)}</details>}
    {snapshot?.previous_report_observations?.length>0&&currentVersion&&<p className="tracking-explainer">另有 {snapshot.previous_report_observations.length} 条观察绑定于旧报告版本，保留在研究记录中。</p>}
    {snapshot&&!currentVersion&&<p className="tracking-explainer">当前展示的是历史报告。请在最新报告中记录新观察。</p>}
  </section>
}
