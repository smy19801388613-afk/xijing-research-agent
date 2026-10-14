"""Startup-frozen application policy and credential-free competition audit.

This restricts application data reads/network calls, not Python or the host OS.
"""
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import uuid
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit
from . import store
from .config import ROOT, FIXTURES, VERSION

_context = ContextVar('competition_context', default={})
_secret = re.compile(r'key|token|secret|authorization|password', re.I)

class PolicyError(ValueError):
    pass


def load_policy():
    enabled = os.environ.get('RESEARCH_COMPETITION_MODE', '').lower() in ('1','true','yes')
    if not enabled:
        return {'enabled':False,'model_origins':[],'retrieval_origins':[]}
    if not os.environ.get('RESEARCH_DATA_DIR'):
        raise PolicyError('比赛模式必须通过RESEARCH_DATA_DIR指定独立工作区')
    if store.DATA.resolve()==(ROOT/'var').resolve():
        raise PolicyError('比赛工作区不能使用普通模式默认var目录')
    path = Path(os.environ.get('RESEARCH_COMPETITION_POLICY', store.DATA/'competition-policy.json'))
    frozen=os.environ.get('RESEARCH_COMPETITION_POLICY_JSON')
    policy = json.loads(frozen) if frozen else json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if not isinstance(policy,dict) or set(policy)-{'enabled','model_origins','retrieval_origins'}:
        raise PolicyError('比赛策略包含未知字段')
    result = {'enabled':True}
    for key in ('model_origins','retrieval_origins'):
        values = policy.get(key, [])
        if not isinstance(values,list) or len(values)>100: raise PolicyError('比赛网络许可必须为来源列表')
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


def clean(value):
    if isinstance(value,dict):
        from .providers import _USAGE_COUNTERS, _USAGE_DETAILS
        def token_metric(key,item):
            if key in _USAGE_COUNTERS | {'max_tokens','max_completion_tokens','requested_output_tokens','generation_tokens'}:
                return isinstance(item,int) and not isinstance(item,bool) and item>=0
            return key in _USAGE_DETAILS and isinstance(item,dict)
        return {str(k):('[REDACTED]' if _secret.search(str(k)) and not token_metric(str(k),v) else clean(v)) for k,v in value.items()}
    if isinstance(value,list): return [clean(v) for v in value]
    if isinstance(value,Path): return str(value)
    if isinstance(value,str):
        value=re.sub(r'(?i)(bearer\s+)[\w.-]+',r'\1[REDACTED]',value)
        value=re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[REDACTED]',value)
        return value
    return value


def audit(kind, **fields):
    if not POLICY['enabled']: return
    store.DATA.mkdir(parents=True,exist_ok=True)
    value=clean({'time':store.now(),'kind':kind,**_context.get(),**fields})
    with sqlite3.connect(store.DATA/'competition-audit.sqlite3',timeout=15) as c:
        c.execute('CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)')
        c.execute('INSERT INTO audit(payload) VALUES (?)',(json.dumps(value,ensure_ascii=False,default=str),))


@contextmanager
def scope(**fields):
    token=_context.set({**_context.get(),**fields})
    try: yield
    finally: _context.reset(token)


def traced(kind, identity=None):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            fields={identity:str(args[0])} if identity and args else {}
            started=time.monotonic()
            with scope(**fields):
                audit(kind+'_start',arguments=kwargs)
                try:
                    result=fn(*args,**kwargs)
                    audit(kind+'_result',elapsed_ms=int((time.monotonic()-started)*1000),result=result)
                    return result
                except Exception as e:
                    audit(kind+'_error',error_type=type(e).__name__,message=str(e)[:1000])
                    raise
        return wrapped
    return decorate


def check_network(url, purpose='retrieval'):
    if not POLICY['enabled']: return
    if purpose not in ('model','retrieval'): raise PolicyError('未知网络用途')
    target=origin(url)
    allowed=target in POLICY[purpose+'_origins']
    audit('network_access',purpose=purpose,origin=target,path=urlsplit(url).path,status='allowed' if allowed else 'blocked')
    if not allowed: raise PolicyError('比赛模式禁止该'+('模型' if purpose=='model' else '资料')+'网络来源：'+target)


def check_file(path):
    resolved=Path(path).resolve()
    if POLICY['enabled']:
        roots=[store.DATA.resolve(),FIXTURES.resolve()]
        # Runtime credential stores are not research materials even inside DATA.
        forbidden=resolved.name in ('connections.json','.env','competition-policy.json') or resolved.name.endswith('.sqlite3')
        allowed=not forbidden and any(resolved.is_relative_to(root) for root in roots)
        audit('file_access',path=str(resolved),status='allowed' if allowed else 'blocked')
        if not allowed: raise PolicyError('比赛模式仅允许读取工作区内研究材料')
    return resolved


def read_bytes(path):
    p=check_file(path);content=p.read_bytes()
    audit('file_read',path=str(p),sha256=hashlib.sha256(content).hexdigest(),bytes=len(content))
    return content


def read_json(path):
    return json.loads(read_bytes(path).decode('utf-8-sig'))


def status():
    return {**POLICY,'data_directory':str(store.DATA.resolve()),'scope':'application','version':VERSION,
        'note':'应用层访问控制；操作系统和比赛机器网络隔离需另行配置。'}


def register_material(content,filename,ticker,year,announced_date=None):
    if not POLICY['enabled']: raise PolicyError('请启动比赛模式后导入结构化材料')
    if len(content)>10*1024*1024: raise ValueError('结构化材料不得超过10MB')
    if not re.fullmatch(r'\d{6}\.(SZ|SH|BJ)',ticker): raise ValueError('证券代码无效')
    if not 2000<=year<=2100: raise ValueError('材料年度无效')
    suffix=Path(filename).suffix.lower()
    try:
        text=content.decode('utf-8-sig')
        if suffix=='.json':
            rows=json.loads(text)
            if isinstance(rows,dict): rows=rows.get('rows')
        elif suffix=='.csv': rows=list(csv.DictReader(io.StringIO(text)))
        else: raise ValueError('结构化材料支持JSON或CSV；PDF请用资料上传')
        if not isinstance(rows,list) or not 1<=len(rows)<=10000 or any(not isinstance(r,dict) for r in rows):
            raise ValueError('材料需为非空rows对象列表，最多10000行')
    except (UnicodeError,json.JSONDecodeError,csv.Error): raise ValueError('材料必须为有效UTF-8 JSON/CSV') from None
    identity=uuid.uuid4().hex[:16];folder=store.DATA/'competition-materials';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(identity+suffix);path.write_bytes(content)
    item={'id':identity,'filename':Path(filename).name,'ticker':ticker,'year':year,'announced_date':announced_date,
        'path':str(path),'sha256':hashlib.sha256(content).hexdigest(),'row_count':len(rows),'created_at':store.now()}
    (folder/(identity+'.meta.json')).write_text(json.dumps(item,ensure_ascii=False),encoding='utf-8')
    audit('material_import',material=item)
    return {k:v for k,v in item.items() if k!='path'}


def materials():
    folder=store.DATA/'competition-materials'
    return [{k:v for k,v in read_json(p).items() if k!='path'} for p in sorted(folder.glob('*.meta.json'))]


def read_material(identity):
    if not re.fullmatch(r'[a-f0-9]{16}',identity): raise ValueError('材料ID无效')
    meta=read_json(store.DATA/'competition-materials'/(identity+'.meta.json'))
    content=read_bytes(meta['path'])
    if hashlib.sha256(content).hexdigest()!=meta['sha256']: raise ValueError('材料哈希已变化，需重新导入')
    text=content.decode('utf-8-sig')
    rows=json.loads(text) if Path(meta['path']).suffix=='.json' else list(csv.DictReader(io.StringIO(text)))
    if isinstance(rows,dict): rows=rows['rows']
    return meta,rows


def material_row(row, year, as_of, meta):
    """One deterministic eligibility rule for preflight and execution."""
    from datetime import date
    from decimal import Decimal
    from .finance import METRICS, number
    if row.get('metric') not in METRICS:
        return None, '不属于本次支持的核心财务指标', False
    try: row_year=int(row.get('year',0))
    except (ValueError,TypeError): return None, '年度无效', False
    if row_year not in (year,year-1): return None, '不在本次研究的两个年度内', False
    announced=row.get('announced_date') or meta.get('announced_date')
    if not announced: return None, '缺少公告日期，不能证明截止日内已知', False
    try: announced=date.fromisoformat(str(announced)).isoformat()
    except ValueError: return None, '公告日期格式无效', False
    if announced>as_of: return None, '公告日期晚于研究截止日', False
    value=number(row.get('value'));unit=row.get('unit')
    if value is None or not value.is_finite() or unit not in ('元','千元','万元') or row.get('currency')!='CNY' or row.get('scope')!='consolidated' or row.get('period_type')!='annual':
        return None, '缺少有效数值、单位、币种或年度合并口径', True
    return {'year':row_year,'announced_date':announced,'value':str(value*Decimal({'元':1,'千元':1000,'万元':10000}[unit]))}, '', False


def preflight(request):
    """Inspect selected material and configuration without any network request."""
    from datetime import date
    from . import connections
    errors=[];warnings=[];selected=[]
    if not POLICY['enabled']: errors.append('请先启动独立比赛工作区')
    for identity in request.get('material_ids',[]):
        item={'id':identity,'kind':'structured','filename':identity,'status':'excluded','accepted_rows':0,'excluded_rows':0,'issues':[]}
        try:
            meta,rows=read_material(identity);item['filename']=meta['filename']
            if meta['ticker']!=request['ticker'] or meta['year']!=request['year']:
                item['reason']='材料公司或年度与当前任务不匹配';errors.append(item['filename']+'：'+item['reason'])
            else:
                for n,row in enumerate(rows,1):
                    usable,reason,fatal=material_row(row,request['year'],request['as_of'],meta)
                    if usable: item['accepted_rows']+=1
                    else:
                        item['excluded_rows']+=1
                        if len(item['issues'])<30: item['issues'].append({'row':n,'reason':reason})
                        if fatal: errors.append(f"{meta['filename']} 第{n}行：{reason}")
                item['status']='partial' if item['accepted_rows'] and item['excluded_rows'] else 'accepted' if item['accepted_rows'] else 'excluded'
                item['reason']=f"采用 {item['accepted_rows']} 行，排除 {item['excluded_rows']} 行"
                if item['excluded_rows']: warnings.append(item['filename']+'：'+item['reason'])
        except (ValueError,KeyError,OSError):
            item['reason']='材料不可读或内容已变化，请重新导入';errors.append(item['filename']+'：'+item['reason'])
        selected.append(item)
    if request.get('document_id'):
        identity=request['document_id'];item={'id':identity,'kind':'pdf','filename':identity,'status':'excluded'}
        try:
            doc=store.get_document(identity);item['filename']=doc['title'];day=doc.get('announced_date')
            if doc['ticker']!=request['ticker'] or doc['year']!=request['year']:
                item['reason']='PDF公司或年度与当前任务不匹配';errors.append(item['reason'])
            elif day and date.fromisoformat(day).isoformat()>request['as_of']:
                item['reason']='PDF公告日期晚于研究截止日';errors.append(item['reason'])
            else:
                raw=read_bytes(doc['path'])
                if doc.get('sha256') and hashlib.sha256(raw).hexdigest()!=doc['sha256']: raise ValueError('PDF已变化')
                item['status']='accepted' if day else 'partial'
                item['reason']='已接入PDF，可按页查阅' if day else '公告日期未知，仅作待核查材料'
                if not day: warnings.append(item['reason'])
        except (ValueError,KeyError,OSError): item['reason']='PDF不可读或内容已变化';errors.append(item['reason'])
        selected.append(item)
    if not selected: errors.append('至少选择一份PDF或结构化材料')
    elif not any(m['status'] in ('accepted','partial') for m in selected): errors.append('没有可用于本次研究的材料')
    model={'requested':bool(request.get('use_model')),'configured':False,'allowed':False,'name':'','reason':'未启用模型，仅核对材料','connection_verified':False}
    if model['requested']:
        try:
            profile=connections.resolve_model(request.get('model_profile'))
            model['name']=profile.get('name') or profile.get('model','')
            model['configured']=bool(profile.get('model') and (profile.get('key') or profile.get('provider')=='ollama'))
            model['allowed']=not POLICY['enabled'] or origin(profile['base']) in POLICY['model_origins']
            model['reason']='已配置且许可；未发起连通性测试' if model['configured'] and model['allowed'] else '请配置可用模型并许可其接口来源，或取消使用模型'
        except (ValueError,KeyError,OSError): model['reason']='模型配置不可用，请检查连接设置'
        if not model['configured'] or not model['allowed']: errors.append(model['reason'])
    return {'ready':not errors,'errors':list(dict.fromkeys(errors)),'warnings':list(dict.fromkeys(warnings)),
        'materials':selected,'model':model,'network':{'controlled':POLICY['enabled'],'model_allowed':model['allowed'],
        'retrieval_allowed':not POLICY['enabled'] or bool(POLICY['retrieval_origins'])},
        'as_of':request['as_of'],'ticker':request['ticker'],'year':request['year']}


def apply_materials(run):
    from .finance import METRICS, number
    for identity in run['request'].get('material_ids',[]):
        meta,rows=read_material(identity)
        if meta['ticker']!=run['request']['ticker'] or meta['year']!=run['request']['year']:
            raise ValueError('结构化材料公司或年度不匹配')
        for n,row in enumerate(rows,1):
            metric=row.get('metric')
            if metric not in METRICS: continue
            prepared,reason,fatal=material_row(row,run['request']['year'],run['request']['as_of'],meta)
            if fatal: raise ValueError(f'材料第{n}行{reason}')
            if not prepared:
                run['gaps'].append({'code':f'material-date-{identity}-{n}','message':f'{meta["filename"]} 第{n}行未采用：{reason}'});continue
            year=prepared['year'];announced=prepared['announced_date'];value=number(prepared['value'])
            eid=f'material-{identity}-row-{n}';fid=f'{metric}-{year}'
            run['evidence'].append({'id':eid,'kind':'material','title':meta['filename'],'material_id':identity,'sha256':meta['sha256'],
                'announced_date':announced,'retrieved_at':meta['created_at'],'locator':f'结构化材料第{n}行','quote':json.dumps(row,ensure_ascii=False)})
            old=next((f for f in run['facts'] if f['id']==fid),None)
            if old:
                old['evidence_ids'].append(eid)
                # Imported tables are not independent verification of a PDF/API.
                if number(old['value'])!=value: old['status']='conflict'
            else:
                run['facts'].append({'id':fid,'metric':metric,'label':METRICS[metric]['label'],'year':year,'value':str(value),
                    'original_value':str(row['value']),'original_unit':row['unit'],'currency':'CNY','scope':'consolidated','period_type':'annual',
                    'announced_date':announced,'evidence_ids':[eid],'status':'document_only','missing_reason':None})


def submission_bundle(run):
    import zipfile
    import subprocess
    import importlib.metadata
    from .exporting import evidence_bundle
    audit('submission_export',run_id=run['id'])
    base=evidence_bundle(run,store.get_document,store.DATA/'runs'/run['id'])
    items={}
    with zipfile.ZipFile(io.BytesIO(base)) as z:
        items={name:z.read(name) for name in z.namelist()}
    for identity in run['request'].get('material_ids',[]):
        meta,_=read_material(identity);items['materials/'+identity+Path(meta['path']).suffix]=read_bytes(meta['path'])
        items['materials/'+identity+'.meta.json']=json.dumps({k:v for k,v in meta.items() if k!='path'},ensure_ascii=False,indent=2).encode()
    items['competition/material-selection.json']=json.dumps(run.get('material_preflight') or {},ensure_ascii=False,indent=2).encode()
    if POLICY['enabled'] and (store.DATA/'competition-audit.sqlite3').exists():
        with sqlite3.connect(store.DATA/'competition-audit.sqlite3') as c:
            # Isolated workspace audit includes imports/network work for this workspace.
            events=[json.loads(r[0]) for r in c.execute('SELECT payload FROM audit ORDER BY seq')]
        items['competition/audit.json']=json.dumps(events,ensure_ascii=False,indent=2).encode()
    try: commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    except (OSError,subprocess.CalledProcessError): commit='unknown'
    versions={}
    for name in ('fastapi','httpx','pydantic','pdfplumber','pypdfium2'):
        try: versions[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: versions[name]='missing'
    items['competition/environment.json']=json.dumps({'app_version':VERSION,'commit':commit,'dependencies':versions,'policy':POLICY,
        'reproduction_modes':['deterministic_recompute','recorded_model_response','live_model_rerun'],
        'limitations':['模型重跑不保证逐字一致','审计为工作区范围，不包含连接密钥文件','应用层限制不是操作系统沙箱']},ensure_ascii=False,indent=2).encode()
    for relative in ('requirements-lock.txt','web/package-lock.json','app/agent.py','app/deep_research.py'):
        items['competition/reproducibility/'+relative]=(ROOT/relative).read_bytes()
    # Existing manifest is replaced because it must cover newly added files too.
    original_manifest=json.loads(items.pop('manifest.json',b'{}'))
    items['manifest.json']=json.dumps({**original_manifest,'files':[{'path':p,'sha256':hashlib.sha256(v).hexdigest(),'bytes':len(v)} for p,v in sorted(items.items())]},indent=2).encode()
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        for p,v in items.items(): z.writestr(p,v)
    return output.getvalue()
