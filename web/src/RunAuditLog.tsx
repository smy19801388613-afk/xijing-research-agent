import {useState} from 'react';
import {LoaderCircle,RefreshCw} from 'lucide-react';
import {api} from './api';

const names:any={api_action:'接口操作',network_access:'网络访问',file_access:'文件访问',file_read:'读取文件',material_import:'导入材料',submission_export:'导出证据包',tool_arguments:'工具参数',tool_start:'工具开始',tool_result:'工具完成',tool_error:'工具失败',model_request:'模型请求',model_response:'模型响应',model_attempt:'模型尝试',model_failure:'模型失败',run_created:'创建研究',run_snapshot:'保存研究进度',run_update:'更新研究记录'};
const stages:any={research:'财务研究',deep_research:'深度研究',agent:'研究对话',model:'模型'};
function title(e:any){if(names[e.kind])return names[e.kind];const match=String(e.kind||'').match(/^(.*)_(start|result|error)$/);return match&&stages[match[1]]?stages[match[1]]+' · '+({start:'开始',result:'完成',error:'失败'}[match[2]]):'执行事件'}
function summary(e:any){return [e.tool||e.name,e.purpose==='model'?'模型接口':e.purpose==='retrieval'?'资料检索':'',e.origin,e.path?String(e.path).split(/[\\/]/).pop():'',e.status==='allowed'?'已许可':e.status==='blocked'?'已阻止':e.status,e.elapsed_ms!=null?e.elapsed_ms+' ms':''].filter(Boolean).join(' · ')}
function AuditEvent({event}:any){const [open,setOpen]=useState(false);return <details className="audit-event" onToggle={e=>setOpen(e.currentTarget.open)}><summary><time>{event.time?new Date(event.time).toLocaleString('zh-CN',{hour12:false}):'时间未记录'}</time><strong>{title(event)}</strong><span>{summary(event)}</span></summary>{open&&<pre>{JSON.stringify(event,null,2)}</pre>}</details>}

export default function RunAuditLog({runId}:any){
 const [events,setEvents]=useState<any[]|null>(null),[total,setTotal]=useState(0),[loading,setLoading]=useState(false),[error,setError]=useState(''),[limit,setLimit]=useState(30);
 async function load(){setLoading(true);setError('');try{const result=await api('/runs/'+runId+'/audit');setEvents(result.events||[]);setTotal(result.total||0);setLimit(30)}catch(e:any){setError(e.message)}finally{setLoading(false)}}
 return <details className="run-audit" onToggle={e=>{if(e.currentTarget.open&&events===null&&!loading)load()}}><summary>详细执行日志</summary><div className="run-audit-body">
 <div className="run-audit-toolbar"><p>{events===null?'按需读取文件访问、工具调用和结果生成记录。':total?'显示最近 '+events.length+' 条，共 '+total+' 条；完整记录可导出审计证据包。':'此研究暂无详细执行日志。'}</p><button className="text-link" disabled={loading} onClick={load}>{loading?<LoaderCircle className="spin" size={14}/>:<RefreshCw size={14}/>}刷新</button></div>
 {error&&<p className="form-error" role="alert">{error}</p>}
 {events?.length===0&&<p className="form-hint">启用详细审计之前的运行不会补录；上方仍保留当时保存的研究步骤。</p>}
 {events?.slice().reverse().slice(0,limit).map((event:any,index:number)=><AuditEvent key={String(event.seq??event.time)+index} event={event}/>)}
 {!!events&&limit<events.length&&<button className="secondary" onClick={()=>setLimit(limit+30)}>显示更多记录</button>}
 </div></details>
}
