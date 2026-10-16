import {useEffect,useState} from 'react';
import {LoaderCircle,ShieldCheck} from 'lucide-react';
import {api} from './api';

export default function RuntimePolicy(){
 const [policy,setPolicy]=useState<any>(null),[error,setError]=useState(''),[loading,setLoading]=useState(false);
 async function load(){setLoading(true);setError('');try{setPolicy(await api('/runtime-policy'))}catch(e:any){setError(e.message)}finally{setLoading(false)}}
 useEffect(()=>{load()},[]);
 return <details className="panel runtime-policy"><summary><ShieldCheck size={19}/><strong>高级：运行与数据访问</strong>{policy&&<span className="badge">{policy.enabled?'受控运行':'普通运行'}</span>}</summary><div className="runtime-policy-body">
 {loading?<p><LoaderCircle size={16} className="spin"/>正在读取配置…</p>:error?<p className="form-error" role="alert">{error} <button className="text-link" onClick={load}>重试</button></p>:policy&&<>
 <p>{policy.enabled?'当前使用独立数据目录和已许可的网络来源，研究功能与普通运行共用。':'日常研究空间，可使用已配置的模型、资料检索与本地材料。'}</p>
 <dl><dt>数据目录</dt><dd>{policy.data_directory}</dd><dt>详细执行日志</dt><dd>{policy.audit_enabled?'已启用':'未启用'}</dd><dt>模型网络来源</dt><dd>{policy.enabled?(policy.model_origins?.join('、')||'无已许可来源'):'不额外限制，以模型连接设置为准'}</dd><dt>资料网络来源</dt><dd>{policy.enabled?(policy.retrieval_origins?.join('、')||'无已许可来源'):'不额外限制，使用已配置的检索服务'}</dd></dl>
 <p>需要指定材料、独立数据目录或网络白名单时，使用 <code>scripts/start-controlled.ps1</code> 启动受控实例，打开启动窗口给出的地址。访问策略在启动时确定。</p>
 {policy.note&&<p className="form-hint">{policy.note}</p>}
 </>}
 </div></details>
}
