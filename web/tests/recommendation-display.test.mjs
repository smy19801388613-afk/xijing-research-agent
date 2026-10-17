import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import test from 'node:test';
import ts from 'typescript';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

const require=createRequire(import.meta.url);
const source=readFileSync(new URL('../src/InvestmentRecommendation.tsx',import.meta.url),'utf8');
const output=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText;
const module={exports:{}};
const stubs={'./investment-recommendation.css':{},'./ResearchText':{__esModule:true,default:({text})=>React.createElement('p',null,text)},'./ValuationLab':{AssumptionChips:()=>null}};
new Function('require','module','exports',output)(name=>name in stubs?stubs[name]:require(name),module,module.exports);
const InvestmentRecommendation=module.exports.default;

test('recommendation formats prices and scenario space without changing frozen Decimal values',()=>{
  const precise='34.55639264160381512676832788';
  const quote={status:'available',price:'28.2',currency:'CNY',trade_date:'2026-10-16',source_id:'quote',decision_eligible:true};
  const memo={sources:[{id:'quote',title:'合成行情'}],investment_recommendation:{action:'buy',status:'conditional',summary:'合成展示验收',horizon:'六个月',confidence:'medium',
    new_position:'条件未满足，暂不建仓。',existing_position:'先核查条件。',market_snapshot:quote,evidence_ids:['quote'],valuation_scenario_ids:['scenario'],
    valuation_basis:[{scenario_id:'scenario',method_name:'PE',output:{price:precise,equity_value_100m:'2185.816468086090'}}],
    price_comparisons:[{scenario_id:'scenario',scenario_price:precise,current_price:'28.2',currency:'CNY',trade_date:'2026-10-16',upside_pct:'22.5404',assumption_status:'user_scenario'}]}};
  const original=JSON.stringify(memo);
  const html=renderToStaticMarkup(React.createElement(InvestmentRecommendation,{memo}));
  assert.match(html,/条件满足后买入/);assert.match(html,/34\.56/);assert.match(html,/28\.20/);assert.match(html,/22\.54/);
  assert.doesNotMatch(html,/34\.55639264160381512676832788/);assert.doesNotMatch(html,/22\.5404/);
  assert.equal(JSON.stringify(memo),original);
});

test('known recommendation field tokens become Chinese display labels without rewriting the memo',()=>{
  const memo={investment_recommendation:{action:'watch',status:'conditional',summary:'参照revisit_triggers重新评估。',horizon:'六个月',confidence:'medium',
    new_position:'满足buy_conditions后再建仓。',existing_position:'按exit_conditions减持。',key_risks:['检查revisit_triggers的变化。'],
    evidence_ids:[],valuation_scenario_ids:[]}};
  const original=JSON.stringify(memo),html=renderToStaticMarkup(React.createElement(InvestmentRecommendation,{memo}));
  assert.match(html,/参照重新评估条件重新评估/);assert.match(html,/满足建仓或加仓条件后再建仓/);assert.match(html,/按减仓或退出条件减持/);
  assert.doesNotMatch(html,/exit_conditions|buy_conditions|revisit_triggers/);assert.equal(JSON.stringify(memo),original);
});
