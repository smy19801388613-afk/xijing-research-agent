import {useId,useState} from 'react';
import {Check,ChevronDown,LoaderCircle,RotateCcw,Save,SlidersHorizontal} from 'lucide-react';
import {post} from './api';
import './execution-budget.css';

export type Budget={generation_tokens:number,time_minutes:number,max_decisions:number};
type BudgetKey=keyof Budget;
const fallbackBudget:Budget={generation_tokens:49152,time_minutes:15,max_decisions:8};
const fallbackBounds={generation_tokens:{min:4096,max:65536,step:1024},time_minutes:{min:1,max:60,step:1},max_decisions:{min:2,max:32,step:1}};
const fields:{key:BudgetKey,label:string,unit:string,hint:string}[]=[
  {key:'generation_tokens',label:'单次生成额度',unit:'tokens',hint:'含推理与正文；不是整轮总量，重试也受此上限约束。'},
  {key:'time_minutes',label:'每轮执行时间',unit:'分钟',hint:'达到时限后结束本轮，保留已完成的结果。'},
  {key:'max_decisions',label:'每轮模型决策',unit:'次',hint:'不包含报告工具内部的模型调用和截断重试。'},
];
export function budgetBounds(config:any,key:BudgetKey){return {...fallbackBounds[key],...config.execution_budget_bounds?.[key]}}
export function normalizeBudget(value:any,config:any={}):Budget{
  const defaults={...fallbackBudget,...config.execution_budget_defaults};
  return Object.fromEntries(fields.map(({key})=>{const {min,max}=budgetBounds(config,key),raw=Number(value?.[key]??defaults[key]),n=Number.isFinite(raw)?raw:defaults[key];return [key,Math.min(max,Math.max(min,n))]})) as Budget;
}
export function defaultBudget(config:any):Budget{return normalizeBudget(config.execution_budget,config)}
export function budgetSummary(value:Budget){return `${value.generation_tokens.toLocaleString('zh-CN')} tokens · ${value.time_minutes} 分钟 · ${value.max_decisions} 次决策`}

export default function ExecutionBudget({config,value,onChange,onDefaultsSaved,disabled=false,running=false,settings=false}:any){
  const id=useId(),[saving,setSaving]=useState(false),[notice,setNotice]=useState(''),[error,setError]=useState('');
  const resetValue=settings?normalizeBudget(config.execution_budget_defaults,config):defaultBudget(config);
  const sameAsSaved=JSON.stringify(normalizeBudget(value,config))===JSON.stringify(defaultBudget(config));
  function change(next:Budget){setNotice('');setError('');onChange(next)}
  async function save(){
    setSaving(true);setNotice('');setError('');
    try{const next=await post('/connections/preferences',{execution_budget:value});onDefaultsSaved?.(next);onChange(defaultBudget(next));setNotice('默认预算已保存，后续新轮次使用。')}
    catch(e:any){setError(e.message||'保存失败，请重试。')}
    finally{setSaving(false)}
  }
  return <details className={'execution-budget'+(settings?' budget-settings':'')+(running?' budget-running':'')}>
    <summary><SlidersHorizontal size={15}/><strong>{settings?'默认执行预算':running?'本轮执行预算':'执行预算'}</strong><span className="budget-summary">{budgetSummary(value)}</span><ChevronDown className="budget-chevron" size={14}/></summary>
    <div className="budget-content">
      {running&&<p className="budget-running-note" role="status">本轮使用以下预算，结束后可调整下一轮。</p>}
      <div className="budget-controls">{fields.map(({key,label,unit,hint})=>{
        const bounds=budgetBounds(config,key),controlId=id+'-'+key,hintId=controlId+'-hint';
        return <div className="budget-control" key={key}><div className="budget-control-heading"><label htmlFor={controlId}>{label}</label><output htmlFor={controlId}>{value[key].toLocaleString('zh-CN')} <span>{unit}</span></output></div><input id={controlId} type="range" {...bounds} value={value[key]} disabled={disabled||saving} aria-describedby={hintId} aria-valuetext={`${value[key].toLocaleString('zh-CN')} ${unit}`} onChange={e=>change({...value,[key]:Number(e.target.value)})}/><p id={hintId}>{hint}</p></div>
      })}</div>
      <div className="budget-footer"><p>这是上限，实际用量可能更少。调高可能增加等待和费用。</p><div className="budget-actions"><button type="button" className="text-link" disabled={disabled||saving} onClick={()=>change(resetValue)}><RotateCcw size={13}/>{settings?'恢复推荐值':'恢复默认'}</button><button type="button" className="secondary" disabled={disabled||saving||sameAsSaved} onClick={save}>{saving?<LoaderCircle className="spin" size={13}/>:<Save size={13}/>}保存为默认</button></div></div>
      {notice&&<p className="budget-save-status" role="status"><Check size={13}/>{notice}</p>}{error&&<p className="form-error" role="alert">默认预算未保存：{error}</p>}
    </div>
  </details>
}
