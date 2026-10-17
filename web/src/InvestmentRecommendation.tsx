import {ArrowUpRight,ChevronDown,ExternalLink} from 'lucide-react';
import ResearchText from './ResearchText';
import {AssumptionChips} from './ValuationLab';
import './investment-recommendation.css';

const actions:Record<string,string>={buy:'买入',hold:'持有',reduce:'减持',avoid:'回避',watch:'观察'};
const confidence:Record<string,string>={low:'低',medium:'中',high:'高'};
const bases:Record<string,string>={valuation:'估值判断',fundamental_risk:'经营或治理风险',evidence_gap:'证据缺口'};
const fieldLabels:Record<string,string>={exit_conditions:'减仓或退出条件',buy_conditions:'建仓或加仓条件',revisit_triggers:'重新评估条件'};
function recommendationText(value:any){return String(value??'').replace(/(?<![A-Za-z0-9_])(?:exit_conditions|buy_conditions|revisit_triggers)(?![A-Za-z0-9_])/g,key=>fieldLabels[key]);}
const discussPrompt='请基于当前报告、实际查阅的证据和已保存估值，补充明确的投资建议：买入、持有、减持、回避或观察，分别说明未持仓与已有持仓的动作、期限、支持理由、买入与退出条件、主要风险及何时改变建议。核查截止日内可得的市场价格；数据或估值不足时明确暂不决策或条件建议，不编造目标价。保存前检查与原报告的一致性。';

function safeUrl(value:any){
  if(typeof value!=='string')return undefined;
  if(/^https?:\/\//i.test(value)||value.startsWith('/api/documents/'))return value;
  return undefined;
}
function validDecimal(value:any){return (typeof value==='string'||typeof value==='number')&&String(value).trim()!==''&&Number.isFinite(Number(value));}
function sameDecimal(left:any,right:any){return validDecimal(left)&&validDecimal(right)&&Number(left)===Number(right);}
function displayAmount(value:any){return validDecimal(value)?Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';}
function displayPercent(value:any){return validDecimal(value)?Number(value).toLocaleString('zh-CN',{maximumFractionDigits:2}):'—';}
function marketSignature(value:any){
  return JSON.stringify([value?.status,value?.ticker,validDecimal(value?.price)?Number(value.price):value?.price,value?.currency,value?.trade_date,value?.as_of]);
}
function Points({title,items}:any){
  if(!Array.isArray(items)||!items.length)return null;
  return <div className="recommendation-points"><h4>{title}</h4><ul>{items.map((v:string,i:number)=><li key={i}>{recommendationText(v)}</li>)}</ul></div>;
}

export default function InvestmentRecommendation({memo,run,onDiscuss}:any){
  const rec=memo?.investment_recommendation;
  if(!rec||typeof rec!=='object')return <section className="investment-recommendation recommendation-empty" aria-label="投资建议">
    <span className="recommendation-eyebrow">投资建议</span><h3>此版本尚未形成明确建议</h3><p>研究判断已保留，可继续讨论建仓、持仓与退出条件。</p>
    {onDiscuss&&<button className="text-link recommendation-discuss" onClick={()=>onDiscuss(discussPrompt)}>讨论并补充建议 <ArrowUpRight size={15}/></button>}
  </section>;
  const knownAction=actions[rec.action];
  const insufficient=rec.status==='insufficient_evidence'||!['actionable','conditional'].includes(rec.status)||!knownAction;
  const conditional=rec.status==='conditional'&&!insufficient;
  const title=insufficient?'暂不决策':rec.action==='watch'?'观察，暂不行动':conditional?`条件满足后${knownAction}`:knownAction;
  const quote=rec.market_snapshot;
  const hasFrozenSources=Object.prototype.hasOwnProperty.call(memo,'recommendation_sources');
  const frozenSources=hasFrozenSources?(Array.isArray(memo.recommendation_sources)?memo.recommendation_sources:[]):(memo.sources||[]);
  const quoteSource=quote?.source_id&&(frozenSources.some((s:any)=>s.id===quote.source_id)||(!hasFrozenSources&&quote.text&&safeUrl(quote.source_url)));
  const hasQuote=quote?.status==='available'&&validDecimal(quote.price)&&Number(quote.price)>0&&quote.trade_date&&quoteSource;
  const sources=(rec.evidence_ids||[]).map((id:string)=>frozenSources.find((s:any)=>s.id===id)||{id,title:id,missing:true});
  const scenarios=(rec.valuation_scenario_ids||[]).map((id:string)=>(rec.valuation_basis||[]).find((v:any)=>v.scenario_id===id)||(memo.valuation_context?.scenarios||[]).find((v:any)=>v.id===id)||{scenario_id:id,missing:true});
  const comparisons=hasQuote&&quote.decision_eligible===true&&Array.isArray(rec.price_comparisons)?rec.price_comparisons.filter((p:any)=>{
    const basis=(rec.valuation_basis||[]).find((v:any)=>v.scenario_id===p.scenario_id);
    return rec.valuation_scenario_ids?.includes(p.scenario_id)&&basis&&sameDecimal(p.scenario_price,basis.output?.price)&&Number(p.scenario_price)>0&&sameDecimal(p.current_price,quote.price)&&p.currency===quote.currency&&p.trade_date===quote.trade_date&&p.assumption_status==='user_scenario'&&validDecimal(p.upside_pct);
  }):[];
  const marketChanged=run?.market_snapshot&&marketSignature(run.market_snapshot)!==marketSignature(quote);
  const cutoffChanged=run?.request?.as_of&&rec.as_of&&run.request.as_of!==rec.as_of;
  const freshness=run?.report_sync?.recommendation_freshness;
  const missingSnapshot=memo.recommendation_source_gaps||[];
  return <section className={`investment-recommendation ${insufficient?'recommendation-insufficient':''}`} aria-label="投资建议">
    <div className="recommendation-heading"><div><span className="recommendation-eyebrow">投资建议{bases[rec.basis]?' · '+bases[rec.basis]:''}</span><h3>{title}</h3></div><div className="recommendation-meta"><span>{recommendationText(rec.horizon||'期限待明确')}</span><span>置信度 · {confidence[rec.confidence]||'待评估'}</span>{rec.as_of&&<span>截至 {rec.as_of}</span>}</div></div>
    {(marketChanged||cutoffChanged||freshness?.needs_update||missingSnapshot.length>0)&&<div className="recommendation-frozen-notice" role="note"><strong>本建议的依据需要重新核验</strong><p>本建议保留原快照与原判断，局部文字修改不会更新依据。</p>{freshness?.reasons?.length?<ul>{freshness.reasons.map((reason:string,i:number)=><li key={i}>{reason}</li>)}</ul>:<p>{missingSnapshot.length?'缺少当时冻结的来源版本：'+missingSnapshot.join('、'):marketChanged?'当前行情资料已变化。':cutoffChanged?'研究截止日已变化。':''}</p>}</div>}
    <div className="recommendation-positions"><div><h4>未持仓</h4><p>{recommendationText(rec.new_position||'行动待明确')}</p></div><div><h4>已有持仓</h4><p>{recommendationText(rec.existing_position||'行动待明确')}</p></div></div>
    <div className="recommendation-pricing">{hasQuote&&<div className="recommendation-market"><span>{quote.decision_eligible===false?'参考收盘价（适用性待核实）':'参考收盘价'} <strong>{displayAmount(quote.price)} <small>{quote.currency||''}</small></strong></span><span>{quote.ticker?quote.ticker+' · ':''}交易日 {quote.trade_date}</span>{safeUrl(quote.source_url)&&<a href={safeUrl(quote.source_url)} target="_blank" rel="noreferrer">行情来源 <ExternalLink size={12}/></a>}</div>}
    {comparisons.length>0&&<div className="recommendation-comparisons">{comparisons.map((p:any,i:number)=><div key={p.scenario_id||i}><span>估值情景</span><strong>{displayAmount(p.scenario_price)} <small>{p.currency}</small></strong><span>相对所列参考价的假设空间 {displayPercent(p.upside_pct)}%</span></div>)}<p className="recommendation-qualifier">来自已保存的用户假设情景，不代表收益预测。</p></div>}</div>
    <div className="recommendation-summary"><h4>建议理由</h4><ResearchText text={recommendationText(rec.summary)}/></div>
    <div className="recommendation-conditions"><Points title="建仓或加仓条件" items={rec.buy_conditions}/><Points title="减仓或退出条件" items={rec.exit_conditions}/></div>
    <Points title={insufficient?'当前决策缺口':'仍需核实'} items={rec.blocking_gaps}/>
    <details className="recommendation-details"><summary><span>风险、调整条件与依据</span><ChevronDown size={16}/></summary>
      <div className="recommendation-conditions"><Points title="主要风险" items={rec.key_risks}/><Points title="何时调整建议" items={rec.revisit_triggers}/></div>
      {hasQuote&&quote.decision_eligible===false&&<Points title="行情适用限制" items={quote.decision_gaps}/>}
      {!!sources.length&&<div className="recommendation-evidence"><h4>建议依据</h4>{sources.map((s:any)=><details key={s.id}><summary>{s.title||s.id}{s.missing?' · 此版本未附来源':''}</summary>{s.text&&<blockquote>{s.text}</blockquote>}{safeUrl(s.url)&&<a href={safeUrl(s.url)} target="_blank" rel="noreferrer">查看原始来源 <ExternalLink size={12}/></a>}</details>)}</div>}
      {!!scenarios.length&&<div className="recommendation-evidence"><h4>本版本引用的估值情景</h4>{scenarios.map((v:any)=><details key={v.scenario_id||v.id}><summary>{v.method_name||v.method||v.scenario_id}{v.missing?' · 此版本未附情景':''}</summary>{!v.missing&&<><AssumptionChips values={v.assumptions||{}}/>{v.output?.price!==undefined&&v.output?.price!==null&&<p>情景每股价值：{displayAmount(v.output.price)}</p>}{v.output?.equity_value_100m!==undefined&&<p>情景股权价值：{displayAmount(v.output.equity_value_100m)} 亿元</p>}{v.formula&&<ResearchText text={typeof v.formula==='string'?v.formula:JSON.stringify(v.formula)}/>}</>}</details>)}</div>}
    </details>
    {onDiscuss&&<button className="text-link recommendation-discuss" onClick={()=>onDiscuss('请讨论当前投资建议：'+rec.summary+'。先核对支持证据、估值假设与当前决策缺口，解释哪些新信息会改变建议；与我确认需要修订的部分。')}>讨论这条建议 <ArrowUpRight size={15}/></button>}
  </section>;
}
