"""Optional startup-frozen access policy and backwards-compatible entry points.

This restricts application data reads/network calls, not Python or the host OS.
"""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from . import store
from .config import ROOT, FIXTURES, VERSION

from .execution_audit import audit, clean, scope, traced

class PolicyError(ValueError):
    pass


def load_policy():
    enabled = os.environ.get('RESEARCH_COMPETITION_MODE', '').lower() in ('1','true','yes')
    if not enabled:
        return {'enabled':False,'model_origins':[],'retrieval_origins':[]}
    if not os.environ.get('RESEARCH_DATA_DIR'):
        raise PolicyError('受控运行必须通过RESEARCH_DATA_DIR指定独立工作区')
    if store.DATA.resolve()==(ROOT/'var').resolve():
        raise PolicyError('受控工作区不能使用普通模式默认var目录')
    path = Path(os.environ.get('RESEARCH_COMPETITION_POLICY', store.DATA/'competition-policy.json'))
    frozen=os.environ.get('RESEARCH_COMPETITION_POLICY_JSON')
    policy = json.loads(frozen) if frozen else json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if not isinstance(policy,dict) or set(policy)-{'enabled','model_origins','retrieval_origins'}:
        raise PolicyError('访问策略包含未知字段')
    result = {'enabled':True}
    for key in ('model_origins','retrieval_origins'):
        values = policy.get(key, [])
        if not isinstance(values,list) or len(values)>100: raise PolicyError('网络许可必须为来源列表')
        result[key] = sorted(set(origin(v) for v in values))
    return result


def origin(url):
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
        raise PolicyError('网络许可需要明确HTTP/HTTPS来源，不允许凭据或通配符')
    host=p.hostname.lower().rstrip('.')
    if '*' in host: raise PolicyError('网络许可不支持通配符')
    try: port=p.port or (443 if p.scheme=='https' else 80)
    except ValueError: raise PolicyError('网络端口无效') from None
    return f'{p.scheme}://{host}:{port}'

# Loaded once, before worker/API startup. A running task cannot rewrite its policy.
POLICY=load_policy()
if POLICY["enabled"]:
    os.environ["RESEARCH_COMPETITION_POLICY_JSON"]=json.dumps(POLICY)


def check_network(url, purpose='retrieval'):
    if purpose not in ('model','retrieval'): raise PolicyError('未知网络用途')
    target=origin(url)
    allowed=not POLICY['enabled'] or target in POLICY[purpose+'_origins']
    audit('network_access',purpose=purpose,origin=target,path=urlsplit(url).path,status='allowed' if allowed else 'blocked')
    if not allowed: raise PolicyError('受控运行禁止该'+('模型' if purpose=='model' else '资料')+'网络来源：'+target)


def check_file(path):
    resolved=Path(path).resolve()
    allowed=True
    if POLICY['enabled']:
        roots=[store.DATA.resolve(),FIXTURES.resolve()]
        # Runtime credential stores are not research materials even inside DATA.
        forbidden=resolved.name in ('connections.json','.env','competition-policy.json') or resolved.name.endswith('.sqlite3')
        allowed=not forbidden and any(resolved.is_relative_to(root) for root in roots)
    audit('file_access',path=str(resolved),status='allowed' if allowed else 'blocked')
    if not allowed: raise PolicyError('受控运行仅允许读取工作区内研究材料')
    return resolved


def read_bytes(path):
    p=check_file(path);content=p.read_bytes()
    audit('file_read',path=str(p),sha256=hashlib.sha256(content).hexdigest(),bytes=len(content))
    return content


def read_json(path):
    return json.loads(read_bytes(path).decode('utf-8-sig'))


def status():
    return {**POLICY,'data_directory':str(store.DATA.resolve()),'scope':'application','version':VERSION,'audit_enabled':True,'audit_scope':'run',
        'note':'应用层访问控制；操作系统和运行机器网络隔离需另行配置。'}


# Historical imports and routes remain usable while shared capabilities live in
# modules that are independent of whether access restrictions are enabled.
def register_material(*args, **kwargs):
    from .materials import register_material as call
    return call(*args, **kwargs)


def materials(*args, **kwargs):
    from .materials import materials as call
    return call(*args, **kwargs)


def read_material(*args, **kwargs):
    from .materials import read_material as call
    return call(*args, **kwargs)


def material_row(*args, **kwargs):
    from .materials import material_row as call
    return call(*args, **kwargs)


def preflight(*args, **kwargs):
    from .materials import preflight as call
    return call(*args, **kwargs)


def apply_materials(*args, **kwargs):
    from .materials import apply_materials as call
    return call(*args, **kwargs)


def submission_bundle(run):
    from .exporting import evidence_bundle
    return evidence_bundle(run, store.get_document, store.DATA / 'runs' / run['id'], legacy=True)
