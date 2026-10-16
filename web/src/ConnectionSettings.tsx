import {useEffect,useState} from 'react';
import {Plus,PlugZap,Check,Save,KeyRound,Database,LoaderCircle} from 'lucide-react';
import {api,post} from './api';
import RuntimePolicy from './RuntimePolicy';
import ExecutionBudget,{defaultBudget} from './ExecutionBudget';
export default function ConnectionSettings({config,onChange}:any){
  const [draft,setDraft]=useState<any>(null),[token,setToken]=useState(''),[source,setSource]=useState(config.data_source||'tushare'),[busy,setBusy]=useState(''),[message,setMessage]=useState(''),[error,setError]=useState(''),[models,setModels]=useState<string[]>([]);
  const [budget,setBudget]=useState(()=>defaultBudget(config));
  useEffect(()=>{setBudget(defaultBudget(config))},[JSON.stringify(config.execution_budget)]);
  useEffect(()=>{if(!draft&&config.profiles?.length)edit(config.profiles.find((p:any)=>p.id===config.active_model)||config.profiles[0])},[config]);
  function edit(p:any){setDraft({...p,api_key:'',clear_key:false,activate:false});setModels([]);setError('');setMessage('')}
  function create(){const p=config.presets[0];edit({...p,id:'model-'+Date.now(),name:'新的模型连接',model:p.model,json_mode:true,activate:false})}
  async function action(name:string,fn:()=>Promise<any>){setBusy(name);setError('');setMessage('');try{await fn()}catch(e:any){setError(e.message)}finally{setBusy('')}}
  async function save(){await action('save',async()=>{const s=await post('/connections/profiles',draft);onChange(s);edit(s.profiles.find((p:any)=>p.id===draft.id));setMessage('连接已保存。可测试连接或设为默认。')})}
  function preset(provider:string){const p=config.presets.find((v:any)=>v.provider===provider);setDraft({...draft,...p,name:p.label,api_key:'',clear_key:true});setModels([])}
  if(!draft)return <div className="content-page"><LoaderCircle className="spin"/></div>;
  const saved=config.profiles?.some((p:any)=>p.id===draft.id);
  return <div className="content-page connections-page">
    <div className="section-heading"><div><span className="eyebrow">YOUR CONNECTIONS</span><h1>连接你的研究工具</h1><p className="small-muted">自己的 API、自己的模型。每套连接分别保存，可随时切换。</p></div><button className="secondary" onClick={create}><Plus size={15}/>添加连接</button></div>
    {error&&<div className="inline-error" role="alert">{error}</div>}{message&&<div className="inline-success" role="status"><Check size={15}/>{message}</div>}
    <div className="connections-layout"><aside className="connection-list">
      {config.profiles?.map((p:any)=><button className={'connection-choice '+(draft.id===p.id?'selected':'')} key={p.id} onClick={()=>edit(p)}><PlugZap size={18}/><span><strong>{p.name}</strong><small>{p.model||'未填写模型'}</small></span>{config.active_model===p.id&&<span className="badge green">默认</span>}</button>)}
      <div className="security-hint"><KeyRound size={16}/><p>密钥使用当前 Windows 用户加密保存。留空会保留已存密钥；切换厂商时需重新填写。</p></div>
    </aside><section className="panel profile-form"><header><h2>模型连接</h2><span className="badge">{draft.key_configured?'已存密钥':'未存密钥'}</span></header>
      <div className="form-grid"><label>连接名称<input value={draft.name} onChange={e=>setDraft({...draft,name:e.target.value})}/></label><label>服务商<select value={draft.provider} onChange={e=>preset(e.target.value)}>{config.presets?.map((p:any)=><option value={p.provider} key={p.provider}>{p.label}</option>)}</select></label></div>
      <label>API 服务地址<input value={draft.base} placeholder="https://api.example.com/v1" onChange={e=>setDraft({...draft,base:e.target.value})}/></label>
      <div className="form-grid"><label>模型名称<input list="provider-models" placeholder="按服务商提供的模型名填写" value={draft.model} onChange={e=>setDraft({...draft,model:e.target.value})}/><datalist id="provider-models">{models.map(m=><option key={m} value={m}/>)}</datalist></label><label>接口协议<select value={draft.protocol} onChange={e=>setDraft({...draft,protocol:e.target.value})}><option value="openai">OpenAI 兼容 Chat Completions</option><option value="anthropic">Anthropic Messages</option></select></label></div>
      <label>API Key<input type="password" autoComplete="new-password" placeholder={draft.key_configured?'已保存，留空保留现有密钥':'填写你的 API Key；本地 Ollama 可留空'} value={draft.api_key} onChange={e=>setDraft({...draft,api_key:e.target.value,clear_key:false})}/></label>
      <div className="profile-toggles"><label><input type="checkbox" checked={draft.json_mode} onChange={e=>setDraft({...draft,json_mode:e.target.checked})}/>请求 JSON 输出（兼容接口）</label><label><input type="checkbox" checked={draft.clear_key} onChange={e=>setDraft({...draft,clear_key:e.target.checked})}/>清除原密钥</label></div>
      <div className="button-row wrap"><button className="primary" disabled={!!busy||!draft.model||!draft.base} onClick={save}><Save size={15}/>保存连接</button>
      <button className="secondary" disabled={!!busy||!saved} onClick={()=>action('test',async()=>{const r=await post('/connections/'+draft.id+'/test');setMessage(r.detail+' · '+r.meta.model+' · '+(r.meta.elapsed_ms/1000).toFixed(1)+' 秒')})}><PlugZap size={15}/>{busy==='test'?'测试中…':'测试已保存连接'}</button>
      <button className="secondary" disabled={!!busy||!saved} onClick={()=>action('models',async()=>{const r=await api('/connections/'+draft.id+'/models');setModels(r.models);setMessage('已读取 '+r.models.length+' 个模型，可在模型名称输入框中选择并保存')})}>读取模型列表</button>
      <button className="text-link" disabled={!!busy||!saved||config.active_model===draft.id} onClick={()=>action('activate',async()=>{onChange(await post('/connections/preferences',{active_model:draft.id}));setMessage('已设为默认模型连接')})}>设为默认</button></div>
      <p className="small-muted form-help">测试和模型列表使用已保存的连接；修改后请先保存。连接测试会产生一次很小的模型调用。</p>
    </section></div>
    <ExecutionBudget config={config} value={budget} onChange={setBudget} onDefaultsSaved={onChange} disabled={!!busy} settings/>
    <section className="panel data-connection"><header><Database size={19}/><h2>财务数据来源</h2></header><div className="source-choices">{config.data_sources?.map((p:any)=><label key={p.id} className={source===p.id?'selected':''}><input type="radio" name="source" value={p.id} checked={source===p.id} onChange={()=>setSource(p.id)}/><span><strong>{p.name}</strong><small>{p.description}</small></span></label>)}</div>
      <label>Tushare Token <span className="small-muted">{config.tushare_configured?'· 已配置，留空保留':''}</span><input type="password" autoComplete="new-password" value={token} placeholder="使用自己的 Token；公开财务或文件模式不需要 Token" onChange={e=>setToken(e.target.value)}/></label>
      <div className="button-row"><button className="primary" disabled={!!busy} onClick={()=>action('data',async()=>{onChange(await post('/connections/preferences',{data_source:source,tushare_token:token}));setToken('');setMessage('数据连接已保存，下次新研究使用该来源')})}><Save size={15}/>保存数据连接</button><button className="text-link" disabled={!!busy||!config.tushare_configured} onClick={()=>action('clear-token',async()=>{onChange(await post('/connections/preferences',{clear_tushare:true}));setToken('');setMessage('已清除 Tushare Token')})}>清除 Token</button></div>
      <p className="small-muted form-help">公司目录使用 Tushare 每日缓存；没有 Token 时可用本机历史目录或直接填写完整证券代码。每份研究会记录实际使用的数据源。</p>
    </section>
    <RuntimePolicy/>
  </div>
}
