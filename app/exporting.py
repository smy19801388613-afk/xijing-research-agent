from . import competition
import json
import re
from copy import deepcopy
from html import escape
from decimal import Decimal,InvalidOperation
from .finance import METRICS,number
from .fact_state import verification, enrich
from .report_styles import REPORT_CSS
from .review_state import issue_text

DOCUMENT_LINK = re.compile(r'(?:https?://(?:127\.0\.0\.1|localhost)(?::\d+)?)?/api/documents/([\w-]+)/file')

def rewrite_document_links(value, document_paths):
    """Rewrite a delivery copy only; stored research and the audit JSON stay intact."""
    if isinstance(value, dict): return {k:rewrite_document_links(v,document_paths) for k,v in value.items()}
    if isinstance(value, list): return [rewrite_document_links(v,document_paths) for v in value]
    if isinstance(value, str):
        value=DOCUMENT_LINK.sub(lambda m:document_paths.get(m[1],m[0]),value)
        for url,path in document_paths.items():
            if url.startswith(('http://','https://')):
                value=re.sub(re.escape(url)+r'(?=[#\s)\"\']|$)',lambda _:path,value)
        return value
    return value

def calculation_change(calc):
    if calc and number(calc.get('value')) is not None: return f"{number(calc['value']):+.2f}%"
    if calc and number(calc.get('change')) is not None: return f"金额变化 {number(calc['change'])/Decimal(100000000):+.2f} 亿元"
    return '不适用/缺失'


def financial_amount(fact):
    value = number(fact.get('value')) if fact else None
    return f'{value/Decimal(100000000):,.2f}' if value is not None else '缺失'


def fact_source_label(fact, run):
    if not fact: return '缺失'
    return (fact.get('verification') or verification(fact, run.get('evidence', []), run['request'].get('as_of')))['label']


def basis_messages(run):
    from .financial_basis import conflicts
    return list(dict.fromkeys(i['message'] for i in [*run.get('financial_basis_issues', []), *conflicts(run)] if i.get('message')))


def source_description(source):
    state=source.get('read_status') or source.get('extraction') or source.get('status') or source.get('kind')
    return {'pdf_text':'PDF原文','public_pdf_text':'公开PDF原文','body_read':'已读网页正文',
        'article_text':'网页正文','original_record':'接口原始记录','search_snippet':'搜索摘要',
        'api':'接口记录','pdf':'PDF文档','document':'文档原文','windows_ocr':'OCR待复核',
        'ocr':'OCR待复核'}.get(state,'来源材料')

def current_valuations(run):
    latest={}
    for item in run.get('valuation_history',[]): latest[item.get('method',item.get('id'))]=item
    return list(latest.values())

def valuation_summary(value):
    from .valuation import CATALOG
    method=next((m for m in CATALOG if m['id']==value.get('method')),{})
    fields={f['key']:f for f in method.get('fields',[])}
    assumptions='；'.join(f"{fields.get(key,{}).get('label',key)} {v}{fields.get(key,{}).get('unit','')}" for key,v in value.get('assumptions',{}).items() if v is not None) or '未记录'
    output=value.get('output',{})
    cap=number(output.get('equity_value_100m'))
    result=f'股权价值 {cap:,.2f} 亿元' if cap is not None else '详见完整审计记录'
    return f"{value.get('method_name',value.get('method','估值'))}（最新保存的探索情景）：{assumptions}。{result}。"

def legacy_scenario_summary(value):
    return valuation_summary({'method':'pe','method_name':'PE 情景','assumptions':{
        ('multiple' if key=='pe' else key):v for key,v in value.get('inputs',{}).items()},'output':value.get('output',{})})

def delivery_note(run, audit, packaged):
    scope='完整审计底稿，包含全部历史版本及原始记录。' if audit else '当前报告；完整历史版本、原始计算和运行记录保留在完整审计导出及证据包中。'
    links='已打包的 PDF 使用相对路径；外部链接需联网，详见 manifest.json。' if packaged else '本地 PDF 链接需在运行析鲸AI服务的本机打开；转交他人请使用完整证据包。'
    from .questions import report_sync
    sync=report_sync(run)
    status=(' '+sync['message']+' 问题清单展示当前研究记录，报告正文保留原版本。') if sync['needs_update'] else ''
    return f"报告第 {run.get('report_revision',1)} 版。{scope}{links}{status}"

def valuation_state_message(run):
    from .report_state import report_valuation_status
    return report_valuation_status(run).get('message','')

def evidence_bundle(run, get_document, snapshot_dir, legacy=False):
    from . import execution_audit
    with execution_audit.scope(run_id=run['id']):
        execution_audit.audit('evidence_export', report_revision=run.get('report_revision', 1))
        return _evidence_bundle(run, get_document, snapshot_dir, legacy)


def _evidence_bundle(run, get_document, snapshot_dir, legacy=False):
    """Portable report plus complete audit data. Never fetch external resources."""
    import hashlib
    import io
    import zipfile
    from pathlib import Path
    from urllib.parse import urlsplit
    urls=set()
    def collect(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in ('url','source_url') and isinstance(item,str) and item: urls.add(item)
                collect(item)
        elif isinstance(value,list):
            for item in value: collect(item)
    collect(run)
    ids=set(run.get('document_ids',[]))
    if run.get('document_id'): ids.add(run['document_id'])
    ids.update(match[1] for url in urls for match in DOCUMENT_LINK.finditer(url))
    ids.update(d['id'] for d in run.get('attached_documents',[]) if d.get('id'))
    paths={}; files={}; manifest_docs=[]; unavailable=[]
    for doc_id in sorted(ids):
        if not re.fullmatch(r'[\w-]+',doc_id): continue
        try:
            doc=get_document(doc_id)
            content=competition.read_bytes(doc['path'])
        except (KeyError,OSError):
            unavailable.append(doc_id);continue
        path='documents/'+doc_id+'.pdf';paths[doc_id]=path;files[path]=content
        original=doc.get('source_url')
        if original:
            urls.add(original);paths[original.split('#')[0]]=path
        manifest_docs.append({'id':doc_id,'title':doc.get('title'),'path':path,'sha256':hashlib.sha256(content).hexdigest(),
            'original_url':original,'access':'packaged','requires_network':False})
    files['report.md']=markdown(run,document_paths=paths).encode('utf-8')
    files['report.html']=printable(run,document_paths=paths).encode('utf-8')
    files['audit.md']=markdown(run,audit=True,document_paths=paths).encode('utf-8')
    files['audit.html']=printable(run,audit=True,document_paths=paths).encode('utf-8')
    files['run.json']=json.dumps(run,ensure_ascii=False,indent=2).encode('utf-8')
    recommendation=(run.get('memo') or {}).get('investment_recommendation')
    if isinstance(recommendation,dict):
        files['analysis/investment-recommendation.json']=json.dumps(recommendation,ensure_ascii=False,indent=2).encode('utf-8')
    for field in ('market_snapshot','market_snapshot_history'):
        if run.get(field): files['analysis/'+field+'.json']=json.dumps(run[field],ensure_ascii=False,indent=2).encode('utf-8')
    from .research_extensions import context as extension_context
    extension_inputs = extension_context(run)
    if extension_inputs:
        files['analysis/current-inputs.json'] = json.dumps(extension_inputs,ensure_ascii=False,indent=2).encode('utf-8')
        for field in ('financial_period_records','financial_period_deleted','valuation_sensitivity_history','comparable_analyses','tracking_observations'):
            if run.get(field): files['analysis/'+field+'.json'] = json.dumps(run[field],ensure_ascii=False,indent=2).encode('utf-8')
    for file in snapshot_dir.glob('*.json'): files['snapshots/'+file.name]=competition.read_bytes(file)
    references=[]
    for url in sorted(urls):
        target=rewrite_document_links(url,paths)
        packaged=target!=url
        local=bool(DOCUMENT_LINK.search(url))
        references.append({'original_url':url,'href':target,'access':'packaged' if packaged else 'local_service_required' if local else 'network_required' if urlsplit(url).scheme in ('http','https') else 'unresolved',
            'requires_network':not packaged and not local and urlsplit(url).scheme in ('http','https')})
    manifest={'schema_version':1,'run_id':run['id'],'report_revision':run.get('report_revision',1),
        'reading_files':['report.html','report.md'],'audit_files':['audit.html','audit.md','run.json'],
        'note':'解压后打开 report.html 阅读当前报告。已附 PDF 使用相对路径并保留页码；浏览器是否定位页码取决于 PDF 阅读器。外部链接需联网；未自动下载外链。run.json 保留原始记录与原 URL。',
        'documents':manifest_docs,'references':references,'unavailable_document_ids':unavailable,
        'files':[{'path':path,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()} for path,content in files.items()]}
    from . import execution_audit, materials, store
    from .config import ROOT, VERSION
    import importlib.metadata
    import subprocess
    for identity in run.get('request', {}).get('material_ids', []):
        meta, _ = materials.read_material(identity)
        files['materials/' + identity + Path(meta['path']).suffix] = competition.read_bytes(meta['path'])
        files['materials/' + identity + '.meta.json'] = json.dumps(
            {k: v for k, v in meta.items() if k != 'path'}, ensure_ascii=False, indent=2).encode('utf-8')
    events = execution_audit.events_for_run(run)
    files['execution/audit.json'] = json.dumps(events, ensure_ascii=False, indent=2).encode('utf-8')
    files['execution/material-selection.json'] = json.dumps(
        run.get('material_preflight') or {}, ensure_ascii=False, indent=2).encode('utf-8')
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        commit = 'unknown'
    versions = {}
    for name in ('fastapi', 'httpx', 'pydantic', 'pdfplumber', 'pypdfium2'):
        try: versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: versions[name] = 'missing'
    source_files = [*sorted((ROOT / 'app').glob('*.py')), *sorted((ROOT / 'web' / 'src').glob('*'))]
    source_hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in source_files if p.is_file()}
    environment = {'app_version': VERSION, 'commit': commit, 'source_sha256': source_hashes,
        'dependencies': versions, 'policy': competition.POLICY, 'audit_scope': 'run',
        'model_request_content': 'fingerprint_only',
        'limitations': ['仅包含当前研究关联记录，历史未记录的步骤不会补造',
                       '模型输入可能包含跨轮信息，包内仅提供输入指纹；不支持从本包完整回放模型请求',
                       '真实模型重跑不保证逐字一致；完整源码需另行提供',
                       '应用层访问限制不等于操作系统隔离']}
    files['execution/environment.json'] = json.dumps(environment, ensure_ascii=False, indent=2).encode('utf-8')
    for relative in ('requirements-lock.txt', 'web/package-lock.json'):
        files['execution/reproducibility/' + relative] = (ROOT / relative).read_bytes()
    if legacy:
        # Existing CLI consumers can keep their paths; content has the same run scope.
        for name in ('audit.json', 'material-selection.json', 'environment.json'):
            files['competition/' + name] = files['execution/' + name]
    manifest['schema_version'] = 2
    manifest['audit_files'] += ['execution/audit.json', 'execution/material-selection.json', 'execution/environment.json']
    manifest['execution_audit'] = {'scope': 'run', 'events': len(events), 'model_requests': 'fingerprint_only'}
    manifest['files'] = [{'path': path, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                         for path, content in files.items()]
    files['manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
        for path,content in files.items(): archive.writestr(path,content)
    return stream.getvalue()

def memo_references(memo,ids):
    catalog={s['id']:s for s in memo.get('sources',[])}
    return [catalog[i] for i in ids if i in catalog]


def recommendation_sources(memo):
    """An explicit frozen catalog, even empty, owns the old recommendation."""
    sources = memo['recommendation_sources'] if 'recommendation_sources' in memo else memo.get('sources', [])
    return [s for s in sources if isinstance(s, dict)] if isinstance(sources, list) else []

def export_url(url):
    # Downloaded reports must still resolve local evidence outside the app URL.
    return 'http://127.0.0.1:8920'+url if url.startswith('/api/documents/') else url

def reference_markdown(memo,ids):
    return '；'.join('['+_markdown_text(s['title'])+']('+url+')' if (url:=_recommendation_url(s.get('url'))) else _markdown_text(s['title']) for s in memo_references(memo,ids))

def reference_html(memo,ids):
    return ' · '.join('<a href="'+escape(url,quote=True)+'">'+escape(s['title'])+'</a>' if (url:=_recommendation_url(s.get('url'))) else escape(s['title']) for s in memo_references(memo,ids))

def prose_html(value):
    # Escape before the tiny Markdown presentation pass: never execute model HTML.
    return ''.join('<p>'+re.sub(r'\*\*([^*]+)\*\*',r'<strong>\1</strong>',escape(p)).replace('\n','<br>')+'</p>' for p in str(value).split('\n\n') if p.strip())


RECOMMENDATION_ACTIONS={'buy':'买入','hold':'持有','reduce':'减持','avoid':'回避','watch':'观察'}
RECOMMENDATION_CONFIDENCE={'low':'低','medium':'中','high':'高'}
RECOMMENDATION_BASES={'valuation':'估值判断','fundamental_risk':'经营或治理风险','evidence_gap':'证据缺口'}
RECOMMENDATION_FIELD_LABELS={'exit_conditions':'减仓或退出条件','buy_conditions':'建仓或加仓条件','revisit_triggers':'重新评估条件'}
RECOMMENDATION_LISTS=(('buy_conditions','建仓或加仓条件'),('exit_conditions','减仓或退出条件'),
                      ('key_risks','主要风险'),('revisit_triggers','何时调整建议'),('blocking_gaps','当前决策缺口'))


def _recommendation_text(value):
    """Translate known display tokens only; stored prose and evidence stay intact."""
    return re.sub(r'(?<![A-Za-z0-9_])(?:exit_conditions|buy_conditions|revisit_triggers)(?![A-Za-z0-9_])',
                  lambda match:RECOMMENDATION_FIELD_LABELS[match.group()],str(value))


def recommendation_label(rec):
    action=RECOMMENDATION_ACTIONS.get(rec.get('action'))
    if rec.get('status') not in ('actionable','conditional') or not action: return '暂不决策'
    if rec.get('action')=='watch': return '观察，暂不行动'
    return '条件满足后'+action if rec.get('status')=='conditional' else action


def _recommendation_url(value):
    from urllib.parse import urlsplit
    if not isinstance(value,str): return None
    value=export_url(value)
    if urlsplit(value).scheme in ('https','http') and not re.search(r'[\x00-\x20<>"\\]',value): return value
    # Evidence bundles rewrite attached PDFs into controlled relative paths.
    if re.fullmatch(r'documents/[\w-]+\.pdf(?:#page=\d+)?',value): return value
    return None


def _recommendation_references(memo,rec):
    sources={s.get('id'):s for s in recommendation_sources(memo)}
    evidence=[sources.get(key,{'id':key,'title':str(key)+' · 此版本未附来源'}) for key in rec.get('evidence_ids',[])]
    scenarios={v.get('id'):v for v in (memo.get('valuation_context') or {}).get('scenarios',[]) if isinstance(v,dict)}
    scenarios.update({v.get('scenario_id'):v for v in rec.get('valuation_basis',[]) if isinstance(v,dict)})
    valuations=[scenarios.get(key,{'scenario_id':key,'method_name':str(key)+' · 此版本未附情景'}) for key in rec.get('valuation_scenario_ids',[])]
    return evidence,valuations


def _recommendation_assumptions(item):
    from .valuation import CATALOG
    method=next((m for m in CATALOG if m['id']==item.get('method')),{})
    labels={f['key']:f for f in method.get('fields',[])}
    return '；'.join(labels.get(k,{}).get('label',str(k))+'：'+str(v)+labels.get(k,{}).get('unit','') for k,v in item.get('assumptions',{}).items())


def _finite_decimal(value):
    try:
        if isinstance(value,bool): return None
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation,TypeError,ValueError): return None


def _recommendation_quote(rec,memo):
    quote=rec.get('market_snapshot') or {}
    if not isinstance(quote,dict): return None
    price=_finite_decimal(quote.get('price'))
    has_source=quote.get('source_id') and any(s.get('id')==quote['source_id'] for s in recommendation_sources(memo))
    if 'recommendation_sources' not in memo:
        has_source = has_source or (quote.get('source_id') and quote.get('text') and _recommendation_url(quote.get('source_url')))
    return quote if quote.get('status')=='available' and price is not None and price>0 and quote.get('trade_date') and has_source else None


def _recommendation_comparisons(rec,quote):
    if not quote or quote.get('decision_eligible') is not True: return []
    scenarios={v.get('scenario_id'):v for v in rec.get('valuation_basis',[]) if isinstance(v,dict)}
    result=[]
    for item in rec.get('price_comparisons',[]):
        scenario=scenarios.get(item.get('scenario_id')); estimate=_finite_decimal(item.get('scenario_price'))
        if (item.get('scenario_id') in rec.get('valuation_scenario_ids',[]) and scenario and estimate is not None and estimate>0
            and estimate==_finite_decimal((scenario.get('output') or {}).get('price'))
            and _finite_decimal(item.get('current_price'))==_finite_decimal(quote.get('price'))
            and item.get('currency')==quote.get('currency') and item.get('trade_date')==quote.get('trade_date')
            and item.get('assumption_status')=='user_scenario' and _finite_decimal(item.get('upside_pct')) is not None): result.append(item)
    return result


def recommendation_freshness(run):
    """Use the same source, valuation and cutoff dependency state as the app."""
    from .questions import report_sync
    state=report_sync(run)['recommendation_freshness']
    if not state['needs_update']: return ''
    return '本建议依据需要重新核验。'+'；'.join(state['reasons'])+'。局部文字修改不会更新这些依据。'


def _markdown_text(value):
    """Treat recommendation fields as text, never active Markdown or HTML."""
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])',r'\\\1',escape(str(value),quote=False)).replace('\r','').replace('\n',' ')


def recommendation_markdown(memo,freshness=''):
    rec=memo.get('investment_recommendation')
    if not isinstance(rec,dict): return '\n## 投资建议\n\n此版本尚未形成明确建议。原研究立场保留。'
    t=_markdown_text
    lines=['\n## 投资建议：'+recommendation_label(rec),t(_recommendation_text(rec.get('summary',''))),
           '判断依据：'+RECOMMENDATION_BASES.get(rec.get('basis'),'待明确'),
           '期限：'+t(_recommendation_text(rec.get('horizon') or '待明确'))+'；置信度：'+RECOMMENDATION_CONFIDENCE.get(rec.get('confidence'),'待评估')
           +('；截至：'+t(rec['as_of']) if rec.get('as_of') else ''),
           '**未持仓：** '+t(_recommendation_text(rec.get('new_position') or '行动待明确')),
           '**已有持仓：** '+t(_recommendation_text(rec.get('existing_position') or '行动待明确'))]
    if freshness: lines.append(t(freshness))
    quote=_recommendation_quote(rec,memo)
    if quote:
        text=('参考收盘价（适用性待核实）：' if quote.get('decision_eligible') is False else '参考收盘价：')+t(quote['price'])+' '+t(quote.get('currency',''))+'；证券：'+t(quote.get('ticker','未记录'))+'；交易日：'+t(quote['trade_date'])
        url=_recommendation_url(quote.get('source_url'))
        if url: text+='；[行情来源](<'+url+'>)'
        lines.append(text)
        if quote.get('decision_eligible') is False and quote.get('decision_gaps'):
            lines+=['行情适用限制：'+t('；'.join(quote['decision_gaps']))]
        for item in _recommendation_comparisons(rec,quote):
            lines.append('估值情景：'+t(item.get('scenario_price',''))+' '+t(item.get('currency',''))
                         +'；相对所列参考价的假设空间：'+t(item.get('upside_pct',''))+'%；来自已保存的用户假设情景，不代表收益预测。')
    for field,label in RECOMMENDATION_LISTS:
        if rec.get(field): lines+=['### '+label]+['- '+t(_recommendation_text(item)) for item in rec[field]]
    evidence,valuations=_recommendation_references(memo,rec)
    if evidence:
        lines+=['### 建议依据']
        for source in evidence:
            url=_recommendation_url(source.get('url')); title=t(source.get('title') or source.get('id','来源'))
            lines+=['- '+('['+title+'](<'+url+'>)' if url else title)]
    if valuations:
        lines+=['### 本版本引用的估值情景']
        for item in valuations:
            lines.append('- '+t(item.get('method_name') or item.get('method') or item.get('scenario_id','估值')))
            if item.get('assumptions'): lines.append('用户假设：'+t(_recommendation_assumptions(item)))
            output=item.get('output') or {}
            if output.get('price') is not None: lines.append('情景每股价值：'+t(output['price']))
            if output.get('equity_value_100m') is not None: lines.append('情景股权价值：'+t(output['equity_value_100m'])+' 亿元')
    return '\n\n'.join(lines)


def reading_number_html(value):
    """Round only the reading surface; the original Decimal remains inspectable."""
    n = _finite_decimal(value)
    if n is None: return escape(str(value))
    return '<span title="原始值：'+escape(str(value),quote=True)+'">'+f'{n:,.2f}'+'</span>'


def recommendation_html(memo,freshness=''):
    rec=memo.get('investment_recommendation')
    if not isinstance(rec,dict): return '<section class="recommendation"><h2>投资建议</h2><p>此版本尚未形成明确建议。原研究立场保留。</p></section>'
    def t(value): return escape(str(value),quote=True)
    parts=['<section class="recommendation"><h2>投资建议：'+recommendation_label(rec)+'</h2>',
           '<p>判断依据：'+RECOMMENDATION_BASES.get(rec.get('basis'),'待明确')+'</p>',
           '<p>期限：'+t(_recommendation_text(rec.get('horizon') or '待明确'))+' · 置信度：'+RECOMMENDATION_CONFIDENCE.get(rec.get('confidence'),'待评估')
           +(' · 截至：'+t(rec['as_of']) if rec.get('as_of') else '')+'</p>',
           '<div class="recommendation-positions"><p><strong>未持仓：</strong>'+t(_recommendation_text(rec.get('new_position') or '行动待明确'))+'</p>',
           '<p><strong>已有持仓：</strong>'+t(_recommendation_text(rec.get('existing_position') or '行动待明确'))+'</p></div>']
    if freshness: parts.append('<p class="recommendation-frozen-notice">'+t(freshness)+'</p>')
    quote=_recommendation_quote(rec,memo)
    if quote:
        url=_recommendation_url(quote.get('source_url'))
        parts.append('<div class="recommendation-pricing"><div class="price-tile"><span>'+('参考收盘价（适用性待核实）' if quote.get('decision_eligible') is False else '参考收盘价')+'</span><strong>'+reading_number_html(quote['price'])+' <small>'+t(quote.get('currency',''))+'</small></strong><span>'+t(quote.get('ticker','未记录'))+' · 交易日 '+t(quote['trade_date'])+'</span>'
                     +('<a href="'+t(url)+'">行情来源</a>' if url else '')+'</div>')
        if quote.get('decision_eligible') is False and quote.get('decision_gaps'):
            parts.append('<p>行情适用限制：'+t('；'.join(quote['decision_gaps']))+'</p>')
        for item in _recommendation_comparisons(rec,quote):
            parts.append('<div class="price-tile"><span>估值情景</span><strong>'+reading_number_html(item.get('scenario_price',''))+' <small>'+t(item.get('currency',''))+'</small></strong><span>相对所列参考价的假设空间：'
                         +reading_number_html(item.get('upside_pct',''))+'%</span><span class="muted">来自已保存的用户假设情景，不代表收益预测。</span></div>')
        parts.append('</div>')
    if rec.get('summary'): parts.append('<h3>建议理由</h3>'+prose_html(_recommendation_text(rec['summary'])))
    parts.append('<div class="recommendation-conditions">')
    for field,label in RECOMMENDATION_LISTS:
        if rec.get(field): parts.append('<section><h3>'+label+'</h3><ul>'+''.join('<li>'+t(_recommendation_text(item))+'</li>' for item in rec[field])+'</ul></section>')
    parts.append('</div>')
    evidence,valuations=_recommendation_references(memo,rec)
    if evidence:
        parts+=['<h3>建议依据</h3><ul>']
        for source in evidence:
            url=_recommendation_url(source.get('url')); title=t(source.get('title') or source.get('id','来源'))
            parts.append('<li>'+('<a href="'+t(url)+'">'+title+'</a>' if url else title)+'</li>')
        parts+=['</ul>']
    if valuations:
        parts+=['<h3>本版本引用的估值情景</h3>']
        for item in valuations:
            parts+=['<details open><summary>'+t(item.get('method_name') or item.get('method') or item.get('scenario_id','估值'))+'</summary>']
            if item.get('assumptions'): parts.append('<p>用户假设：'+t(_recommendation_assumptions(item))+'</p>')
            output=item.get('output') or {}
            if output.get('price') is not None: parts.append('<p>情景每股价值：'+t(output['price'])+'</p>')
            if output.get('equity_value_100m') is not None: parts.append('<p>情景股权价值：'+t(output['equity_value_100m'])+' 亿元</p>')
            parts+=['</details>']
    return ''.join(parts+['</section>'])


def extension_html(value):
    """Render our generated analysis Markdown without trusting embedded HTML.

    Only structural Markdown is interpreted. Cell text, quotes, code, links,
    headings and paragraph content are escaped; no content supplies attributes
    or raw tags. Blank lines between generated table rows are tolerated.
    """
    lines = str(value).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    blocks = []

    def nonblank(index):
        while index < len(lines) and not lines[index].strip(): index += 1
        return index

    def cells(line):
        line = line.strip()
        if not line.startswith('|') or not line.endswith('|'): return None
        return [part.strip().replace('\\|', '|') for part in re.split(r'(?<!\\)\|', line[1:-1])]

    def table_start(index):
        head = cells(lines[index])
        divider = nonblank(index + 1)
        parts = cells(lines[divider]) if divider < len(lines) else None
        if head and parts and len(head) == len(parts) and all(re.fullmatch(r':?-{3,}:?', item) for item in parts):
            return head, divider
        return None

    def list_item(line):
        return re.match(r'^\s*(?:([-*+])|(\d+)[.)])\s+(.+)$', line)

    def starts_block(index):
        line = lines[index].strip()
        return bool(line.startswith('```') or re.match(r'^#{1,6}\s+', line) or line.startswith('>')
                    or list_item(line) or table_start(index))

    index = 0
    while index < len(lines):
        if not lines[index].strip(): index += 1; continue
        line = lines[index].strip()
        if line.startswith('```'):
            index += 1; body = []
            while index < len(lines) and not lines[index].strip().startswith('```'):
                body.append(lines[index]); index += 1
            if index < len(lines): index += 1
            blocks.append('<pre><code>' + escape('\n'.join(body)) + '</code></pre>')
            continue
        heading = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading:
            level = str(len(heading[1]))
            blocks.append('<h' + level + '>' + escape(heading[2]) + '</h' + level + '>')
            index += 1; continue
        table = table_start(index)
        if table:
            head, divider = table
            rows = []; index = divider + 1
            while index < len(lines):
                candidate = nonblank(index)
                row = cells(lines[candidate]) if candidate < len(lines) else None
                if row is None or len(row) != len(head): break
                rows.append(row); index = candidate + 1
            blocks.append('<table class="extension-table"><thead><tr>' + ''.join('<th>' + escape(cell) + '</th>' for cell in head)
                          + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + escape(cell) + '</td>' for cell in row)
                          + '</tr>' for row in rows) + '</tbody></table>')
            continue
        if line.startswith('>'):
            body = []
            while index < len(lines) and lines[index].lstrip().startswith('>'):
                body.append(re.sub(r'^\s*> ?', '', lines[index])); index += 1
            blocks.append('<blockquote>' + '<br>'.join(escape(item) for item in body) + '</blockquote>')
            continue
        item = list_item(line)
        if item:
            ordered = bool(item[2]); tag = 'ol' if ordered else 'ul'; items = []
            while index < len(lines):
                candidate = nonblank(index); item = list_item(lines[candidate]) if candidate < len(lines) else None
                if not item or bool(item[2]) != ordered: break
                items.append(item[3]); index = candidate + 1
            blocks.append('<' + tag + '>' + ''.join('<li>' + escape(item) + '</li>' for item in items) + '</' + tag + '>')
            continue
        body = [lines[index]]; index += 1
        while index < len(lines) and lines[index].strip() and not starts_block(index):
            body.append(lines[index]); index += 1
        blocks.append('<p>' + '<br>'.join(escape(item) for item in body) + '</p>')
    return ''.join(blocks)

def markdown(run, audit=False, document_paths=None):
    if not audit: run = enrich(run)
    run=rewrite_document_links(run,document_paths) if document_paths is not None else deepcopy(run)
    req=run["request"];memo=run.get("memo") or {}
    lines=[f"# {memo.get('title') or run['company']+' · '+str(req['year'])+' 年财务研究'}",
        f"**{run['company']} · {req['ticker']}**  |  截止日：{req['as_of']}  |  报告第 {run.get('report_revision',1)} 版"+('  |  样例回放，非实时数据' if req.get('mode')=='sample' else ''),
        f"研究问题：{req['question']}",recommendation_markdown(memo,recommendation_freshness(run)),
        "\n## 核心财务事实（人民币亿元）","| 指标 | 本期 | 上期 | 同比 / 金额变化 |","|---|---:|---:|---:|"]
    for metric,meta in METRICS.items():
        vals=[]
        for year in (req["year"],req["year"]-1):
            f=next((f for f in run["facts"] if f["metric"]==metric and f["year"]==year),None)
            vals.append(financial_amount(f))
        c=next((c for c in run["calculations"] if c["metric"]==metric),None)
        rate=calculation_change(c)
        lines.append(f"| {meta['label']} | {vals[0]} | {vals[1]} | {rate} |")
    if basis_messages(run):
        lines += ['\n### 财务口径冲突'] + ['- '+message for message in basis_messages(run)]
        if audit: lines += ['审计版保留原始计算值；存在上述冲突的结果不代表当前可用结论。']
    lines += ["\n来源状态："+"；".join(f["label"]+" "+str(f["year"])+"："+fact_source_label(f,run) for f in run["facts"])]
    lines += ["\n同比公式：(本期 / 上期 - 1) × 100%；零或负基期不套用该公式。",
        "\n## 核心判断",memo.get("summary","研究尚未完成。")]
    if memo.get('stance'): lines += ['研究立场：'+memo['stance']]
    if memo.get('theses'): lines += ['\n## 核心投资论点']
    for t in memo.get("theses",[]):
        lines += ["\n### "+t["title"],t["body"],"依据："+(reference_markdown(memo,t.get('evidence_ids',[])) or ", ".join(t.get("fact_ids",[])) or '证据待补充'),
            "反方证据/替代解释："+t["counter_evidence"],"修订条件："+t["invalidate_if"]]
    for s in memo.get('sections',[]):
        lines += ['\n## '+s['title'],s['body'],'依据：'+(reference_markdown(memo,s.get('evidence_ids',[])) or '证据待补充')]
    from .questions import snapshot
    question_view=snapshot(run)
    lines += ['\n## 问题级证据清单',question_view['note']]
    for q in question_view['items']:
        if q.get('archived') and not audit: continue
        lines += ['\n### '+q['question']+' · '+q['label']+(' · 已归档' if q.get('archived') else ''),q.get('answer') or '尚未回答','完成条件：'+q['completion_criteria'],
                  '优先级：'+{'high':'高','medium':'中','low':'低'}.get(q.get('priority','medium'),'中')]
        for b in q.get('bindings',[]):
            source=f"[{b['title']}]({export_url(b['url'])})" if b.get('url') else b['title']
            lines += ['- '+('反方' if b['relation']=='counter' else '支持')+'：'+b['quote']+'（'+source+'） · '+b['reason']]
        lines += ['反方：'+(q.get('counter_evidence') or '待补充')]+['缺口：'+g for g in q.get('gaps',[])]
        if q.get('next_action'): lines += ['下一步：'+q['next_action']]
    if memo.get('questions'): lines += ['\n## 下一步核实清单']+['- '+q for q in memo['questions']]
    if memo.get('sources'):
        lines += ['\n## 研究来源目录']
        for s in memo['sources']:
            lines += ['- '+reference_markdown(memo,[s['id']])+' | '+source_description(s)+' | 披露日期：'+str(s.get('published_at') or '待核实')]
    frozen_sources = recommendation_sources(memo) if 'recommendation_sources' in memo else []
    if frozen_sources:
        lines += ['\n## 投资建议冻结来源', '以下来源随本版本建议保存，独立于后续正文资料。']
        for s in frozen_sources:
            lines += ['### '+reference_markdown({'sources':frozen_sources},[s['id']]),
                      '披露日期：'+str(s.get('published_at') or '待核实'), str(s.get('text') or s.get('quote') or '')]
    if memo.get('review'):
        lines += ['\n## 反方审阅记录',memo['review'].get('semantic_status','历史审阅记录，状态待复核')]+['- '+issue_text(v) for v in memo['review'].get('issues',[])]
    limitations=[g["message"] for g in run["gaps"]]+memo.get("limitations",[])
    if limitations: lines += ["\n## 待核实与限制"]+["- "+s for s in limitations]
    if run["evidence"]: lines += ["\n## 数据与计算证据"]
    for e in run["evidence"]:
        lines += [f"\n### {e['id']} · {e['title']}",e["locator"],e.get("source_url") or "本地上传文件",e["quote"]]
    if audit: lines += ["\n## 计算记录"]+[json.dumps(c,ensure_ascii=False) for c in run["calculations"]]
    if audit and run.get("scenario_history"):
        lines += ["\n## 用户情景（探索假设）"]+[json.dumps(s,ensure_ascii=False) for s in run["scenario_history"]]
    elif run.get('scenario_history'):
        lines += ['\n## 用户情景（探索假设）',legacy_scenario_summary(run['scenario_history'][-1])]
    if run.get("valuation_history"):
        lines += ["\n## 多方法估值与假设",valuation_state_message(run)]+([json.dumps(v,ensure_ascii=False,indent=2) for v in run["valuation_history"]] if audit else [valuation_summary(v) for v in current_valuations(run)])
    if audit and run.get("memo_revisions"):
        lines += [f"\n## 报告修订记录（当前第 {run.get('report_revision',1)} 版）"]+[json.dumps(v,ensure_ascii=False,indent=2) for v in run["memo_revisions"]]
    if audit and run.get("attached_documents"):
        lines += ["\n## 对话补充材料", "以下材料已接入全文查阅，未自动覆盖已核验财务事实。"]+[json.dumps(d,ensure_ascii=False) for d in run["attached_documents"]]
    if audit: lines += ["\n## 运行记录"]+[f"- {e['time']} {e['label']}：{e['detail']}" for e in run["events"]]
    from .research_extensions import markdown as extension_markdown
    extra = extension_markdown(run, audit=audit)
    if extra: lines.append(extra)
    lines += ['\n## 交付说明',delivery_note(run,audit,document_paths is not None)]
    if audit: lines += [f"Run ID: {run['id']} | 版本: {run['version']} | 状态：{run['state']}"]
    return re.sub(r"(\|[^\n]*\|)\n\n(?=\|)",r"\1\n","\n\n".join(lines))

def printable(run, audit=False, document_paths=None):
    if not audit: run = enrich(run)
    run=rewrite_document_links(run,document_paths) if document_paths is not None else deepcopy(run)
    req=run["request"];memo=run.get("memo") or {}
    def e(value): return escape(str(value))
    rows=[]
    for metric,meta in METRICS.items():
        current=next((f for f in run["facts"] if f["metric"]==metric and f["year"]==req["year"]),None)
        previous=next((f for f in run["facts"] if f["metric"]==metric and f["year"]==req["year"]-1),None)
        calc=next((c for c in run["calculations"] if c["metric"]==metric),None)
        rate=calculation_change(calc)
        state=fact_source_label(current,run)
        rows.append(f"<tr><th>{e(meta['label'])}</th><td>{financial_amount(current)}</td><td>{financial_amount(previous)}</td><td>{rate}</td><td>{e(state)}</td></tr>")
    sections=['<h2>核心投资论点</h2>'] if memo.get('theses') else []
    for t in memo.get("theses",[]):
        sections.append("<section class='thesis'><h3>"+e(t["title"])+"</h3>"+extension_html(t["body"])+"<p class='muted'>依据："+(reference_html(memo,t.get('evidence_ids',[])) or e(" / ".join(t.get("fact_ids",[]))) or '证据待补充')+"</p><div class='counter'><b>反方 / 替代解释</b><p>"+e(t["counter_evidence"])+"</p><b>何时修订判断</b><p>"+e(t["invalidate_if"])+"</p></div></section>")
    for s in memo.get('sections',[]):
        sections.append('<section><h2>'+e(s['title'])+'</h2>'+extension_html(s['body'])+'<p class="muted">依据：'+(reference_html(memo,s.get('evidence_ids',[])) or '证据待补充')+'</p></section>')
    appendix=[]
    if memo.get('sources'):
        appendix.append('<h2>研究来源目录</h2>'+''.join('<article class="source-entry"><h3>'+reference_html(memo,[s['id']])+'</h3><p class="source-meta">'+e(source_description(s))+' · 披露日期 '+e(s.get('published_at') or '待核实')+'</p><details open><summary>来源摘录</summary>'+prose_html(s.get('text',''))+'</details></article>' for s in memo['sources']))
    frozen_sources=recommendation_sources(memo) if 'recommendation_sources' in memo else []
    if frozen_sources:
        appendix.append('<h2>投资建议冻结来源</h2><p class="source-intro">以下来源随本版本建议保存，独立于后续正文资料。</p>'+''.join(
            '<article class="source-entry"><h3>'+reference_html({'sources':frozen_sources},[s['id']])+'</h3><p class="source-meta">披露日期 '+e(s.get('published_at') or '待核实')+'</p>'+prose_html(s.get('text') or s.get('quote') or '')+'</article>' for s in frozen_sources))
    if memo.get('review'):
        appendix.append('<details open><summary>反方审阅记录</summary><p>'+e(memo['review'].get('semantic_status','历史审阅记录，状态待复核'))+'</p><ul>'+''.join('<li>'+e(issue_text(v))+'</li>' for v in memo['review'].get('issues',[]))+'</ul></details>')
    limitations=[g["message"] for g in run["gaps"]]+memo.get("limitations",[])
    sources=[]
    for source in run["evidence"]:
        url=_recommendation_url(source.get('source_url'))
        link=f"<a href='{e(url)}'>原始来源</a>" if url else "本地记录"
        sources.append(f"<article><h3>{e(source['title'])}</h3><p class='muted'>{e(source['id'])} · {e(source['locator'])} · {link}</p><pre>{e(source['quote'])}</pre></article>")
    calc_html="".join("<pre>"+e(json.dumps(c,ensure_ascii=False,indent=2))+"</pre>" for c in run["calculations"])
    scenario_html=("<h2>用户探索情景</h2>"+"".join("<pre>"+e(json.dumps(s,ensure_ascii=False,indent=2))+"</pre>" for s in run["scenario_history"])) if audit and run.get("scenario_history") else ""
    if not audit and run.get('scenario_history'):
        scenario_html='<h2>用户探索情景</h2><p>'+e(legacy_scenario_summary(run['scenario_history'][-1]))+'</p>'
    if run.get("valuation_history"):
        scenario_html+='<h2>估值假设与报告关联</h2><p>'+e(valuation_state_message(run))+'</p>'
        scenario_html+=("<details><summary>估值假设与计算明细</summary>"+"".join("<article><h3>"+e(v["method_name"])+"</h3><pre>"+e(json.dumps(v,ensure_ascii=False,indent=2))+"</pre></article>" for v in run["valuation_history"])+"</details>") if audit else ''.join('<p>'+e(valuation_summary(v))+'</p>' for v in current_valuations(run))
    if audit and run.get("memo_revisions"):
        scenario_html+="<h2>报告修订记录</h2>"+"".join("<details><summary>第 "+str(v["revision"])+" 版 · "+e(v["change_note"])+"</summary><pre>"+e(json.dumps(v["memo"],ensure_ascii=False,indent=2))+"</pre></details>" for v in run["memo_revisions"])
    if audit and run.get("attached_documents"):
        scenario_html+="<h2>对话补充材料</h2><p>已接入全文查阅，未自动覆盖已核验财务事实。</p>"+"".join("<pre>"+e(json.dumps(d,ensure_ascii=False,indent=2))+"</pre>" for d in run["attached_documents"])
    from .questions import snapshot
    question_view=snapshot(run)
    scenario_html+='<h2>问题级证据清单</h2><p>'+e(question_view['note'])+'</p>'
    for q in question_view['items']:
        if q.get('archived') and not audit: continue
        scenario_html+='<article><h3>'+e(q['question'])+' · '+e(q['label'])+(' · 已归档' if q.get('archived') else '')+'</h3><p>'+e(q.get('answer') or '尚未回答')+'</p><p>完成条件：'+e(q['completion_criteria'])+'</p><p>优先级：'+e({'high':'高','medium':'中','low':'低'}.get(q.get('priority','medium'),'中'))+'</p>'
        for b in q.get('bindings',[]):
            url=_recommendation_url(b.get('url'))
            source='<a href="'+e(url)+'">'+e(b['title'])+'</a>' if url else e(b['title'])
            scenario_html+='<blockquote>'+e(b['quote'])+'</blockquote><p>'+('反方' if b['relation']=='counter' else '支持')+' · '+source+' · '+e(b['reason'])+'</p>'
        if q.get('next_action'): scenario_html+='<p>下一步：'+e(q['next_action'])+'</p>'
        scenario_html+='<p>反方：'+e(q.get('counter_evidence') or '待补充')+'</p><ul>'+''.join('<li>'+e(g)+'</li>' for g in q.get('gaps',[]))+'</ul></article>'
    events="".join("<li>"+e(v["time"])+" · "+e(v["label"])+"："+e(v["detail"])+"</li>" for v in run["events"])
    from .research_extensions import markdown as extension_markdown
    extra = extension_markdown(run, audit=audit)
    if extra: scenario_html += '<section>' + extension_html(extra) + '</section>'
    basis_html = ('<aside class="basis-notice"><h3>财务口径冲突</h3><ul>'+''.join('<li>'+e(message)+'</li>' for message in basis_messages(run))+'</ul></aside>') if basis_messages(run) else ''
    if audit and basis_html: basis_html += '<p>审计版保留原始计算值；存在上述冲突的结果不代表当前可用结论。</p>'
    followups_html=('<h2>继续研究的问题</h2><ul>'+''.join('<li>'+e(q)+'</li>' for q in memo.get('questions',[]))+'</ul>') if memo.get('questions') else ''
    limits_html=('<h2>数据与方法限制</h2><ul>'+''.join('<li>'+e(q)+'</li>' for q in limitations)+'</ul>') if limitations else ''
    data_evidence_html=('<h3>数据与计算证据</h3>'+''.join(sources)) if sources else ''
    audit_meta = ('<p class="muted">运行状态：'+e(run['state'])+' · 模型状态：'+e(run.get('model',{}).get('status','未记录'))+' · Run ID: '+e(run['id'])+' · 应用版本：'+e(run['version'])+'</p>') if audit else ''
    raw_calculations = ('<details open><summary>计算过程与原始字段</summary>'+calc_html+'</details>') if audit else ''
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="icon" href="data:,"><title>{e(run['company'])} · 投资备忘录</title>
<style>{REPORT_CSS}</style></head><body><main class="paper">
<header class="report-header"><div class="brand">析鲸AI · 投资研究备忘录</div>
<p class="company-line">{e(run['company'])}<span>{e(req['ticker'])}</span></p>
<h1>{e(memo.get('title') or str(req['year'])+' 年财务研究')}</h1>
<div class="report-meta"><span>研究截止日<strong>{e(req['as_of'])}</strong></span><span>财务年度<strong>{req['year']}</strong></span><span><strong>报告第 {run.get('report_revision',1)} 版</strong></span>{'<span>样例回放，非实时数据</span>' if req.get('mode')=='sample' else ''}</div>
<p class="research-question"><strong>研究问题</strong> · {e(req['question'])}</p></header>
{recommendation_html(memo,recommendation_freshness(run))}
<section><h2>核心财务事实</h2><p class="muted">人民币亿元，年度合并报表。归母净利润为归属于母公司股东口径。</p>
<table class="financial-table"><thead><tr><th>指标</th><th>{req['year']}</th><th>{req['year']-1}</th><th>同比 / 金额变化</th><th>本期来源</th></tr></thead><tbody>{''.join(rows)}</tbody></table>{basis_html}</section>
<section><h2>核心判断</h2><p><strong>{e(memo.get('stance',''))}</strong></p><div class="summary">{extension_html(memo.get('summary','研究尚未完成'))}</div></section>{''.join(sections)}
{followups_html}{limits_html}
{scenario_html}<section class="appendix"><h2>证据附录</h2>{''.join(appendix)}
{data_evidence_html}{raw_calculations}
{('<details open><summary>运行记录</summary><ul class="muted">'+events+'</ul></details>') if audit else ''}</section>
<footer><h3>交付说明</h3><p class="muted">{e(delivery_note(run,audit,document_paths is not None))}</p>{audit_meta}<p class="print-hint">浏览器打印可保存为 PDF；完整原始记录见证据包。</p></footer></main></body></html>"""
