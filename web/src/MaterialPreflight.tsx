import {AlertCircle,CheckCircle2,LoaderCircle} from 'lucide-react';

function reasonIncludesCounts(material:any){
 const reason=String(material.reason||'').replace(/\s+/g,'');
 return reason.includes(`采用${material.accepted_rows}行`)&&reason.includes(`排除${material.excluded_rows||0}行`);
}

export default function MaterialPreflight({value,checking,error}:any){
 if(checking)return <section className="preflight checking" role="status"><LoaderCircle className="spin" size={19}/><span>正在检查材料、截止日与模型配置…</span></section>;
 if(error)return <section className="preflight blocked" role="alert"><AlertCircle size={19}/><div><strong>启动检查未完成</strong><p>{error}</p></div></section>;
 if(!value)return <section className="preflight" role="status">正在准备启动前检查。</section>;
 return <section className={'preflight '+(value.ready?'ready':'blocked')} aria-label="启动前检查"><header>{value.ready?<CheckCircle2 size={21}/>:<AlertCircle size={21}/>}<div><h3>{value.ready?'可以开始研究':'开始前还需要处理'}</h3><p>{value.ticker} · {value.year} 年 · 截止 {value.as_of}</p></div></header>
 {!!value.errors?.length&&<ul className="preflight-errors">{value.errors.map((e:string,i:number)=><li key={i}>{e}</li>)}</ul>}
 <div className="preflight-model"><strong>模型</strong><div><p>{value.model?.requested?(value.model.configured?(value.model.allowed?'已配置，未实测连接':'已配置，当前访问许可不允许使用'):'尚未完成配置'):'本次不调用模型'}{value.model?.name?' · '+value.model.name:''}</p>{value.model?.reason&&<p>{value.model.reason}</p>}</div></div>
 {!!value.materials?.length&&<div className="preflight-materials">{value.materials.map((m:any,i:number)=><article key={m.id||i}><div><strong>{m.filename||m.id}</strong><span className={'material-status '+m.status}>{{accepted:'采用',partial:'部分采用',excluded:'排除'}[m.status as string]||m.status}</span></div><p>{m.reason}</p>{m.accepted_rows!=null&&!reasonIncludesCounts(m)&&<p>采用 {m.accepted_rows} 行{m.excluded_rows?'，排除 '+m.excluded_rows+' 行':''}</p>}{!!m.issues?.length&&<details><summary>查看具体行的检查结果</summary><ul>{m.issues.map((issue:any,n:number)=><li key={n}>第 {issue.row} 行：{issue.reason}</li>)}</ul></details>}</article>)}</div>}
 {!!value.warnings?.length&&<details className="preflight-warnings"><summary>{value.warnings.length} 项提醒</summary><ul>{value.warnings.map((w:string,i:number)=><li key={i}>{w}</li>)}</ul></details>}
 <p className="form-hint">此检查不调用模型。研究开始前还会再次核对材料与访问许可。</p></section>
}
