import {useEffect,useRef,useState} from 'react';
import {ArrowRight,Database,ExternalLink,FileText,LoaderCircle,Upload,X} from 'lucide-react';
import CompanySearch from './CompanySearch';
import {api} from './api';

const fieldNames:any={metric:'指标',year:'年度',value:'数值',unit:'单位',currency:'币种',scope:'口径',period_type:'期间类型',announced_date:'公告日期'};
const sample='metric,year,value,unit,currency,scope,period_type,announced_date\nrevenue,2025,100000000,元,CNY,consolidated,annual,2026-03-31\nn_income_attr_p,2025,10000000,元,CNY,consolidated,annual,2026-03-31\nn_cashflow_act,2025,12000000,元,CNY,consolidated,annual,2026-03-31\n';

export function MaterialViewer({id,onClose}:any){
 const [value,setValue]=useState<any>(null),[error,setError]=useState(''),[page,setPage]=useState(0);
 useEffect(()=>{const controller=new AbortController();api('/materials/'+id,{signal:controller.signal}).then(setValue).catch(e=>{if(e.name!=='AbortError')setError(e.message)});return()=>controller.abort()},[id]);
 useEffect(()=>{const key=(e:KeyboardEvent)=>{if(e.key==='Escape')onClose()};document.addEventListener('keydown',key);return()=>document.removeEventListener('keydown',key)},[onClose]);
 const rows=value?.rows||[],visible=rows.slice(page*50,(page+1)*50),columns=[...new Set<string>(visible.flatMap((row:any)=>Object.keys(row)))];
 return <div className="modal-backdrop" onMouseDown={e=>e.target===e.currentTarget&&onClose()}><section className="modal wide material-viewer" role="dialog" aria-modal="true" aria-label="查看结构化材料"><header><h2>{value?.metadata?.filename||'查看结构化材料'}</h2><button className="icon-button" aria-label="关闭材料" onClick={onClose}><X size={20}/></button></header>
 {error?<p className="form-error" role="alert">{error}</p>:!value?<p className="material-loading"><LoaderCircle className="spin" size={18}/>正在读取材料…</p>:<><p>{value.metadata.ticker} · {value.metadata.year} 年 · 共 {rows.length} 行</p><p className="form-hint">以下为导入的原始数据；是否符合研究截止日与报表口径，以新研究的启动检查为准。</p><div className="material-table"><table><thead><tr><th>行</th>{columns.map(key=><th key={key}>{fieldNames[key]||key}</th>)}</tr></thead><tbody>{visible.map((row:any,index:number)=><tr key={page*50+index}><td>{page*50+index+1}</td>{columns.map(key=><td key={key}>{row[key]==null?'—':typeof row[key]==='object'?JSON.stringify(row[key]):String(row[key])}</td>)}</tr>)}</tbody></table></div><div className="material-pagination"><button className="secondary" disabled={page===0} onClick={()=>setPage(page-1)}>上一页</button><span>第 {page+1} / {Math.max(1,Math.ceil(rows.length/50))} 页</span><button className="secondary" disabled={(page+1)*50>=rows.length} onClick={()=>setPage(page+1)}>下一页</button></div></>}
 </section></div>
}

export default function SourceLibrary({run,docs,materials,onDocumentsChange,onMaterialsChange,onNewResearch,onAttach,busy,onNotice}:any){
 const [meta,setMeta]=useState({ticker:run?.request.ticker||'000333.SZ',year:run?.request.year||new Date().getFullYear()-1,announced_date:''});
 const [uploading,setUploading]=useState(false),[error,setError]=useState(''),[view,setView]=useState('');
 const fileInput=useRef<HTMLInputElement>(null);
 useEffect(()=>{if(run)setMeta({ticker:run.request.ticker,year:run.request.year,announced_date:''})},[run?.id]);
 async function upload(file:File){setUploading(true);setError('');try{
  const structured=/\.(json|csv)$/i.test(file.name);if(!structured&&!/\.pdf$/i.test(file.name))throw new Error('请选择 PDF、JSON 或 CSV 文件');
  const data=new FormData();data.append('file',file);data.append('ticker',meta.ticker);data.append('year',String(meta.year));if(meta.announced_date)data.append('announced_date',meta.announced_date);
  await api(structured?'/materials':'/documents',{method:'POST',body:data});
  if(structured)onMaterialsChange(await api('/materials'));else onDocumentsChange(await api('/documents'));
  onNotice('材料已保存，可在下方查看或用于新研究');
 }catch(e:any){setError(e.message)}finally{setUploading(false);if(fileInput.current)fileInput.current.value=''}}
 return <div className="content-page source-library"><div className="section-heading"><div><h1>资料库</h1><p>将报告和财务数据绑定到公司，带着证据开始研究。</p></div></div>
 {error&&<p className="form-error" role="alert">{error}</p>}
 <section className="panel upload-panel"><div><h2><Upload size={18}/>导入研究材料</h2><p className="small-muted">支持 PDF 年报与 JSON / CSV 财务数据。原始文件和来源信息将随研究保留。</p></div><div className="upload-fields unified-upload-fields"><CompanySearch value={meta.ticker} disabled={uploading} onChange={(ticker:string)=>setMeta({...meta,ticker})}/><label>报告年度<input type="number" min="2000" max="2100" value={meta.year} disabled={uploading} onChange={e=>setMeta({...meta,year:Number(e.target.value)})}/></label><label>公告日期（可选）<input type="date" value={meta.announced_date} disabled={uploading} onChange={e=>setMeta({...meta,announced_date:e.target.value})}/></label><button className="primary" disabled={uploading||!/^\d{6}\.(SH|SZ|BJ)$/.test(meta.ticker)||!meta.year} onClick={()=>fileInput.current?.click()}>{uploading?<LoaderCircle className="spin" size={16}/>:<Upload size={16}/>}选择文件</button><input hidden type="file" accept=".pdf,.json,.csv,application/pdf,application/json,text/csv" ref={fileInput} onChange={e=>e.target.files?.[0]&&upload(e.target.files[0])}/></div>
 <details className="material-format"><summary>文件要求与财务数据格式</summary><p>PDF 最多 100 MB / 800 页；JSON / CSV 最多 10 MB / 10,000 行，使用 UTF-8 编码。扫描页可通过本机 OCR 查阅，金额仍需核对原图。</p><p>每行对应一个年度指标，支持营业收入（revenue）、归母净利润（n_income_attr_p）和经营现金流（n_cashflow_act）。包含指标、年度、数值、单位（元 / 千元 / 万元）、币种 CNY、合并口径 consolidated、年度期间 annual 和公告日期。JSON 可用对象列表或 rows 列表。</p><p>可同时提供研究年度与上一年度。行内未填写公告日期时采用上方日期；缺少日期或晚于截止日的行不会作为已知财务事实。</p><a className="text-link" download="financial-material-example.csv" href={'data:text/csv;charset=utf-8,'+encodeURIComponent('\ufeff'+sample)}>下载 CSV 示例（演示数据）</a></details>
 </section>
 <div className="library-section-title"><h2>PDF 报告</h2><span>{docs.length} 份</span></div><div className="document-grid">{docs.map((d:any)=><section className="panel document-card" key={d.id}><span className="document-icon"><FileText size={25}/></span><span className="badge">{d.kind==='official_sample'?'官方样例':d.kind==='public_report'?'公开年报':'本地材料'}</span><h3>{d.title}</h3><p>{d.ticker} · {d.year} 年</p><small>公告日期 {d.announced_date||'未确认'} · {d.page_count?d.page_count+' 页':'可查阅'}</small><div><a className="text-link" href={'/api/documents/'+d.id+'/file'} target="_blank" rel="noreferrer">打开原文<ExternalLink size={13}/></a><button className="text-link" onClick={()=>onNewResearch({ticker:d.ticker,year:d.year,document_id:d.id,material_ids:[],mode:'live'})}>用于新研究<ArrowRight size={13}/></button>{run&&d.ticker===run.request.ticker&&d.year===run.request.year&&<button className="text-link" disabled={busy} onClick={()=>onAttach(d)}>接入当前对话<ArrowRight size={13}/></button>}</div></section>)}</div>{!docs.length&&<p className="library-empty">还没有 PDF，上传年报后即可查阅与引用原文。</p>}
 <div className="library-section-title"><h2>结构化财务材料</h2><span>{materials.length} 份</span></div><div className="document-grid">{materials.map((m:any)=><section className="panel document-card" key={m.id}><span className="document-icon"><Database size={25}/></span><span className="badge">{m.filename.toLowerCase().endsWith('.csv')?'CSV':'JSON'}</span><h3>{m.filename}</h3><p>{m.ticker} · {m.year} 年 · {m.row_count} 行</p><small>公告日期 {m.announced_date||'按行内日期核验'}</small><div><button className="text-link" onClick={()=>setView(m.id)}>查看数据<FileText size={13}/></button><button className="text-link" onClick={()=>onNewResearch({ticker:m.ticker,year:m.year,document_id:'',material_ids:[m.id],mode:'live',data_source:'documents'})}>用于新研究<ArrowRight size={13}/></button></div></section>)}</div>{!materials.length&&<p className="library-empty">还没有结构化材料，可导入自己的财务数据并查看逐行核验结果。</p>}
 {view&&<MaterialViewer id={view} onClose={()=>setView('')}/>}
 </div>
}
