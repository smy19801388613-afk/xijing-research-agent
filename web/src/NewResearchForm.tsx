import {useEffect,useRef,useState} from 'react';
import {ArrowRight,BookOpen,LoaderCircle,Sparkles} from 'lucide-react';
import CompanySearch from './CompanySearch';
import MaterialPreflight from './MaterialPreflight';
import {api,post} from './api';

export default function NewResearchForm({form,setForm,docs,materials,config,creating,onStart,error}:any){
 const [preflight,setPreflight]=useState<any>(null),[checking,setChecking]=useState(false),[checkError,setCheckError]=useState(''),[submitting,setSubmitting]=useState(false),[policy,setPolicy]=useState<any>(null),[policyError,setPolicyError]=useState('');
 const sequence=useRef(0);
 const mounted=useRef(true);
 const payload={...form,year:Number(form.year),document_id:form.document_id||null,material_ids:form.material_ids||[]};
 const signature=JSON.stringify(payload);
 const selectedIds=form.material_ids||[];
 const available=materials.filter((m:any)=>m.ticker===form.ticker&&m.year===Number(form.year));
 const locked=creating||submitting;
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[]);
 useEffect(()=>{const controller=new AbortController();api('/runtime-policy',{signal:controller.signal}).then(setPolicy).catch(e=>{if(e.name!=='AbortError')setPolicyError(e.message)});return()=>controller.abort()},[]);
 useEffect(()=>{if(policy?.enabled&&form.mode==='live'&&form.data_source!=='documents')setForm((value:any)=>({...value,data_source:'documents'}))},[policy?.enabled,form.mode,form.data_source]);
 useEffect(()=>{
  const current=++sequence.current,controller=new AbortController();setPreflight(null);setCheckError('');
  if(form.mode!=='live'){setChecking(false);return}
  setChecking(true);
  const timer=setTimeout(()=>api('/materials/preflight',{method:'POST',body:signature,signal:controller.signal})
   .then(v=>{if(current===sequence.current)setPreflight(v)})
   .catch(e=>{if(e.name!=='AbortError'&&current===sequence.current)setCheckError(e.message)})
   .finally(()=>{if(current===sequence.current)setChecking(false)}),400);
  return()=>{clearTimeout(timer);controller.abort();sequence.current++};
 },[signature,materials,docs]);
 async function submit(e:any){
  e.preventDefault();if(locked||checking)return;setSubmitting(true);setCheckError('');
  try{if(form.mode==='live'){const latest=await post('/materials/preflight',payload);if(!mounted.current)return;setPreflight(latest);if(!latest.ready)return}if(mounted.current)await onStart(payload)}
  catch(e:any){setCheckError(e.message)}finally{setSubmitting(false)}
 }
 function toggleMaterial(id:string,selected:boolean){setForm({...form,material_ids:selected?[...selectedIds,id]:selectedIds.filter((v:string)=>v!==id)})}
 return <form className="new-research-form" onSubmit={submit}><fieldset disabled={locked}>
  <div className="mode-options"><button type="button" className={form.mode==='live'?'selected':''} onClick={()=>setForm({...form,mode:'live'})}><Sparkles size={20}/><strong>实时研究</strong><span>选择数据与材料 · 模型分析</span></button><button type="button" className={form.mode==='sample'?'selected':''} onClick={()=>setForm({...form,mode:'sample',ticker:'000333.SZ',year:2025,document_id:'',material_ids:[]})}><BookOpen size={20}/><strong>样例回放</strong><span>美的 2025 · 已保存快照</span></button></div>
  <CompanySearch value={form.ticker} disabled={form.mode==='sample'||locked} onChange={(ticker:string)=>setForm((f:any)=>({...f,ticker,document_id:'',material_ids:[]}))}/>
  <div className="form-row"><label>报告年度<input required type="number" min="2000" max={new Date().getFullYear()} value={form.year} disabled={form.mode==='sample'} onChange={e=>setForm({...form,year:Number(e.target.value),document_id:'',material_ids:[]})}/></label><label>研究截止日<input required type="date" max={config.today} value={form.as_of} onChange={e=>setForm({...form,as_of:e.target.value})}/></label></div>
  {form.mode==='live'&&<label>本次数据源<select disabled={policy?.enabled} value={policy?.enabled?'documents':form.data_source||'default'} onChange={e=>setForm({...form,data_source:e.target.value})}><option value="default">使用默认 · {config.data_sources?.find((s:any)=>s.id===config.data_source)?.name}</option>{config.data_sources?.map((s:any)=><option key={s.id} value={s.id}>{s.name}</option>)}{!config.data_sources?.some((s:any)=>s.id==='documents')&&<option value="documents">仅使用指定材料</option>}</select>{policy?.enabled&&<span className="form-hint">受控运行：财务底稿使用指定材料，模型与检索遵循当前访问许可。</span>}</label>}
  <label>研究问题<textarea required minLength={4} maxLength={1500} rows={3} value={form.question} onChange={e=>setForm({...form,question:e.target.value})}/></label>
  {form.mode==='live'&&<section className="research-material-picker" aria-label="本次研究材料"><h3>参考材料</h3><label>PDF 报告<select value={form.document_id||''} onChange={e=>setForm({...form,document_id:e.target.value})}><option value="">{selectedIds.length||policy?.enabled?'不绑定 PDF':'自动匹配材料 / 不指定 PDF'}</option>{docs.filter((d:any)=>d.ticker===form.ticker&&d.year===Number(form.year)).map((d:any)=><option key={d.id} value={d.id}>{d.title}</option>)}</select></label>
   <div className="research-material-list" role="group" aria-label="选择结构化材料">{available.map((m:any)=><label className="material-choice" key={m.id}><input type="checkbox" checked={selectedIds.includes(m.id)} onChange={e=>toggleMaterial(m.id,e.target.checked)}/><span><strong>{m.filename}</strong><span>{m.row_count} 行 · {m.ticker} · {m.year} 年</span></span></label>)}</div>
   {!available.length&&<p className="form-hint">暂无匹配的 JSON / CSV。可先在资料库导入，再用于新研究。</p>}
   {(selectedIds.length>0||form.document_id)&&form.data_source!=='documents'&&<button type="button" className="text-link" onClick={()=>setForm({...form,data_source:'documents'})}>仅使用这些材料，不读取财务接口</button>}
  </section>}
  {form.mode==='live'&&<label className="checkbox-label"><input type="checkbox" checked={form.use_model} onChange={e=>setForm({...form,use_model:e.target.checked})}/>调用模型提出研究追问并生成草稿</label>}
 </fieldset>
 {form.mode==='live'?<MaterialPreflight value={preflight} checking={checking||!policy&&!policyError} error={checkError||policyError}/>:<p className="modal-note">样例使用 2026-09-28 保存的数据，不调用实时接口或模型。</p>}
 {error&&<p className="form-error" role="alert">{error}</p>}
 <button className="primary full research-start" disabled={locked||!form.ticker||(form.mode==='live'&&(checking||!policy||!!policyError||!preflight?.ready))} type="submit">{locked?<LoaderCircle size={17} className="spin"/>:<ArrowRight size={17}/>} {locked?'正在准备研究…':'开始研究'}</button>
 </form>
}
