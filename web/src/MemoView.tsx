import {useId,useState} from 'react';
import {ChevronDown,ChevronRight,ExternalLink} from 'lucide-react';
import ResearchText from './ResearchText';
import ReportSyncNotice from './ReportSyncNotice';
import ResearchTracking from './ResearchTracking';

function EvidenceLinks({ids,memo}:any){
  const sources=(ids||[]).map((id:string)=>memo.sources?.find((s:any)=>s.id===id)).filter(Boolean);
  if(!sources.length)return null;
  return <div className="memo-citations">{sources.map((s:any)=>s.url?<a href={s.url} key={s.id} target="_blank" rel="noreferrer">{s.title}<ExternalLink size={11}/></a>:<span key={s.id}>{s.title}{s.kind==='valuation'?' · 假设情景':s.verification?' · '+s.verification.label:s.status==='api_only'?' · 接口单源':''}</span>)}</div>
}
function ReviewRecord({review}:any){
  const labels:Record<string,string>={open:'未解决',resolved:'已复核解决',needs_review:'待复核'};
  const issues=(review.issues||[]).map((issue:any,index:number)=>typeof issue==='string'
    ?{id:`legacy-${index}`,text:issue,status:'needs_review'}
    :{...issue,id:issue.id||`legacy-${index}`,text:issue.text||issue.issue||issue.claim||issue.description||JSON.stringify(issue)});
  return <><p>{review.semantic_status}</p>
    {review.independent_review_performed===false&&<p>本次没有执行独立反方复审，报告修订不等于审阅问题已解决。</p>}
    {issues.length>0?<ul>{issues.map((issue:any)=><li key={issue.id}><strong>{labels[issue.status]||'历史意见，待复核'}：</strong>{issue.text}</li>)}</ul>
      :<p>{review.independent_review_performed?'本次反方审阅未记录具体问题，仍需人工复核。':'当前版本没有可展示的反方意见记录；这不表示已经通过语义复核。'}</p>}
  </>
}
export default function MemoView({run,onDeepResearch,onDiscuss,onSync,onHistory}:any){
  const memo=run?.memo;
  const [sourcesOpen,setSourcesOpen]=useState(false);
  const anchor=useId().replace(/:/g,'');
  if(!memo)return <p>研究报告尚未生成。</p>;
  const deep=memo.schema_version===2;
  const valuationState=run.report_valuation_status;
  return <div className="investment-memo">
    <header className="memo-heading"><div><span className="memo-kicker">{deep?'投资研究备忘录':'财务研究简报'} · 第 {run.report_revision||1} 版</span><h2>{memo.title}</h2></div>{onDeepResearch&&<button className="secondary" onClick={onDeepResearch}>深入研究并更新</button>}</header>
    <ReportSyncNotice run={run} onSync={onSync} onHistory={onHistory}/>
    {memo.stance&&<div className="memo-stance"><strong>研究立场</strong><p>{memo.stance}</p></div>}
    <ResearchText text={memo.summary}/>
    {valuationState&&['changed','unlinked'].includes(valuationState.state)&&<div className="memo-valuation-status" role="note"><strong>估值与报告的关联</strong><p>{valuationState.message}</p></div>}
    {deep&&<nav className="memo-nav" aria-label="报告目录"><strong>目录</strong>{memo.sections.map((s:any)=><a key={s.key} href={'#'+anchor+'-memo-'+s.key}>{s.title}</a>)}</nav>}
    <div className="memo-theses">{memo.theses?.map((t:any,i:number)=><article key={i} className="memo-thesis"><h3>{t.title}</h3><ResearchText text={t.body}/><EvidenceLinks ids={t.evidence_ids} memo={memo}/><div className="memo-challenge"><p><strong>反证与替代解释</strong>{t.counter_evidence}</p><p><strong>何时改变判断</strong>{t.invalidate_if}</p></div></article>)}</div>
    {deep&&memo.sections.map((s:any)=><section id={anchor+'-memo-'+s.key} tabIndex={-1} className="memo-section" key={s.key}><h3>{s.title}{s.status==='gap'&&<span className="badge">证据待补充</span>}</h3><ResearchText text={s.body}/><EvidenceLinks ids={s.evidence_ids} memo={memo}/>{onDiscuss&&<button className="text-link memo-followup" onClick={()=>onDiscuss(`请围绕报告“${s.title}”继续查证：核对关键证据、反证及仍缺失的资料，先说明发现与不确定性。`)}>继续查证此节</button>}</section>)}
    <ResearchTracking run={run} onDiscuss={onDiscuss}/>
    {!!memo.questions?.length&&<section className="memo-section"><h3>核实清单</h3><ul>{memo.questions.map((q:string,i:number)=><li key={i}>{q}{onDiscuss&&<button className="text-link memo-followup" onClick={()=>onDiscuss(`请继续查证：${q}。请给出来源和未能确认的部分。`)}>继续查证</button>}</li>)}</ul></section>}
    {!!memo.sources?.length&&<section className="memo-source-list"><button className="memo-source-toggle" aria-expanded={sourcesOpen} onClick={()=>setSourcesOpen(!sourcesOpen)}>{sourcesOpen?<ChevronDown size={16}/>:<ChevronRight size={16}/>}查阅来源 · {memo.sources.length} 项</button>{sourcesOpen&&memo.sources.map((s:any)=><article key={s.id}><strong>{s.url?<a href={s.url} target="_blank" rel="noreferrer">{s.title}</a>:s.title}</strong><p>{s.kind==='pdf'?'PDF 原文':s.read_status==='body_read'?'已读取网页正文':s.read_status==='search_snippet'?'搜索摘要，未读取正文':s.kind==='valuation'?'用户假设情景':s.verification?s.verification.label:s.status==='api_only'?'接口单源，未双源核验':'计算或财务来源'}{s.published_at?' · 披露日期 '+s.published_at:' · 披露日期待核实'}</p>{s.text&&<details><summary>查阅摘录</summary><p className="source-excerpt">{s.text}</p></details>}</article>)}</section>}
    <details className="memo-limitations"><summary>研究边界与审阅记录</summary><ul>{memo.limitations?.map((v:string,i:number)=><li key={i}>{v}</li>)}</ul>{memo.coverage&&<p>本次检索 {memo.coverage.queries} 条搜索词；研究资料累计包含 {memo.coverage.web_sources} 个网页来源（{memo.coverage.web_bodies} 个已读正文）、{memo.coverage.pdf_pages} 页 PDF。</p>}{memo.review&&<ReviewRecord review={memo.review}/>}</details>
  </div>
}
