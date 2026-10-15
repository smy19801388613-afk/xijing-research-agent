import {useEffect,useRef,useState} from 'react';
import {ArrowUp,BookOpen,Check,ChevronDown,ChevronRight,FilePenLine,Layers3,LoaderCircle,MessageSquare,Plus,Search,SlidersHorizontal,Square,User,AlertCircle,Trash2,FileText,Globe2,Sparkles,ExternalLink} from 'lucide-react';
import {api,post} from './api';
import {ValuationResult,AssumptionChips} from './ValuationLab';
import ResearchDocuments,{DocumentLinks} from './ResearchDocuments';
import ResearchText from './ResearchText';
import ExecutionBudget,{defaultBudget,normalizeBudget} from './ExecutionBudget';
const toolNames:any={plan_research_questions:'建立研究问题',update_research_question:'保存问题证据',search_company:'查找公司',start_research:'建立财务底稿',fetch_annual_report:'获取公开年报',attach_document:'接入研究材料',read_document:'查阅 PDF 原文',search_documents:'检索披露原文',web_search:'联网搜索',search_web:'联网搜索',read_webpage:'阅读网页',read_public_pdf:'查阅公开公告PDF',read_web_page:'阅读网页',read_web:'阅读网页',build_investment_memo:'撰写深度研究',refine_investment_memo:'依据原文修订报告',valuation_options:'比较估值方法',calculate_valuation:'计算估值情景',propose_valuation:'提出估值方案',calculate_proposal:'计算选定方案',revise_report:'修订研究报告'};
const deepResearchPrompt='请围绕当前公司开展深度买方研究：联网搜索行业、竞争格局、最新经营进展与风险，阅读关键原文并结合财务底稿、年报，检验投资论点和反证，讨论估值适用条件、催化剂与判断失效条件，最后生成并保存完整研究备忘录。明确资料日期、来源、推断和未解决的问题；缺失数据不要编造。';
function sourceKey(url:string){try{const u=new URL(url);u.protocol='https:';u.hash='';Array.from(u.searchParams.keys()).filter(k=>k.startsWith('utm_')).forEach(k=>u.searchParams.delete(k));return u.toString().replace(/\/$/,'')}catch{return url}}
function observedSources(steps:any[],message=''){
  const sources=new Map<string,any>();
  for(const step of steps){const r=step.result||{},read=['read_webpage','read_web_page','read_public_pdf'].includes(step.tool);
    let rows=step.tool==='web_search'||step.tool==='read_public_pdf'?r.results||[]:read?[r]:step.tool==='build_investment_memo'?r.observed_sources||[]:[];
    if(read&&!rows.length)rows=[{url:step.arguments?.url}];
    for(const s of rows){const url=s.url||step.arguments?.url;if(!/^https?:\/\//i.test(url||''))continue;
      const body=(read||step.tool==='build_investment_memo')&&!['failed','cancelled'].includes(step.state)&&!!s.text?.trim()&&!r.error&&!['error','excluded','excluded_after_cutoff','needs_ocr','document_link'].includes(r.status)&&!['failed','excluded','pdf_link_only','search_snippet','snippet_only'].includes(s.read_status);
      const key=sourceKey(url),prior=sources.get(key),status=body?'body_read':read?'failed':'search_snippet';
      if(prior?.read_status==='body_read'||(prior&&status==='search_snippet'))continue;
      sources.set(key,{...prior,...s,url,read_status:status,cited:message.includes(url)});
    }
  }
  return Array.from(sources.values()).sort((a,b)=>Number(b.read_status==='body_read')-Number(a.read_status==='body_read')||Number(b.cited)-Number(a.cited));
}
function turnCoverage(chat:any,turnId:string){
  if(chat?.coverage_views?.[turnId])return chat.coverage_views[turnId];
  if(chat?.turn_coverage?.[turnId])return chat.turn_coverage[turnId];
  const steps=(chat?.steps||[]).filter((s:any)=>s.turn_id===turnId),text=(chat?.messages||[]).find((m:any)=>m.turn_id===turnId&&m.role==='user')?.content||'',sources=observedSources(steps);
  const body=sources.filter(s=>s.read_status==='body_read').length,required=text.split(/[。；，,、\n]/).some((c:string)=>/(?:打开|读取|阅读|查阅|核对).{0,10}(?:正文|原文|全文)/.test(c)&&!/(?:不|无需|别).{0,8}(?:打开|读取|阅读|查阅|核对)/.test(c)),pages=steps.filter((s:any)=>s.tool==='read_document'&&s.state==='completed').reduce((n:number,s:any)=>n+(s.result?.snippets?.filter((p:any)=>p.text?.trim()).length||0),0);
  const unknown=steps.some((s:any)=>s.tool==='build_investment_memo'&&!s.result?.observed_sources);
  return {search_calls:steps.filter((s:any)=>s.tool==='web_search'&&s.state==='completed').length,search_results:steps.filter((s:any)=>s.tool==='web_search').reduce((n:number,s:any)=>n+(s.result?.results?.length||0),0),body_sources:body,document_pages:pages,status:unknown?'unknown':required&&!body&&!pages?'partial':'observed',sources,reused_cited_sources:null};
}
function Coverage({value}:any){
  if(!value)return null;
  const task=value.task_coverage||value,unknown=value.telemetry_incomplete||value.status==='unknown';
  const active=value.search_calls||value.body_sources||value.document_pages||value.reused_cited_sources;
  if(!active&&!task.search_calls&&!task.body_sources&&!task.telemetry_incomplete&&!['partial','unknown'].includes(value.status))return null;
  const noNewRead=!value.body_sources&&!value.document_pages;
  return <div className={'turn-coverage '+value.status} role="status">
    <strong>{unknown?'资料活动记录不完整':'本轮检索与阅读'}</strong>
    {unknown?<span>旧版深度研究漏记了内部检索，无法还原精确次数；零条记录不代表没有执行。</span>:<span>搜索 {value.search_calls} 次 · 结果 {value.search_results} 条 · 网络原文 {value.body_sources} 篇 · 本地读页 {value.document_pages}</span>}
    {value.task_coverage&&(value.task_turn_count>1||task.telemetry_incomplete)&&<small>{task.telemetry_incomplete?'任务累计：旧版记录不完整，无法还原完整检索与阅读次数。':`任务累计（含续查）：搜索 ${task.search_calls} 次 · 网络原文 ${task.body_sources} 篇 · 本地读页 ${task.document_pages}`}</small>}
    {noNewRead&&!unknown&&<small>{value.reused_cited_sources?`本轮引用已有材料 ${value.reused_cited_sources} 条，未重新读取。`:value.saved_body_sources?`本轮未新增原文查阅，当前资料库已有 ${value.saved_body_sources} 条可读来源；仍需核对与问题的对应关系。`:'本轮尚未取得可用原文，搜索摘要只能作为线索。'}</small>}
    {value.question_coverage&&<small>研究问题：{value.question_coverage.counts.supported} / {value.question_coverage.total} 项有证据回答，语义判断待复核。</small>}
    {!!value.failed_sources&&<small>{value.failed_sources} 个来源未能读取正文。</small>}
  </div>
}
function WebReferences({sources}:any){return <div className="web-references">{sources.filter((s:any)=>/^https?:\/\//i.test(s.url||'')).map((s:any,i:number)=><a href={s.url} target="_blank" rel="noopener noreferrer" key={s.id||s.url||i}><Globe2 size={15}/><span>{s.title||s.url}<small>{s.published_at||'日期未确认'}{s.read_status?' · '+(({read:'已读原文',article_text:'已读原文',public_pdf_text:'已读PDF原文',pdf_link_only:'PDF链接，未读正文',failed:'未取得正文',completed:'已读原文',body_read:'已读原文',snippet_only:'搜索摘要',search_snippet:'搜索摘要'} as any)[s.read_status]||'待查阅'):''}</small></span><ExternalLink size={12}/></a>)}</div>}
const constraintNames:any={saved_only:'只用已保存材料',no_network:'不联网',no_report:'不修改报告',no_calculation:'不计算估值',no_valuation:'不做估值',require_body:'核对原文',no_tools:'不调用工具',no_question_writes:'不修改问题记录'};
function ConstraintChips({value}:any){
 if(!value)return null;
 const persistent=value.persistent_labels||Object.entries(value.persistent_rules||{}).filter(([key,v])=>Boolean(v)&&constraintNames[key]).map(([key])=>constraintNames[key]);
 const labels=value.labels||[];
 if(!labels.length&&!persistent.length)return null;
 return <div className="effective-constraints" aria-label="当前有效约束"><span className="constraint-scope">{value.scope==='continuation'?'续查沿用':'本轮约束'}</span>{labels.map((label:string,i:number)=><span className="constraint-chip" key={i}>{label}</span>)}{!!persistent.length&&<details><summary>持续约束 · {persistent.length} 项</summary><div>{persistent.map((label:string)=><span className="constraint-chip" key={label}>{label}</span>)}</div></details>}</div>
}
function Delivery({value,onResume,canResume,disabled,turnId}:any){
 if(!value)return null;
 const incomplete=value.status!=='completed',completed=value.completed||[],pending=value.pending||[];
 const savedThisTurn=completed.some((s:any)=>s.turn_id===turnId&&['build_investment_memo','refine_investment_memo','revise_report','start_research'].includes(s.tool));
 const reportChanged=value.report_changed&&(!turnId||savedThisTurn),revision=value.report_revision_after??value.report_revision_before;
 if(!incomplete&&!reportChanged)return null;
 const titles:any={completed:'本轮已完成',partial:'已保留进展，仍有事项未完成',failed:'本轮未完成',cancelled:'已停止，进展已保留'};
 return <section className={'turn-delivery '+value.status} aria-label="本轮执行结果"><div className="delivery-heading">{incomplete?<AlertCircle size={17}/>:<Check size={17}/>}<strong>{titles[value.status]||'本轮执行结果'}</strong></div>{reportChanged?<p>报告已更新至第 {revision} 版。</p>:revision!=null&&<p>沿用第 {revision} 版报告，本轮未保存新版本。</p>}{!!completed.length&&<details><summary>任务累计已完成 {completed.length} 个步骤</summary><ul>{completed.map((step:any,i:number)=><li key={step.step_id||i}>{step.label||toolNames[step.tool]||step.tool}</li>)}</ul></details>}{!!pending.length&&<div className="delivery-pending"><strong>还需完成</strong><ul>{pending.map((item:string,i:number)=><li key={i}>{item}</li>)}</ul></div>}{canResume&&value.can_resume&&value.resume_from_turn_id&&<button type="button" className="secondary" disabled={disabled} onClick={()=>onResume(value.resume_from_turn_id)}>继续未完成部分<ChevronRight size={15}/></button>}</section>
}
function Step({step,onOpen,onAdopt,busy,accepted}:any){
  const [open,setOpen]=useState(false),result=step.result;
  return <div className={'agent-step '+step.state}>
    <button className="step-toggle" aria-expanded={open} onClick={()=>setOpen(!open)}>{step.state==='running'?<LoaderCircle size={14} className="spin"/>:step.state==='completed'?<Check size={14}/>:<AlertCircle size={14}/>}<strong>{toolNames[step.tool]||step.tool}</strong><span>{step.message}</span>{open?<ChevronDown size={14}/>:<ChevronRight size={14}/>}</button>
    {result?.artifact==='valuation'&&<div className="chat-artifact"><ValuationResult result={result.result} compact/><button className="text-link" onClick={()=>onOpen('model')}>查看假设与计算过程<ChevronRight size={14}/></button></div>}
    {result?.artifact==='proposal'&&<div className="chat-artifact proposal-artifact"><div><strong>{result.proposal.method_name} · 待确认的探索方案</strong><span className="badge">{result.proposal.id}</span></div><AssumptionChips values={result.proposal.assumptions}/><p>{result.proposal.rationale}</p><button className="primary" disabled={busy||accepted} onClick={()=>onAdopt(result.proposal)}>{accepted?'已采用':'采用此方案并计算'}</button><small>也可以在对话中提出修改，再决定是否采用。</small></div>}
    {result?.artifact==='report'&&<div className="chat-artifact report-artifact"><FilePenLine size={20}/><div><strong>{step.reused_from_step_id?'沿用已有报告':'报告已更新'} · 第 {result.revision} 版</strong><p>{result.change_note}</p></div><button className="text-link" onClick={()=>onOpen('overview')}>查看报告</button></div>}
    {result?.artifact==='research'&&<div className="chat-artifact report-artifact"><BookOpen size={20}/><div><strong>{result.company} · 财务底稿</strong><p>{result.fact_count} 项财务事实 · {result.state==='completed_with_gaps'?'已保存，含待核实项':result.state}</p></div><button className="text-link" onClick={()=>onOpen('overview')}>查看证据</button></div>}
    {open&&result?.artifact==='document'&&<div className="chat-artifact report-artifact"><FileText size={20}/><div><strong>{result.document.title}</strong><p>{result.page_count} 页</p><a href={'/api/documents/'+result.document.id+'/file'} target="_blank" rel="noreferrer">打开 PDF 原文</a></div></div>}
    {open&&result?.artifact==='document_search'&&<div className="chat-artifact document-citations"><strong>原文依据 · {result.snippets.length} 个片段</strong>{result.snippets.map((s:any,i:number)=><details key={i}><summary>{s.title} · 第 {s.page} 页{s.query?' · '+s.query:''}</summary><p>{s.text}</p><a href={s.url} target="_blank" rel="noreferrer">打开第 {s.page} 页</a></details>)}<small>{result.note}</small></div>}
    {open&&['web_search','read_public_pdf'].includes(step.tool)&&<div className="chat-artifact"><WebReferences sources={result?.results||[]}/>{!!result?.errors?.length&&<p className="small-muted">{result.errors.map((item:any)=>typeof item==='string'?item:([item?.message,item?.error,item?.detail].find(value=>typeof value==='string')||'部分来源暂时无法访问')).join('；')}</p>}</div>}
    {open&&['read_webpage','read_web_page'].includes(step.tool)&&result?.url&&<div className="chat-artifact"><WebReferences sources={[result]}/><p className="web-excerpt">{String(result.text||'').slice(0,1200)}</p></div>}
    {result?.error&&<p className="step-error">{result.error}</p>}
    {open&&result&&<details className="tool-raw"><summary>查看工具原始结果</summary><pre className="tool-result">{JSON.stringify(result,null,2)}</pre></details>}
  </div>
}
export default function AgentChat({run,config,onConfigChange,onRunUpdated,onOpen,onNewResearch,prompt,autoSendPrompt=false,onPromptConsumed}:any){
  const [chat,setChat]=useState<any>(null),[chats,setChats]=useState<any[]>([]),[draft,setDraft]=useState(''),[profile,setProfile]=useState(config.active_model||''),[sending,setSending]=useState(false),[error,setError]=useState(''),[loading,setLoading]=useState(true);
  const [attached,setAttached]=useState<any[]>([]),[historyNotice,setHistoryNotice]=useState(''),[showMaterials,setShowMaterials]=useState(false),[stopping,setStopping]=useState(false);
  const [budget,setBudget]=useState(()=>defaultBudget(config));
  const chatRef=useRef<any>(null);
  const stoppingRef=useRef<{id:string,turnId:string}|null>(null);
  const consumedPrompt=useRef(''),sendingRef=useRef(false);
  const bottom=useRef<HTMLDivElement>(null),textarea=useRef<HTMLTextAreaElement>(null),callback=useRef(onRunUpdated),openCallback=useRef(onOpen);
  callback.current=onRunUpdated;openCallback.current=onOpen;
  const active=chat?.state==='running';
  const terminationPending=chat?.termination?.status==='pending'&&chat.termination.retryable&&chat.termination.turn_id===chat.turn_id;
  const selectionKey='research-conversation:'+(run?.id||'free');
  function remember(c:any){if(stoppingRef.current&&(stoppingRef.current.id!==c.id||stoppingRef.current.turnId!==c.turn_id||c.state!=='running')){stoppingRef.current=null;setStopping(false)}chatRef.current=c;localStorage.setItem(selectionKey,c.id);setChat(c)}
  function acceptSnapshot(c:any,id:string,turnId:string){
    const current=chatRef.current;
    if(!current||current.id!==id||current.turn_id!==turnId||c.id!==id)return false;
    if(c.turn_id!==turnId&&(!c.updated_at||c.updated_at<=current.updated_at))return false;
    // A queued event from an older request must not revive a stopped turn.
    if((c.turn_id===turnId&&current.state!=='running'&&c.state==='running')||(current.updated_at&&c.updated_at&&c.updated_at<current.updated_at))return false;
    remember(c);setError(value=>c.state!=='running'||value.startsWith('暂时无法获取任务状态')||value.startsWith('状态消息读取失败')?'':value);return true;
  }
  async function ensureChat(){if(chatRef.current)return chatRef.current;const c=await post('/conversations',{run_id:run?.id||null});remember(c);await list();return c}
  async function documentsChanged(){const c=chatRef.current;if(!c)return;remember(await api('/conversations/'+c.id));setAttached(await api('/conversations/'+c.id+'/documents'));if(c.run_id)await callback.current(c.run_id)}
  useEffect(()=>{let alive=true;setAttached([]);if(chat?.id)api('/conversations/'+chat.id+'/documents').then(rows=>alive&&setAttached(rows)).catch(e=>alive&&setError(e.message));return()=>{alive=false}},[chat?.id,chat?.updated_at]);
  async function removeChat(){if(!chat||active)return;setError('');try{await api('/conversations/'+chat.id,{method:'DELETE'});localStorage.removeItem(selectionKey);chatRef.current=null;setChat(null);setAttached([]);const rows=await list();if(rows.length)remember(await api('/conversations/'+rows[0].id));setHistoryNotice('对话已移入回收站，可以恢复。')}catch(e:any){setError(e.message)}}
  useEffect(()=>{setProfile(config.active_model||'')},[config.active_model]);
  useEffect(()=>{setBudget(defaultBudget(config))},[JSON.stringify(config.execution_budget)]);
  async function list(){const v=await api('/conversations'+(run?.id?'?run_id='+run.id:''));setChats(v);return v}
  useEffect(()=>{let alive=true;api('/conversations'+(run?.id?'?run_id='+run.id:'')).then(async rows=>{if(!alive)return;setChats(rows);const saved=localStorage.getItem(selectionKey);const selected=rows.find((r:any)=>r.id===saved)||rows[0];if(selected){const c=await api('/conversations/'+selected.id);if(alive)remember(c)}}).catch(e=>alive&&setError(e.message)).finally(()=>alive&&setLoading(false));return()=>{alive=false}},[run?.id]);
  useEffect(()=>{if(!prompt){consumedPrompt.current='';return}if(loading||consumedPrompt.current===prompt)return;setDraft(prompt);textarea.current?.focus();if(autoSendPrompt&&active)return;consumedPrompt.current=prompt;onPromptConsumed?.();if(autoSendPrompt){if(!profile){setError('请先选择模型连接，再发送更新报告请求。');return}send(undefined,prompt)}},[prompt,autoSendPrompt,loading,active]);
  useEffect(()=>{bottom.current?.scrollIntoView({behavior:'smooth',block:'end'})},[chat?.messages.length,chat?.steps.length,active]);
  useEffect(()=>{
    if(!chat?.id||!active)return;
    const id=chat.id,turnId=chat.turn_id,events=new EventSource('/api/conversations/'+id+'/events');let disposed=false,checking=false;
    function update(c:any){if(disposed||!acceptSnapshot(c,id,turnId))return;if(c.state!=='running'){events.close();list().catch(()=>{});if(c.run_id)callback.current(c.run_id)}}
    events.onmessage=e=>{try{update(JSON.parse(e.data))}catch{setError('状态消息读取失败，正在重新获取任务状态。')}};
    events.onerror=()=>{if(disposed||checking)return;checking=true;api('/conversations/'+id).then(c=>{if(!disposed)update(c)}).catch(()=>{if(!disposed&&chatRef.current?.id===id&&chatRef.current?.turn_id===turnId)setError('暂时无法获取任务状态，正在重新连接。')}).finally(()=>{checking=false})};
    return()=>{disposed=true;events.close()};
  },[chat?.id,chat?.turn_id,active]);
  async function stop(){
    const current=chatRef.current,canRetry=current?.termination?.status==='pending'&&current.termination.retryable&&current.termination.turn_id===current.turn_id;
    if(!current||(current.state!=='running'&&!canRetry)||stoppingRef.current)return;
    const target={id:current.id,turnId:current.turn_id};stoppingRef.current=target;setStopping(true);setError('');
    const controller=new AbortController(),timeout=window.setTimeout(()=>controller.abort(),15000);
    try{
      const response=await api('/conversations/'+target.id+'/cancel',{method:'POST',body:JSON.stringify({turn_id:target.turnId}),signal:controller.signal});
      const next=response.id?response:await api('/conversations/'+target.id,{signal:controller.signal});
      if(acceptSnapshot(next,target.id,target.turnId)&&next.state!=='running'){list().catch(()=>{});if(next.run_id)callback.current(next.run_id)}
    }catch(e:any){
      if(stoppingRef.current===target){stoppingRef.current=null;setStopping(false);setError(e.name==='AbortError'?'停止请求尚未确认，请重试；任务状态仍会自动更新。':'停止请求未能确认：'+e.message+'。可以重试。')}
    }finally{window.clearTimeout(timeout)}
  }
  async function newChat(unbound=false){setError('');try{const c=await post('/conversations',{run_id:unbound?null:run?.id||null});remember(c);setDraft('');await list()}catch(e:any){setError(e.message)}}
  async function select(id:string){if(!id)return;setError('');try{remember(await api('/conversations/'+id))}catch(e:any){setError(e.message)}}
  async function send(e?:any,content=draft,resumeFrom?:string){e?.preventDefault();if(!content.trim()||sendingRef.current||active)return;sendingRef.current=true;setSending(true);setError('');try{
    const c=await ensureChat();
    const next=await post('/conversations/'+c.id+'/messages',{message:content,profile_id:profile||null,execution_budget:{...budget},...(resumeFrom?{resume_from_turn_id:resumeFrom}:{})});
    remember(next);if(!resumeFrom)setDraft('');await list();
  }catch(e:any){setError(e.message)}finally{sendingRef.current=false;setSending(false)}}
  async function openArtifact(tab:string){if(chat?.run_id)await callback.current(chat.run_id);openCallback.current(tab)}
  const linkedRun=chat&&!chat.run_id?null:run;
  const contextName=linkedRun?linkedRun.company+' · '+linkedRun.request.year:'自由研究';
  const messages=chat?.messages||[];
  const webSources=observedSources(chat?.steps||[],messages.filter((m:any)=>m.role==='assistant').map((m:any)=>m.content).join('\n'));
  const suggestions=linkedRun?[
    ['深入研究并保存报告',deepResearchPrompt],
    ['讨论研究思路','我更关心利润增长的可持续性。请先和我讨论该如何研究，有哪些反证和缺失资料。'],
    ['一起选择估值方法','请比较 PE、DCF 和其他估值方法对当前公司的适用性，先不要直接给出估值。'],
    ['改变报告关注点','请把研究报告改为以现金流风险为重点，保留反证和后续核实清单，并保存新版本。'],
  ]:[
    ['从一家公司开始','请研究美的集团 2025 年的收入、归母净利润和经营现金流，先找到公司并建立事实底稿。'],
    ['讨论一个投资想法','我想研究家电行业，应该如何把一个投资想法拆成可验证的假设？'],
    ['理解估值方法','帮我比较 PE、PB、DCF、EV/EBITDA 和 DDM 的适用条件。先不计算。'],
  ];
  return <div className="agent-page"><div className="agent-top"><div><h1>{linkedRun?linkedRun.company:'研究对话'}</h1></div><div className="button-row"><button className="secondary" disabled={active} onClick={()=>newChat(false)}><Plus size={15}/>新话题</button><button className="text-link" disabled={active} onClick={()=>newChat(true)}>自由研究</button><button className={'secondary materials-toggle '+(showMaterials?'selected':'')} aria-expanded={showMaterials} onClick={()=>setShowMaterials(!showMaterials)}><BookOpen size={15}/>研究资料</button></div></div>
    <div className="chat-context"><span><Layers3 size={15}/>{contextName}</span><button onClick={onNewResearch}><Search size={14}/>选择公司</button><i/><label><span>模型</span><select aria-label="对话模型" disabled={active} value={profile} onChange={e=>setProfile(e.target.value)}>{config.profiles?.map((p:any)=><option key={p.id} value={p.id}>{p.name} · {p.model}</option>)}</select></label><button className="context-settings" aria-label="设置模型连接" onClick={()=>onOpen('settings')}><SlidersHorizontal size={14}/></button></div>
    <ConstraintChips value={chat?.effective_constraints}/>
    <div className={'chat-split '+(showMaterials?'materials-open':'')}><section className="conversation">
      {!!chats.length&&<div className="conversation-selector"><MessageSquare size={13}/><select aria-label="历史研究对话" value={chat?.id||''} disabled={active} onChange={e=>select(e.target.value)}><option value="" disabled>选择对话</option>{chats.map(c=><option key={c.id} value={c.id}>{c.title} · {new Date(c.updated_at).toLocaleDateString('zh-CN')}</option>)}</select><button type="button" className="icon-button" title="删除当前对话，可从回收站恢复" aria-label="删除当前对话" disabled={active||sending} onClick={removeChat}><Trash2 size={14}/></button></div>}
      {historyNotice&&<div className="history-notice" role="status">{historyNotice}<button className="text-link" onClick={()=>onOpen('trash')}>打开回收站</button></div>}
      <div className="chat-messages" aria-live="polite">
        {loading?<div className="chat-empty"><LoaderCircle className="spin"/></div>:!messages.length&&<div className="chat-empty"><span className="agent-symbol"><Layers3 size={28}/></span><h2>今天想研究什么？</h2><p>从一个判断开始，寻找证据，检验反证。</p><div className="chat-suggestions">{suggestions.map(([title,text])=><button key={title} onClick={()=>{setDraft(text);textarea.current?.focus()}}><span>{title}</span><ChevronRight size={15}/></button>)}</div></div>}
        {messages.map((m:any,i:number)=>{const steps=m.role==='user'?(chat?.steps||[]).filter((s:any)=>s.turn_id===m.turn_id):[];return <div key={m.id} className={'chat-turn '+m.role}>
          <div className="message-row"><span className="message-avatar">{m.role==='user'?<User size={15}/>:<Layers3 size={17}/>}</span><div className={'message-content '+(m.error?'error':'')}><div className="message-label">{m.role==='user'?'你':'研序'}<time>{new Date(m.time).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}</time></div><ResearchText className="chat-prose" text={m.content}/></div></div>
          {!!steps.length&&<details className="tool-timeline" open={active&&i===messages.length-1}><summary><ActivityIcon running={active&&i===messages.length-1}/><span>{active&&i===messages.length-1?'正在研究':'研究过程'} · {steps.length} 个步骤</span>{steps.some((s:any)=>s.result?.artifact==='proposal')&&<span className="badge">含估值方案</span>}<ChevronDown size={14}/></summary><div className="tool-timeline-items">{steps.map((s:any)=><Step key={s.id} step={s} onOpen={openArtifact} busy={active||sending} accepted={chat?.proposals?.find((p:any)=>p.id===s.result?.proposal?.id)?.state==='accepted'} onAdopt={(p:any)=>send(undefined,'我采用估值方案 '+p.id+'（'+p.method_name+'），请按展示的参数计算并保存。')}/>)}</div></details>}
          {m.role==='assistant'&&<Coverage value={turnCoverage(chat,m.turn_id)||m.coverage}/>}
          {m.role==='assistant'&&<Delivery value={m.delivery} turnId={m.turn_id} canResume={i===messages.length-1} disabled={active||sending||!profile} onResume={(id:string)=>send(undefined,'继续未完成部分',id)}/>}
          {m.role==='user'&&i===messages.length-1&&active&&<div className="thinking-status" role="status"><LoaderCircle size={14} className="spin"/>{stopping?'正在停止，等待服务器确认…':steps.at(-1)?.state==='running'?'正在执行工具…':'正在结合你的问题和工具结果继续研究…'}</div>}
        </div>})}<div ref={bottom}/>
      </div>
      {terminationPending&&<div className="termination-notice" role="status"><span>本轮已停止写入，后台连接尚未确认关闭，可重试结束连接。</span><button type="button" className="secondary" disabled={stopping||sending} aria-busy={stopping} onClick={stop}>{stopping?<><LoaderCircle size={13} className="spin"/>正在确认…</>:'重试结束连接'}</button></div>}
      <ExecutionBudget config={config} value={active?normalizeBudget(chat.execution_budget||chat.turn_contexts?.[chat.turn_id]?.execution_budget||budget,config):budget} onChange={setBudget} onDefaultsSaved={onConfigChange} disabled={active||sending} running={active} usage={chat?.budget_usage}/>
      <form className={'chat-composer'+(active?' chat-is-active':'')} onSubmit={send}>{error&&<p className="form-error" role="alert">{error}</p>}<textarea ref={textarea} aria-label="与研序讨论研究" placeholder="提出问题，或继续追问你的投资判断…" value={draft} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();send()}}} maxLength={8000} disabled={sending}/>
        <ResearchDocuments run={linkedRun} ensureChat={ensureChat} onDone={documentsChanged} disabled={active||sending}/>
        <div className="composer-bottom"><span>Enter 发送 · Shift + Enter 换行</span>{active?<button type="button" className="secondary" disabled={stopping} aria-busy={stopping} onClick={stop}>{stopping?<LoaderCircle size={13} className="spin"/>:<Square size={13}/>} {stopping?'正在停止…':'停止'}</button>:<button className="send-button" aria-label="发送研究消息" disabled={sending||!draft.trim()||!profile}>{sending?<LoaderCircle className="spin" size={17}/>:<ArrowUp size={18}/>}</button>}</div></form>
    </section><aside className="chat-companion"><h3>资料与成果</h3><button className="deep-research-action" onClick={()=>send(undefined,deepResearchPrompt)} disabled={!linkedRun||active||sending||!profile}><Sparkles size={18}/><span>深入研究并保存报告</span><ChevronRight size={14}/></button><button onClick={()=>onOpen('overview')} disabled={!linkedRun}><BookOpen size={18}/><span>公司研究<small>{linkedRun?linkedRun.facts.length+' 项事实 · 第 '+(linkedRun.report_revision||1)+' 版':'尚未建立研究'}</small></span><ChevronRight size={14}/></button><button onClick={()=>onOpen('model')} disabled={!linkedRun}><SlidersHorizontal size={18}/><span>估值与假设<small>{linkedRun?.valuation_history?.length||0} 份情景</small></span><ChevronRight size={14}/></button><DocumentLinks documents={attached}/>{!!webSources.length&&<div className="chat-web-sources"><h4>网络来源 · 共 {webSources.length} 个</h4><WebReferences sources={webSources.slice(0,8)}/>{webSources.length>8&&<details><summary>查看其余 {webSources.length-8} 个来源</summary><WebReferences sources={webSources.slice(8)}/></details>}</div>}</aside></div>
  </div>
}
function ActivityIcon({running}:{running:boolean}){return running?<LoaderCircle size={14} className="spin"/>:<Check size={14}/>}
