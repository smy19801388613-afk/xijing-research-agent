import {ArrowUpRight,History,RefreshCw} from 'lucide-react';

export const reportSyncPrompt='请仅依据当前已保存的研究问题、最新答案、支持与反方证据，更新并保存研究备忘录。逐项核对问题清单与现稿的差异，修正过时判断，保留未解决的缺口和有效的已有分析；不重新联网，不改变估值参数。保留原报告版本，并说明本次更新了什么。';

export default function ReportSyncNotice({run,onSync,onHistory}:any){
 const sync=run?.report_sync;
 if(!sync||!run.memo||(!sync.needs_update&&sync.status!=='untracked'))return null;
 return <div className={'report-sync '+sync.status} role="status"><div><RefreshCw size={18}/><div><strong>{sync.needs_update?'有研究进展尚未同步到报告':'报告与问题清单尚未建立对应记录'}</strong><p>{sync.message}</p></div></div><div className="button-row">{onSync&&<button className="primary" disabled={['queued','running'].includes(run.state)} onClick={onSync}>通过对话更新报告<ArrowUpRight size={14}/></button>}{onHistory&&<button className="text-link" onClick={onHistory}><History size={14}/>查看旧稿</button>}</div></div>
}
