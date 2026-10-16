"""Stop only workers owned by this workspace and this exact execution.

PID alone is never an identity. Windows termination uses the same opened
process handle whose creation time was checked; Linux uses a pidfd, avoiding
PID reuse between validation and signalling. Unknown identities fail closed.
"""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid
from datetime import datetime,timezone
from . import store
from .config import ROOT

START_GRACE_SECONDS=30


def _windows_handle(pid, terminate=False):
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.GetProcessTimes.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
    kernel.TerminateProcess.restype=wintypes.BOOL
    handle=kernel.OpenProcess(0x1000 | (0x0001 if terminate else 0),False,pid)
    if not handle: return kernel,None,None
    times=[wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]):
        kernel.CloseHandle(handle)
        return kernel,None,None
    created=str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    return kernel,handle,created


def process_birth(pid):
    if os.name=='nt':
        kernel,handle,birth=_windows_handle(pid)
        if handle: kernel.CloseHandle(handle)
        return birth
    try:
        # Fields after the final ')' begin at proc field 3; starttime is 22.
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError,IndexError): return None


def terminate_verified(pid,birth):
    if not birth or not isinstance(pid,int) or pid<=0 or pid==os.getpid(): return False
    if os.name=='nt':
        kernel,handle,actual=_windows_handle(pid,terminate=True)
        if not handle: return False
        try: return bool(actual==birth and kernel.TerminateProcess(handle,1))
        finally: kernel.CloseHandle(handle)
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'): return False
    try: fd=os.pidfd_open(pid)
    except OSError: return False
    try:
        if process_birth(pid)!=birth: return False
        signal.pidfd_send_signal(fd,signal.SIGKILL)
        return True
    except OSError: return False
    finally: os.close(fd)


def process_state(pid,birth):
    """Distinguish exited/replaced identities from permission/query failures."""
    if os.name=='nt':
        from ctypes import wintypes
        kernel,handle,actual=_windows_handle(pid)
        if not handle:
            return 'exited' if ctypes.get_last_error()==87 else 'unknown'
        try:
            code=wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype=wintypes.BOOL
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(code)): return 'unknown'
            if code.value!=259: return 'exited'
            return 'current' if actual==birth else 'replaced'
        finally: kernel.CloseHandle(handle)
    actual=process_birth(pid)
    if actual is not None: return 'current' if actual==birth else 'replaced'
    return 'exited' if not Path(f'/proc/{pid}').exists() else 'unknown'


def launch(kind,target,turn_id=''):
    nonce=uuid.uuid4().hex
    with store.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        table='conversations' if kind=='conversation' else 'runs'
        row=c.execute(f'SELECT payload,cancel FROM {table} WHERE id=?',(target,)).fetchone()
        value=json.loads(row[0]) if row else {}
        active=bool(row and not row[1] and (value.get('state')=='running' and value.get('turn_id')==turn_id
                    if kind=='conversation' else value.get('state') in ('queued','running')))
        if not active: raise ValueError('该执行已经结束，不能再次启动旧进程')
        registered=c.execute('SELECT pid,birth FROM workers WHERE kind=? AND target=? AND turn_id=?',(kind,target,turn_id)).fetchone()
        if registered and process_state(*registered) not in ('exited','replaced'):
            raise ValueError('该研究执行已启动')
        previous=value.get('execution_launch') or {}
        if previous and (kind=='run' or previous.get('turn_id')==turn_id):
            raise ValueError('该执行已提交启动，请等待状态恢复后创建新的重试')
        value['execution_launch']={'nonce':nonce,'requested_at':store.now(),'turn_id':turn_id}
        if kind=='run': value['_write_version']=value.get('_write_version',0)+1
        c.execute(f'UPDATE {table} SET payload=? WHERE id=?',(json.dumps(value,ensure_ascii=False),target))
    folder=store.DATA/('conversations' if kind=='conversation' else 'runs')/target
    module='app.agent_worker' if kind=='conversation' else 'app.worker'
    args=[sys.executable,'-u','-m',module,target]
    if kind=='conversation': args.append(turn_id)
    # Explicit workspace routing also makes isolated integration tests safe.
    env=os.environ.copy();env['RESEARCH_DATA_DIR']=str(store.DATA.resolve())
    try:
        folder.mkdir(parents=True,exist_ok=True)
        with (folder/'worker.log').open('ab') as log:
            process=subprocess.Popen(args,cwd=ROOT,stdout=log,stderr=log,env=env,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except Exception:
        store.fail_execution(kind,target,turn_id,reason='launch_failed',
            message='无法启动本地执行进程，已保留任务与已有结果；请重试。',expected_launch=nonce)
        raise
    birth=None
    try:
        birth=process_birth(process.pid)
        active=False
        with store.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            if kind=='conversation':
                row=c.execute('SELECT payload,cancel FROM conversations WHERE id=?',(target,)).fetchone()
                value=json.loads(row[0]) if row else {}
                active=bool(row and not row[1] and value.get('state')=='running' and value.get('turn_id')==turn_id
                            and value.get('execution_launch',{}).get('nonce')==nonce)
            else:
                row=c.execute('SELECT payload,cancel FROM runs WHERE id=?',(target,)).fetchone()
                value=json.loads(row[0]) if row else {}
                active=bool(row and not row[1] and value.get('state') in ('queued','running')
                            and value.get('execution_launch',{}).get('nonce')==nonce)
            if active and birth:
                c.execute('INSERT OR REPLACE INTO workers VALUES (?,?,?,?,?)',(kind,target,turn_id,process.pid,birth))
        if not active:
            # Cancellation may arrive after Popen but before registration.
            if birth: terminate_verified(process.pid,birth)
            elif process.poll() is None: process.terminate()
        elif not birth:
            raise RuntimeError('Cannot register worker identity')
        (folder/'worker.pid').write_text(str(process.pid),encoding='ascii')
    except Exception:
        try:
            store.fail_execution(kind,target,turn_id,reason='launch_failed',
                message='无法登记执行进程，已保留任务与已有结果；请重试。',expected_launch=nonce)
        finally:
            # This Popen handle belongs to the process just created here. Revoke
            # first and close it even if registration itself could not persist.
            if birth: terminate_verified(process.pid,birth)
            elif process.poll() is None: process.terminate()
        raise
    return process


def reconcile():
    """Recover confirmed dead active workers, leaving live/unknown identities alone."""
    with store.connection() as c:
        chats=[json.loads(r[0]) for r in c.execute('SELECT payload FROM conversations')]
        runs=[json.loads(r[0]) for r in c.execute("SELECT payload FROM runs WHERE state IN ('queued','running')")]
        workers={(r[0],r[1],r[2]):(r[3],r[4]) for r in c.execute('SELECT kind,target,turn_id,pid,birth FROM workers')}
    active_chats={(v['id'],v.get('turn_id')) for v in chats if v.get('state')=='running'}
    candidates=[('conversation',v,v.get('turn_id','')) for v in chats if v.get('state')=='running']
    for run in runs:
        owner=run.get('execution_owner') or {}
        if (owner.get('conversation_id'),owner.get('turn_id')) not in active_chats:
            candidates.append(('run',run,''))
    recovered=[]
    for kind,value,turn_id in candidates:
        identity=workers.get((kind,value['id'],turn_id))
        if identity:
            if process_state(*identity) not in ('exited','replaced'): continue
        else:
            launch=value.get('execution_launch',{})
            if kind=='conversation' and launch.get('turn_id')!=turn_id: launch={}
            stamp=launch.get('requested_at') or value.get('turn_contexts',{}).get(turn_id,{}).get('started_at') or value.get('created_at')
            try: age=(datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()
            except (ValueError,TypeError): continue
            if age<START_GRACE_SECONDS: continue
        if store.fail_execution(kind,value['id'],turn_id,expected_worker=identity or (),
                                expected_launch=value.get('execution_launch',{}).get('nonce')):
            recovered.append({'kind':kind,'id':value['id'],'turn_id':turn_id})
    return recovered


def stop(kind,target,turn_id=''):
    with store.connection() as c:
        row=c.execute('SELECT pid,birth FROM workers WHERE kind=? AND target=? AND turn_id=?',
                      (kind,target,turn_id)).fetchone()
    if not row: return {'status':'not_registered','retryable':False,'detail':'本轮已停止，未发现仍登记的执行进程'}
    stopped=terminate_verified(row[0],row[1])
    state='exited' if stopped else process_state(row[0],row[1])
    if stopped or state in ('exited','replaced'):
        with store.connection() as c:
            c.execute('DELETE FROM workers WHERE kind=? AND target=? AND turn_id=? AND pid=? AND birth=?',
                      (kind,target,turn_id,*row))
        return {'status':'stopped' if stopped else 'already_exited','retryable':False,'detail':'本轮执行进程已结束'}
    # Preserve the exact identity on transient termination/query failures so a
    # retry never has to guess a PID. The database write guard remains revoked.
    return {'status':'pending','retryable':True,'detail':'本轮已停止写入，后台连接尚未确认关闭，可重试结束连接'}


def stop_conversation(cid,turn_id):
    results=[stop('conversation',cid,turn_id)]
    # start_research currently executes inline. Keep separately launched owned
    # workers covered too, without touching another chat on the same run.
    with store.connection() as c:
        runs=[(rid,json.loads(payload)) for rid,payload in c.execute('SELECT id,payload FROM runs').fetchall()]
    for rid,run in runs:
        if (run.get('execution_owner')=={'conversation_id':cid,'turn_id':turn_id}
                and (run.get('state')=='cancelled' or run.get('stop_reason')=='time_limit')):
            results.append(stop('run',rid))
    result=next((r for r in results if r['retryable']),None) or next((r for r in results if r['status']=='stopped'),None) or results[0]
    return {**result,'turn_id':turn_id}
