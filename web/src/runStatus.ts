type Snapshot = {id:string,state:string,updated_at?:string};
type Stream = Pick<EventSource,'onmessage'|'onerror'|'close'>;
type Options = {
  read:(id:string,signal:AbortSignal)=>Promise<Snapshot>;
  onSnapshot:(value:Snapshot)=>void;
  onConnectionError:(message:string|null)=>void;
  open?:(url:string)=>Stream;
  schedule?:(callback:()=>void,ms:number)=>ReturnType<typeof setTimeout>;
  clear?:(timer:ReturnType<typeof setTimeout>)=>void;
};

/** Keep one selected run live through transient SSE and HTTP failures. */
export function watchRunStatus(id:string,options:Options){
  const open=options.open||((url:string)=>new EventSource(url));
  const schedule=options.schedule||setTimeout,clear=options.clear||clearTimeout;
  let stopped=false,stream:Stream|null=null,retry:ReturnType<typeof setTimeout>|undefined;
  let request:AbortController|undefined,requestTimeout:ReturnType<typeof setTimeout>|undefined;
  let recovering=false,failures=0,latest='';
  function closeStream(){const current=stream;stream=null;current?.close()}
  function stop(){
    stopped=true;closeStream();
    if(retry!==undefined)clear(retry);
    if(requestTimeout!==undefined)clear(requestTimeout);
    request?.abort();
  }
  function publish(value:Snapshot){
    if(stopped||value?.id!==id||typeof value.state!=='string')return;
    if(value.updated_at&&latest&&value.updated_at<latest)return;
    latest=value.updated_at||latest;
    options.onConnectionError(null);
    if(!['queued','running'].includes(value.state))stop();
    options.onSnapshot(value);
  }
  async function recover(){
    if(stopped||recovering)return;
    recovering=true;closeStream();
    options.onConnectionError('研究状态连接暂时中断，正在自动重连。');
    request=new AbortController();
    requestTimeout=schedule(()=>request?.abort(),5000);
    try{publish(await options.read(id,request.signal))}
    catch{if(!stopped)options.onConnectionError('暂时无法获取研究状态，正在自动重试。')}
    finally{
      if(requestTimeout!==undefined)clear(requestTimeout);
      request=undefined;requestTimeout=undefined;recovering=false;
      if(!stopped)retry=schedule(connect,Math.min(1000*2**failures++,10000));
    }
  }
  function connect(){
    retry=undefined;if(stopped)return;
    try{
      const current=open('/api/runs/'+id+'/events');stream=current;
      current.onmessage=event=>{
        if(stopped||stream!==current)return;
        try{const value=JSON.parse(event.data);publish(value);failures=0}
        catch{void recover()}
      };
      current.onerror=()=>{if(!stopped&&stream===current)void recover()};
    }catch{void recover()}
  }
  connect();
  return stop;
}
