import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source=readFileSync(new URL('../src/runStatus.ts',import.meta.url),'utf8');
const output=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
const module={exports:{}};
new Function('module','exports',output)(module,module.exports);
const {watchRunStatus}=module.exports;
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
function harness(read=()=>Promise.resolve({id:'r',state:'running'})){
  let next=0;const timers=new Map(),streams=[],snapshots=[],notices=[],requests=[];
  const stop=watchRunStatus('r',{
    read:(id,signal)=>{requests.push({id,signal});return read(id,signal)},
    onSnapshot:value=>snapshots.push(value),onConnectionError:message=>notices.push(message),
    schedule:(fn,ms)=>{timers.set(++next,{fn,ms});return next},clear:id=>timers.delete(id),
    open:url=>{const value={url,closed:false,onmessage:null,onerror:null,close(){this.closed=true}};streams.push(value);return value},
  });
  const fire=()=>{const [id,{fn}]=[...timers].sort((a,b)=>a[1].ms-b[1].ms)[0];timers.delete(id);fn()};
  return {stop,timers,streams,snapshots,notices,requests,fire};
}
function emit(stream,value){stream.onmessage({data:JSON.stringify(value)})}

test('SSE failure with a still-running GET reconnects and receives terminal state',async()=>{
  const h=harness();h.streams[0].onerror();await tick();
  assert.equal(h.requests.length,1);assert.equal(h.streams[0].closed,true);
  assert.equal(h.snapshots.at(-1).state,'running');assert.equal(h.timers.size,1);
  h.fire();assert.equal(h.streams.length,2);
  emit(h.streams[1],{id:'r',state:'completed_with_gaps'});
  assert.equal(h.snapshots.at(-1).state,'completed_with_gaps');assert.equal(h.streams[1].closed,true);
  assert.equal(h.timers.size,0);assert.equal(h.notices.at(-1),null);
});

test('HTTP and SSE failures continue retrying until the server recovers',async()=>{
  let calls=0;const h=harness(()=>++calls<3?Promise.reject(new Error('offline')):Promise.resolve({id:'r',state:'failed'}));
  for(let i=0;i<3;i++){
    h.streams.at(-1).onerror();await tick();
    if(i<2){assert.equal(h.timers.size,1);h.fire()}
  }
  assert.equal(calls,3);assert.equal(h.snapshots.at(-1).state,'failed');assert.equal(h.timers.size,0);
});

test('a pending fallback is aborted on selection change and cannot overwrite the new page',async()=>{
  const request=deferred(),h=harness(()=>request.promise);
  h.streams[0].onerror();h.stop();
  assert.equal(h.requests[0].signal.aborted,true);assert.equal(h.timers.size,0);
  request.resolve({id:'r',state:'completed_with_gaps'});await tick();
  assert.deepEqual(h.snapshots,[]);assert.equal(h.streams.length,1);assert.equal(h.timers.size,0);
});

test('malformed SSE and a hanging fallback recover after bounded HTTP timeout',async()=>{
  const h=harness((id,signal)=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted')))));
  h.streams[0].onmessage({data:'invalid json'});assert.equal(h.requests.length,1);
  h.fire();await tick();assert.equal(h.requests[0].signal.aborted,true);
  assert.equal(h.timers.size,1);h.fire();assert.equal(h.streams.length,2);h.stop();
});

test('terminal fallback and queued old events cannot restart or regress a finished run',async()=>{
  const h=harness(()=>Promise.resolve({id:'r',state:'cancelled'}));const old=h.streams[0];
  old.onerror();await tick();emit(old,{id:'r',state:'running'});
  assert.equal(h.snapshots.length,1);assert.equal(h.snapshots[0].state,'cancelled');assert.equal(h.timers.size,0);
});

test('out-of-order and wrong-run snapshots are ignored',()=>{
  const h=harness(),stream=h.streams[0];
  emit(stream,{id:'r',state:'running',updated_at:'2026-10-17T10:02:00Z'});
  emit(stream,{id:'other',state:'failed'});
  emit(stream,{id:'r',state:'queued',updated_at:'2026-10-17T10:01:00Z'});
  assert.equal(h.snapshots.length,1);h.stop();
});
