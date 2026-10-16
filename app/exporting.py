from . import competition
import json
import re
from copy import deepcopy
from html import escape
from decimal import Decimal
from .finance import METRICS,number,comparison_view
from .fact_state import verification
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

def export_url(url):
    # Downloaded reports must still resolve local evidence outside the app URL.
    return 'http://127.0.0.1:8920'+url if url.startswith('/api/documents/') else url

def reference_markdown(memo,ids):
    return '；'.join('['+s['title']+']('+export_url(s['url'])+')' if s.get('url') else s['title'] for s in memo_references(memo,ids))

def reference_html(memo,ids):
    return ' · '.join('<a href="'+escape(export_url(s['url']),quote=True)+'">'+escape(s['title'])+'</a>' if s.get('url') else escape(s['title']) for s in memo_references(memo,ids))

def prose_html(value):
    # Escape before the tiny Markdown presentation pass: never execute model HTML.
    return ''.join('<p>'+re.sub(r'\*\*([^*]+)\*\*',r'<strong>\1</strong>',escape(p)).replace('\n','<br>')+'</p>' for p in str(value).split('\n\n') if p.strip())

def markdown(run, audit=False, document_paths=None):
    if not audit: run = comparison_view(run)
    run=rewrite_document_links(run,document_paths) if document_paths is not None else deepcopy(run)
    req=run["request"];memo=run.get("memo") or {}
    lines=[f"# {memo.get('title') or run['company']+' · '+str(req['year'])+' 年财务研究'}",f"研究问题：{req['question']}",
        f"截止日：{req['as_of']} | 模式：{'样例回放' if req['mode']=='sample' else '实时研究'} | 状态：{run['state']}",
        delivery_note(run,audit,document_paths is not None),
        "\n## 核心财务事实（人民币亿元）","| 指标 | 本期 | 上期 | 同比 / 金额变化 |","|---|---:|---:|---:|"]
    for metric,meta in METRICS.items():
        vals=[]
        for year in (req["year"],req["year"]-1):
            f=next((f for f in run["facts"] if f["metric"]==metric and f["year"]==year),None)
            vals.append(f"{number(f['value'])/Decimal(100000000):,.2f}" if f else "缺失")
        c=next((c for c in run["calculations"] if c["metric"]==metric),None)
        rate=calculation_change(c)
        lines.append(f"| {meta['label']} | {vals[0]} | {vals[1]} | {rate} |")
    lines += ["\n来源状态："+"；".join(f["label"]+" "+str(f["year"])+"："+verification(f,run.get("evidence",[]))["label"] for f in run["facts"])]
    lines += ["\n同比公式：(本期 / 上期 - 1) × 100%；零或负基期不套用该公式。",
        "\n## 研究判断（待复核）",memo.get("summary","研究尚未完成。")]
    if memo.get('stance'): lines += ['研究立场：'+memo['stance']]
    for t in memo.get("theses",[]):
        lines += ["\n### "+t["title"],t["body"],"依据："+(reference_markdown(memo,t.get('evidence_ids',[])) or ", ".join(t["fact_ids"])),
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
            lines += ['- '+reference_markdown(memo,[s['id']])+' | '+s.get('read_status',s.get('status',s['kind']))+' | 披露日期：'+str(s.get('published_at') or '待核实')]
    if memo.get('review'):
        lines += ['\n## 反方审阅记录',memo['review'].get('semantic_status','历史审阅记录，状态待复核')]+['- '+issue_text(v) for v in memo['review'].get('issues',[])]
    lines += ["\n## 待核实与限制"]+["- "+g["message"] for g in run["gaps"]]+["- "+s for s in memo.get("limitations",[])]
    lines += ["\n## 数据与计算证据"]
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
    lines += [f"Run ID: {run['id']} | 版本: {run['version']}"]
    return re.sub(r"(\|[^\n]*\|)\n\n(?=\|)",r"\1\n","\n\n".join(lines))

def printable(run, audit=False, document_paths=None):
    if not audit: run = comparison_view(run)
    run=rewrite_document_links(run,document_paths) if document_paths is not None else deepcopy(run)
    req=run["request"];memo=run.get("memo") or {}
    def e(value): return escape(str(value))
    def amount(fact):
        return f"{number(fact['value'])/Decimal(100000000):,.2f}" if fact else "缺失"
    rows=[]
    for metric,meta in METRICS.items():
        current=next((f for f in run["facts"] if f["metric"]==metric and f["year"]==req["year"]),None)
        previous=next((f for f in run["facts"] if f["metric"]==metric and f["year"]==req["year"]-1),None)
        calc=next((c for c in run["calculations"] if c["metric"]==metric),None)
        rate=calculation_change(calc)
        state=verification(current,run.get("evidence",[]))["label"] if current else "缺失"
        rows.append(f"<tr><th>{e(meta['label'])}</th><td>{amount(current)}</td><td>{amount(previous)}</td><td>{rate}</td><td>{state}</td></tr>")
    sections=[]
    for t in memo.get("theses",[]):
        sections.append("<section class='thesis'><h3>"+e(t["title"])+"</h3>"+prose_html(t["body"])+"<p class='muted'>依据："+(reference_html(memo,t.get('evidence_ids',[])) or e(" / ".join(t["fact_ids"])))+"</p><div class='counter'><b>反方 / 替代解释</b><p>"+e(t["counter_evidence"])+"</p><b>何时修订判断</b><p>"+e(t["invalidate_if"])+"</p></div></section>")
    for s in memo.get('sections',[]):
        sections.append('<section><h2>'+e(s['title'])+'</h2>'+prose_html(s['body'])+'<p class="muted">依据：'+(reference_html(memo,s.get('evidence_ids',[])) or '证据待补充')+'</p></section>')
    if memo.get('sources'):
        sections.append('<h2>研究来源目录</h2>'+''.join('<article><h3>'+reference_html(memo,[s['id']])+'</h3><p class="muted">'+e(s.get('read_status',s.get('status',s['kind'])))+' · 披露日期 '+e(s.get('published_at') or '待核实')+'</p><details><summary>来源摘录</summary>'+prose_html(s.get('text',''))+'</details></article>' for s in memo['sources']))
    if memo.get('review'):
        sections.append('<details><summary>反方审阅记录</summary><p>'+e(memo['review'].get('semantic_status','历史审阅记录，状态待复核'))+'</p><ul>'+''.join('<li>'+e(issue_text(v))+'</li>' for v in memo['review'].get('issues',[]))+'</ul></details>')
    limitations=[g["message"] for g in run["gaps"]]+memo.get("limitations",[])
    sources=[]
    for source in run["evidence"]:
        link=f"<a href='{e(source['source_url'])}'>原始来源</a>" if source.get("source_url") else "本地上传"
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
            source='<a href="'+e(export_url(b['url']))+'">'+e(b['title'])+'</a>' if b.get('url') else e(b['title'])
            scenario_html+='<blockquote>'+e(b['quote'])+'</blockquote><p>'+('反方' if b['relation']=='counter' else '支持')+' · '+source+' · '+e(b['reason'])+'</p>'
        if q.get('next_action'): scenario_html+='<p>下一步：'+e(q['next_action'])+'</p>'
        scenario_html+='<p>反方：'+e(q.get('counter_evidence') or '待补充')+'</p><ul>'+''.join('<li>'+e(g)+'</li>' for g in q.get('gaps',[]))+'</ul></article>'
    events="".join("<li>"+e(v["time"])+" · "+e(v["label"])+"："+e(v["detail"])+"</li>" for v in run["events"])
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><link rel="icon" href="data:,"><title>{e(run['company'])} · 研究底稿</title>
<style>body{{font:14px/1.85 system-ui,"Microsoft YaHei",sans-serif;color:#334155;margin:45px auto;padding:0 28px;max-width:980px}}header{{border-bottom:3px solid #a02070;padding-bottom:24px}}.brand{{font-size:12px;letter-spacing:2px;color:#64748b}}h1{{font-size:29px;margin:14px 0}}h2{{margin-top:34px;font-size:20px;color:#475569}}h3{{font-size:15px}}.muted{{font-size:12px;color:#64748b}}table{{width:100%;border-collapse:collapse;text-align:right;font-variant-numeric:tabular-nums}}td,th{{padding:12px;border-bottom:1px solid #e2e8f0}}thead{{background:#f8fafc}}th:first-child{{text-align:left}}.summary,.counter{{padding:18px;background:#fcf6fa;border-left:3px solid #e5bed5}}.thesis{{margin:22px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.7 monospace;background:#f8fafc;padding:15px;border:1px solid #e2e8f0}}a{{color:#a02070}}li{{margin:7px 0}}details{{margin:18px 0}}summary{{cursor:pointer;font-weight:600}}footer{{border-top:1px solid #e2e8f0;margin-top:35px;padding-top:16px}}@media print{{body{{margin:0;max-width:none;font-size:11px}}h2,h3{{break-after:avoid}}tr,.counter{{break-inside:avoid}}.print-hint{{display:none}}}}</style></head><body>
<header><div class="brand">析鲸AI RESEARCH DESK · {e(memo.get('generator_version',run['version']))} · 报告第 {run.get('report_revision',1)} 版</div><h1>{e(memo.get('title') or run['company']+' · '+str(req['year'])+' 年财务研究')}</h1>
<p>{e(req['question'])}</p><p class="muted">截止日 {req['as_of']} · {'样例回放' if req['mode']=='sample' else '实时研究'} · {e(run['state'])} · 模型状态 {e(run['model']['status'])}</p><p class="muted">{e(delivery_note(run,audit,document_paths is not None))}</p><p class="muted print-hint">浏览器按 Ctrl + P 可打印或保存 PDF。</p></header>
<h2>核心财务事实</h2><p class="muted">人民币亿元，年度合并报表。归母净利润为归属于母公司股东口径。</p>
<table><thead><tr><th>指标</th><th>{req['year']}</th><th>{req['year']-1}</th><th>同比 / 金额变化</th><th>本期来源状态</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>{e(memo.get('title','研究判断 · 待复核'))}</h2><p><strong>{e(memo.get('stance',''))}</strong></p><p class="summary">{e(memo.get('summary','研究尚未完成'))}</p>{''.join(sections)}
<h2>继续研究的问题</h2><ul>{''.join('<li>'+e(q)+'</li>' for q in memo.get('questions',[]))}</ul>
<h2>数据与方法限制</h2><ul>{''.join('<li>'+e(q)+'</li>' for q in limitations)}</ul>
{scenario_html}<details><summary>计算过程与原始字段</summary>{calc_html}{''.join(sources)}</details>
{('<details><summary>运行记录</summary><ul class="muted">'+events+'</ul></details>') if audit else ''}<footer class="muted">Run ID: {e(run['id'])} · 数据、计算与研究判断分别保存。完整原始响应见证据包。</footer></body></html>"""
