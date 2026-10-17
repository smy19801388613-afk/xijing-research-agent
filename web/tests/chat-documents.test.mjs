import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import test from 'node:test';
import ts from 'typescript';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

const require=createRequire(import.meta.url);
function loadTs(relative,stubs={}){
  const source=readFileSync(new URL(relative,import.meta.url),'utf8');
  const output=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText;
  const module={exports:{}};
  new Function('require','module','exports',output)(name=>name in stubs?stubs[name]:require(name),module,module.exports);
  return module.exports;
}
const {createDocumentLoader,documentLoadKey}=loadTs('../src/chatDocuments.ts');
const {toolNames,toolLabel}=loadTs('../src/toolNames.ts');
const {DocumentLinks}=loadTs('../src/ResearchDocuments.tsx',{'./api':{},'./CompanySearch':{}});
const {State:FinancialPeriodSourceState}=loadTs('../src/FinancialPeriodsPanel.tsx',{'./api':{},'./financial-periods.css':{}});
const defer=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
const tick=()=>Promise.resolve();

test('streamed conversation timestamps do not repeatedly clear or refetch attachments',async()=>{
  const chat={id:'a',run_id:'r',state:'running',document_ids:['pdf'],updated_at:'first'};
  const request=defer(),published=[];let calls=0;
  const loader=createDocumentLoader(()=>{calls++;return request.promise},value=>published.push(value));
  const key=documentLoadKey(chat,{id:'r',document_ids:['pdf']});
  const first=loader.load('a',key);await tick();
  for(let i=0;i<30;i++)assert.equal(loader.load('a',documentLoadKey({...chat,updated_at:String(i)},{id:'r',document_ids:['pdf']})),first);
  // Await only after resolving the deliberately slow document lookup.
  request.resolve([{id:'pdf',title:'年报'}]);await first;
  assert.equal(calls,1);assert.deepEqual(published.at(-1).documents,[{id:'pdf',title:'年报'}]);
  assert.equal(documentLoadKey(chat,{id:'other',document_ids:['unrelated']}),documentLoadKey(chat,null));
});

test('refresh keeps the same conversation documents visible and loads newly attached material',async()=>{
  const second=defer(),published=[];let calls=0;
  const loader=createDocumentLoader(()=>++calls===1?Promise.resolve([{id:'pdf-1'}]):second.promise,value=>published.push(value));
  await loader.load('a','one');const refresh=loader.load('a','two');await tick();
  assert.deepEqual(published.at(-1).documents,[{id:'pdf-1'}]);assert.equal(published.at(-1).loading,true);
  second.resolve([{id:'pdf-1'},{id:'pdf-2'}]);await refresh;
  assert.equal(published.at(-1).documents.length,2);
});

test('late old conversation response cannot replace the newly selected materials',async()=>{
  const requests={a:defer(),b:defer()},published=[];
  const loader=createDocumentLoader(id=>requests[id].promise,value=>published.push(value));
  const old=loader.load('a','a1');await tick();const current=loader.load('b','b1');await tick();
  requests.b.resolve([{id:'b-pdf'}]);await current;
  requests.a.resolve([{id:'a-pdf'}]);await old;
  assert.equal(published.at(-1).conversationId,'b');assert.deepEqual(published.at(-1).documents,[{id:'b-pdf'}]);
});

test('load failure retains already loaded documents and permits explicit retry',async()=>{
  const published=[];let calls=0;
  const loader=createDocumentLoader(()=>++calls===2?Promise.reject(new Error('network')):Promise.resolve([{id:'pdf'}]),value=>published.push(value));
  await loader.load('a','one');await loader.load('a','two');
  assert.deepEqual(published.at(-1).documents,[{id:'pdf'}]);assert.equal(published.at(-1).error,'network');
  await loader.load('a','two',true);assert.equal(published.at(-1).error,'');assert.equal(calls,3);
});

test('unmounted or replaced loader ignores its outstanding request',async()=>{
  const request=defer(),published=[];const loader=createDocumentLoader(()=>request.promise,value=>published.push(value));
  const old=loader.load('a','key');await tick();loader.cancel();const count=published.length;
  request.resolve([{id:'late'}]);await old;assert.equal(published.length,count);
  await loader.load('a','key');assert.equal(published.at(-1).documents[0].id,'late');
});

test('loading and errors are not presented as a confirmed zero-document list',()=>{
  const loading=renderToStaticMarkup(React.createElement(DocumentLinks,{documents:[],loading:true}));
  assert.match(loading,/正在加载已接入材料/);assert.doesNotMatch(loading,/已接入材料 · 0/);
  const failed=renderToStaticMarkup(React.createElement(DocumentLinks,{documents:[],error:'network',onRetry(){}}));
  assert.match(failed,/重试加载/);assert.doesNotMatch(failed,/已接入材料 · 0/);
  const retained=renderToStaticMarkup(React.createElement(DocumentLinks,{documents:[{id:'pdf',title:'年报',page_count:259}],loading:true}));
  assert.match(retained,/已接入材料 · 1/);assert.match(retained,/年报/);assert.match(retained,/259/);
});

test('every registered agent tool has a Chinese display label, with a safe unknown fallback',()=>{
  const registry=readFileSync(new URL('../../app/tool_capabilities.py',import.meta.url),'utf8').split('LABELS =')[0];
  const tools=Array.from(registry.matchAll(/^    '([^']+)':/gm),match=>match[1]);
  assert.ok(tools.includes('calculate_sensitivity'));
  for(const tool of tools){assert.ok(toolNames[tool],tool);assert.match(toolLabel(tool),/[\u3400-\u9fff]/)}
  assert.equal(toolLabel('calculate_sensitivity'),'计算估值敏感性');
  assert.equal(toolLabel('future_internal_tool'),'研究步骤');
  assert.equal(toolLabel('future_internal_tool','补充公开资料'),'补充公开资料');
});

test('reused financial facts display Chinese provenance and retain original verification level',()=>{
  const render=props=>renderToStaticMarkup(React.createElement(FinancialPeriodSourceState,props)).replace(/<[^>]*>/g,'');
  const aggregate=render({value:'fact_ledger'});
  assert.equal(aggregate,'已存事实复用 · 保留原核验状态');assert.doesNotMatch(aggregate,/fact_ledger|已核验/);
  const record=render({value:'fact_ledger',verification:{state:'api_single',label:'接口单源'}});
  assert.match(record,/原核验状态：接口单源/);assert.doesNotMatch(record,/已核验|双源/);
  assert.equal(render({value:'source_changed',verification:{label:'接口单源'}}),'来源变化 · 已阻断');
  assert.equal(render({value:'future_internal_status'}),'来源状态待确认');
});
