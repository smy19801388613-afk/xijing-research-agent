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
from . import store
from .config import ROOT


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
    folder=store.DATA/('conversations' if kind=='conversation' else 'runs')/target
    folder.mkdir(parents=True,exist_ok=True)
    module='app.agent_worker' if kind=='conversation' else 'app.worker'
    args=[sys.executable,'-u','-m',module,target]
    if kind=='conversation': args.append(turn_id)
    # Explicit workspace routing also makes isolated integration tests safe.
    env=os.environ.copy();env['RESEARCH_DATA_DIR']=str(store.DATA.resolve())
    with (folder/'worker.log').open('ab') as log:
        process=subprocess.Popen(args,cwd=ROOT,stdout=log,stderr=log,env=env,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    birth=process_birth(process.pid)
    active=False
    with store.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if kind=='conversation':
            row=c.execute('SELECT payload,cancel FROM conversations WHERE id=?',(target,)).fetchone()
            value=json.loads(row[0]) if row else {}
            active=bool(row and not row[1] and value.get('state')=='running' and value.get('turn_id')==turn_id)
        else:
            row=c.execute('SELECT state,cancel FROM runs WHERE id=?',(target,)).fetchone()
            active=bool(row and not row[1] and row[0] in ('queued','running'))
        if active and birth:
            c.execute('INSERT OR REPLACE INTO workers VALUES (?,?,?,?,?)',(kind,target,turn_id,process.pid,birth))
    if not active:
        # Cancellation may arrive after Popen but before registration.
        terminate_verified(process.pid,birth)
    (folder/'worker.pid').write_text(str(process.pid),encoding='ascii')
    return process


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
        if run.get('execution_owner')=={'conversation_id':cid,'turn_id':turn_id} and run.get('state')=='cancelled':
            results.append(stop('run',rid))
    result=next((r for r in results if r['retryable']),None) or next((r for r in results if r['status']=='stopped'),None) or results[0]
    return {**result,'turn_id':turn_id}
