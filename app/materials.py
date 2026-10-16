"""Structured research materials shared by normal and controlled workspaces.

Access and audit still go through the startup-frozen application policy. Storage
and row eligibility are independent of whether network restrictions are enabled.
"""
import csv
import hashlib
import io
import json
import re
import uuid
from pathlib import Path
from . import competition, store


def register_material(content,filename,ticker,year,announced_date=None):
    from datetime import date
    if len(content)>10*1024*1024: raise ValueError('结构化材料不得超过10MB')
    if not re.fullmatch(r'\d{6}\.(SZ|SH|BJ)',ticker): raise ValueError('证券代码无效')
    if not 2000<=year<=2100: raise ValueError('材料年度无效')
    if announced_date:
        try: announced_date=date.fromisoformat(str(announced_date)).isoformat()
        except ValueError: raise ValueError('公告日期格式无效，请使用 YYYY-MM-DD') from None
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
    identity=uuid.uuid4().hex[:16];folder=store.DATA/'structured-materials';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(identity+suffix);path.write_bytes(content)
    item={'id':identity,'filename':Path(filename).name,'ticker':ticker,'year':year,'announced_date':announced_date,
        'path':str(path),'sha256':hashlib.sha256(content).hexdigest(),'row_count':len(rows),'created_at':store.now()}
    (folder/(identity+'.meta.json')).write_text(json.dumps(item,ensure_ascii=False),encoding='utf-8')
    competition.audit('material_import',material=item)
    return {k:v for k,v in item.items() if k!='path'}


def _material_folders():
    # New imports use the shared library. Existing workspaces remain readable.
    return (store.DATA/'structured-materials', store.DATA/'competition-materials')


def materials():
    items={}
    for folder in _material_folders():
        for path in sorted(folder.glob('*.meta.json')):
            item=competition.read_json(path)
            items.setdefault(item['id'], {k:v for k,v in item.items() if k!='path'})
    return sorted(items.values(),key=lambda item:(item.get('created_at',''),item['id']))


def read_material(identity):
    if not isinstance(identity,str) or not re.fullmatch(r'[a-f0-9]{16}',identity):
        raise ValueError('材料ID无效')
    meta_path=next((folder/(identity+'.meta.json') for folder in _material_folders()
                    if (folder/(identity+'.meta.json')).is_file()),None)
    if meta_path is None: raise ValueError('材料不存在，请重新导入')
    meta=competition.read_json(meta_path)
    content=competition.read_bytes(meta['path'])
    if hashlib.sha256(content).hexdigest()!=meta['sha256']: raise ValueError('材料哈希已变化，需重新导入')
    text=content.decode('utf-8-sig')
    rows=json.loads(text) if Path(meta['path']).suffix.lower()=='.json' else list(csv.DictReader(io.StringIO(text)))
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
    data_source=request.get('data_source','default')
    if data_source=='default': data_source=connections.load().get('data_source','tushare')
    requires_materials=competition.POLICY['enabled'] or data_source=='documents'
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
                raw=competition.read_bytes(doc['path'])
                if doc.get('sha256') and hashlib.sha256(raw).hexdigest()!=doc['sha256']: raise ValueError('PDF已变化')
                item['status']='accepted' if day else 'partial'
                item['reason']='已接入PDF，可按页查阅' if day else '公告日期未知，仅作待核查材料'
                if not day: warnings.append(item['reason'])
        except (ValueError,KeyError,OSError): item['reason']='PDF不可读或内容已变化';errors.append(item['reason'])
        selected.append(item)
    if not selected and requires_materials: errors.append('至少选择一份PDF或结构化材料')
    elif selected and not any(m['status'] in ('accepted','partial') for m in selected): errors.append('没有可用于本次研究的材料')
    model={'requested':bool(request.get('use_model')),'configured':False,'allowed':False,'name':'','reason':'未启用模型，仅核对材料','connection_verified':False}
    if model['requested']:
        try:
            profile=connections.resolve_model(request.get('model_profile'))
            model['name']=profile.get('name') or profile.get('model','')
            model['configured']=bool(profile.get('model') and (profile.get('key') or profile.get('provider')=='ollama'))
            model['allowed']=not competition.POLICY['enabled'] or competition.origin(profile['base']) in competition.POLICY['model_origins']
            model['reason']='已配置且许可；未发起连通性测试' if model['configured'] and model['allowed'] else '请配置可用模型并许可其接口来源，或取消使用模型'
        except (ValueError,KeyError,OSError): model['reason']='模型配置不可用，请检查连接设置'
        if not model['configured'] or not model['allowed']: errors.append(model['reason'])
    return {'ready':not errors,'errors':list(dict.fromkeys(errors)),'warnings':list(dict.fromkeys(warnings)),
        'materials':selected,'model':model,'network':{'controlled':competition.POLICY['enabled'],'model_allowed':model['allowed'],
        'retrieval_allowed':not competition.POLICY['enabled'] or bool(competition.POLICY['retrieval_origins'])},
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
