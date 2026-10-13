import {useEffect,useRef,useState} from 'react';
import {AlertCircle,CheckCircle2,FileCheck2,FileUp,LoaderCircle,ShieldCheck} from 'lucide-react';
import {api,post} from './api';

function reasonIncludesCounts(material:any){
 const reason=String(material.reason||'').replace(/\s+/g,'');
 return reason.includes(`采用${material.accepted_rows}行`)&&reason.includes(`排除${material.excluded_rows||0}行`);
}

function Preflight({value,checking,error}:any){
 if(checking)return <section className="preflight checking" role="status"><LoaderCircle className="spin" size={19}/><span>正在检查材料、截止日与模型配置…</span></section>;
 if(error)return <section className="preflight blocked" role="alert"><AlertCircle size={19}/><div><strong>启动检查未完成</strong><p>{error}</p></div></section>;
 if(!value)return <section className="preflight" role="status">选择材料后，将自动检查是否可以开始。</section>;
 return <section className={'preflight '+(value.ready?'ready':'blocked')} aria-label="启动前检查"><header>{value.ready?<CheckCircle2 size={21}/>:<AlertCircle size={21}/>}<div><h3>{value.ready?'可以开始研究':'开始前还需要处理'}</h3><p>{value.ticker} · {value.year} 年 · 截止 {value.as_of}</p></div></header>
 {!!value.errors?.length&&<ul className="preflight-errors">{value.errors.map((e:string,i:number)=><li key={i}>{e}</li>)}</ul>}
 <div className="preflight-model"><strong>模型</strong><div><p>{value.model?.requested?(value.model.configured?(value.model.allowed?'已配置，未实测连接':'已配置，当前许可不允许使用'):'尚未完成配置'):'本次只核对材料，不调用模型'}{value.model?.name?' · '+value.model.name:''}</p>{value.model?.reason&&<p>{value.model.reason}</p>}</div></div>
 {!!value.materials?.length&&<div className="preflight-materials">{value.materials.map((m:any,i:number)=><article key={m.id||i}><div><strong>{m.filename||m.id}</strong><span className={'material-status '+m.status}>{{accepted:'采用',partial:'部分采用',excluded:'排除'}[m.status as string]||m.status}</span></div><p>{m.reason}</p>{m.accepted_rows!=null&&!reasonIncludesCounts(m)&&<p>采用 {m.accepted_rows} 行{m.excluded_rows?'，排除 '+m.excluded_rows+' 行':''}</p>}{!!m.issues?.length&&<details><summary>查看具体行的检查结果</summary><ul>{m.issues.map((issue:any,n:number)=><li key={n}>第 {issue.row} 行：{issue.reason}</li>)}</ul></details>}</article>)}</div>}
 {!!value.warnings?.length&&<details className="preflight-warnings"><summary>{value.warnings.length} 项提醒</summary><ul>{value.warnings.map((w:string,i:number)=><li key={i}>{w}</li>)}</ul></details>}
 <p className="form-hint">此检查不调用模型。研究开始前还会再次核对材料与访问许可。</p></section>
}

export default function CompetitionPanel({run,onCreated,onOpen}:any){
 const [policy,setPolicy]=useState<any>(null),[materials,setMaterials]=useState<any[]>([]),[docs,setDocs]=useState<any[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const [preflight,setPreflight]=useState<any>(null),[checking,setChecking]=useState(false),[checkError,setCheckError]=useState('');
 const sequence=useRef(0);
 const [form,setForm]=useState<any>({ticker:run?.request.ticker||'000333.SZ',year:run?.request.year||new Date().getFullYear()-1,as_of:new Date().toLocaleDateString('en-CA'),question:'核对指定材料中的收入、利润与现金流，说明证据和缺口。',document_id:'',material_ids:[],use_model:true});
 const payload={...form,year:Number(form.year),document_id:form.document_id||null,mode:'live',data_source:'documents'};
 const signature=JSON.stringify(payload);
 async function refresh(){const [p,m,d]=await Promise.all([api('/competition'),api('/competition/materials'),api('/documents')]);setPolicy(p);setMaterials(m);setDocs(d)}
 useEffect(()=>{refresh().catch(e=>setError(e.message))},[]);
 useEffect(()=>{if(!policy?.enabled)return;const current=++sequence.current,controller=new AbortController();setChecking(true);setPreflight(null);setCheckError('');const timer=setTimeout(()=>{api('/competition/preflight',{method:'POST',body:signature,signal:controller.signal}).then(v=>{if(current===sequence.current)setPreflight(v)}).catch(e=>{if(e.name!=='AbortError'&&current===sequence.current)setCheckError(e.message)}).finally(()=>{if(current===sequence.current)setChecking(false)})},450);return()=>{clearTimeout(timer);controller.abort();sequence.current++}},[signature,policy?.enabled,materials.length,docs.length]);
 async function upload(file:File){setBusy(true);setError('');try{const data=new FormData();data.append('file',file);data.append('ticker',form.ticker);data.append('year',String(form.year));const saved=await api('/competition/materials',{method:'POST',body:data});await refresh();if(saved.id)setForm((f:any)=>({...f,material_ids:[...new Set([...f.material_ids,saved.id])]}))}catch(e:any){setError(e.message)}finally{setBusy(false)}}
 async function create(e:any){e.preventDefault();if(busy||checking)return;setBusy(true);setChecking(true);setError('');setCheckError('');const current=++sequence.current;try{const latest=await post('/competition/preflight',payload);if(current!==sequence.current)return;setPreflight(latest);if(!latest.ready){setError('材料或配置有变化，请先处理下方检查结果。');return}const r=await post('/runs',payload);await onCreated(r.id)}catch(e:any){setError(e.message)}finally{setBusy(false);setChecking(false)}}
 return <div className="content-page competition-page"><div className="section-heading"><div><h1>比赛工作区</h1><p>用指定材料完成一份可追溯的研究。</p></div><span className="badge">{policy?.enabled?'受控模式':'普通模式'}</span></div>
 {error&&<p className="form-error" role="alert">{error}</p>}
 <section className="panel competition-policy"><ShieldCheck size={23}/><div><h2>{policy?.enabled?'材料与访问范围已隔离':policy?'当前使用普通研究空间':'正在读取工作区配置…'}</h2><p>{policy?.enabled?'开始前核对材料与模型许可，研究过程保留审计记录。':'比赛任务需要单独的受控工作区。这里可以查看启动方式与访问配置。'}</p><details><summary>{policy?.enabled?'查看访问配置':'如何进入比赛工作区'}</summary>{!policy?.enabled&&<p>使用 <code>scripts/start-competition.ps1</code> 启动独立比赛工作区，再打开启动窗口中的地址。</p>}<dl><dt>数据目录</dt><dd>{policy?.data_directory||'尚未取得'}</dd><dt>模型接口许可</dt><dd>{policy?.model_origins?.join('、')||'无'}</dd><dt>资料网络许可</dt><dd>{policy?.retrieval_origins?.join('、')||'无'}</dd></dl>{policy?.note&&<p>{policy.note}</p>}</details></div></section>
 {policy?.enabled&&<form className="competition-form" onSubmit={create}><section className="panel competition-inputs"><h2>本次研究</h2><fieldset disabled={busy}><div className="form-row"><label>证券代码<input required pattern="\d{6}\.(SH|SZ|BJ)" value={form.ticker} onChange={e=>setForm({...form,ticker:e.target.value.toUpperCase(),document_id:'',material_ids:[]})} placeholder="例如 000333.SZ"/></label><label>报告年度<input required type="number" min="2000" max="2100" value={form.year} onChange={e=>setForm({...form,year:e.target.value,document_id:'',material_ids:[]})}/></label><label>研究截止日<input type="date" required value={form.as_of} onChange={e=>setForm({...form,as_of:e.target.value})}/></label></div>
 <label>研究问题<textarea required minLength={4} maxLength={1500} rows={3} value={form.question} onChange={e=>setForm({...form,question:e.target.value})}/></label>
 <div className="competition-material-heading"><h3>指定材料</h3>{onOpen&&<button type="button" className="text-link" onClick={()=>onOpen('library')}>去资料库上传 PDF</button>}</div><label>PDF 年报<select value={form.document_id} onChange={e=>setForm({...form,document_id:e.target.value})}><option value="">不绑定 PDF</option>{docs.map(d=><option key={d.id} value={d.id}>{d.title} · {d.ticker} · {d.year}</option>)}</select></label>
 <div className="competition-upload"><FileUp size={19}/><label>导入 JSON / CSV<input aria-label="导入比赛结构化材料" type="file" accept=".json,.csv" disabled={busy||!/^\d{6}\.(SH|SZ|BJ)$/.test(form.ticker)} onChange={e=>{if(e.target.files?.[0])upload(e.target.files[0]);e.target.value=''}}/></label></div>
 <div className="competition-material-list" role="group" aria-label="选择结构化材料">{materials.map(m=><label className="material-choice" key={m.id}><input type="checkbox" checked={form.material_ids.includes(m.id)} onChange={e=>setForm({...form,material_ids:e.target.checked?[...form.material_ids,m.id]:form.material_ids.filter((id:string)=>id!==m.id)})}/><span><strong>{m.filename}</strong><span>{m.ticker} · {m.year} 年 · {m.row_count} 行</span></span></label>)}{!materials.length&&<p className="form-hint">暂无结构化材料，可以导入文件或选择已上传的 PDF。</p>}</div>
 <details className="material-format"><summary>结构化材料格式</summary><p>JSON / CSV 的字段与示例见项目文档 <code>docs/competition/mode.md</code>。日期、公司、年度或报表口径不匹配的记录会在检查中说明。</p></details>
 <label className="checkbox-label"><input type="checkbox" checked={form.use_model} onChange={e=>setForm({...form,use_model:e.target.checked})}/>调用模型分析材料并生成研究</label></fieldset></section>
 <div className="competition-check"><Preflight value={preflight} checking={checking} error={checkError}/><button className="primary competition-start" disabled={busy||checking||!preflight?.ready}>{busy?<LoaderCircle size={18} className="spin"/>:<FileCheck2 size={18}/>} {busy?'正在准备研究…':'开始指定材料研究'}</button></div></form>}
 {run&&<section className="panel submission-panel"><div><h2>当前研究提交包</h2><p>{run.company} · {run.request.year} 年。包含报告、材料、计算与审计记录。</p></div><a className="secondary" href={'/api/runs/'+run.id+'/submission'}>下载提交包</a></section>}
 </div>
}
