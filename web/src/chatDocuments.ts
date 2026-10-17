export type DocumentState={conversationId:string,documents:any[],loading:boolean,error:string};

export function documentLoadKey(chat:any,run:any){
  if(!chat?.id)return '';
  const linked=run?.id===chat.run_id?run:null;
  const ids=[...(chat.document_ids||[]),...(linked?.document_ids||[]),...(linked?.document_id?[linked.document_id]:[])];
  // Model streaming timestamps do not change material membership.
  return JSON.stringify([chat.id,chat.run_id||'',Array.from(new Set(ids)).sort(),chat.state==='running']);
}

export function createDocumentLoader(fetchDocuments:(id:string)=>Promise<any[]>,publish:(value:DocumentState)=>void){
  let version=0,key='',pending:Promise<void>|null=null;
  let state:DocumentState={conversationId:'',documents:[],loading:false,error:''};
  function load(id:string,nextKey:string,force=false){
    if(!force&&key===nextKey)return pending||Promise.resolve();
    key=nextKey;const ticket=++version;
    state={conversationId:id,documents:state.conversationId===id?state.documents:[],loading:!!id,error:''};publish(state);
    if(!id){pending=null;return Promise.resolve()}
    const work=Promise.resolve().then(()=>fetchDocuments(id)).then(rows=>{
      if(ticket!==version)return;
      state={conversationId:id,documents:rows,loading:false,error:''};publish(state);
    }).catch(error=>{
      if(ticket!==version)return;
      state={...state,loading:false,error:error?.message||'材料列表暂时无法加载'};publish(state);
    }).finally(()=>{if(ticket===version)pending=null});
    pending=work;return work;
  }
  return {load,cancel(){version++;key='';pending=null}};
}
