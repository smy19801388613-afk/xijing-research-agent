from . import competition
import io
import hashlib
import json
import math
import threading
import uuid
from datetime import date
from pathlib import Path
import re
from decimal import Decimal
import pdfplumber
import pypdfium2
from .config import FIXTURES
from .finance import METRICS
from .store import put_document
from . import store

MAX_BYTES=100*1024*1024
MAX_PAGES=800
PDF_LOCK=threading.RLock()

SAMPLE_ID = "midea-2025-summary"

def seed():
    path = FIXTURES / "midea-2025-annual-summary.pdf"
    if path.exists():
        put_document({"id":SAMPLE_ID,"title":"美的集团 2025 年年度报告摘要","ticker":"000333.SZ","year":2025,
            "announced_date":"2026-03-31","path":str(path),"source_url":"https://static.cninfo.com.cn/finalpage/2026-03-31/1225058109.PDF",
            "sha256":hashlib.sha256(competition.read_bytes(path)).hexdigest(),"created_at":"2026-09-28T09:45:43+00:00","kind":"official_sample"})

def pages_for(doc):
    with pdfplumber.open(io.BytesIO(competition.read_bytes(doc["path"]))) as pdf:
        if len(pdf.pages)>MAX_PAGES:
            raise ValueError(f"支持不超过 {MAX_PAGES} 页的 PDF")
        return [p.extract_text() or "" for p in pdf.pages]

def extract(doc, ticker, year, as_of):
    if doc["ticker"]!=ticker or doc["year"]!=year:
        raise ValueError("材料与研究公司或年度不一致")
    if not doc.get("announced_date"):
        raise ValueError("该文件尚未确认公告日期，可在对话中查阅，但不并入按截止日核验的财务事实")
    if doc["announced_date"]>as_of:
        raise ValueError("该材料的公告日期晚于研究截止日")
    pages=pages_for(doc)
    joined="\n".join(pages)
    if ticker.split(".")[0] not in "\n".join(pages[:2]):
        raise ValueError("PDF 中没有匹配的证券代码，暂不合并该材料")
    if not re.search(fr"{year}\s*年.*年度报告",joined[:4000]):
        raise ValueError("未在材料首页确认指定年度的年度报告")
    facts=[]; evidence=[]
    for index,text in enumerate(pages):
        preceding="\n".join(pages[max(0,index-1):index])
        position=re.search(fr"{year}\s*年\s+{year-1}\s*年",text)
        if not position:
            continue
        before=preceding+"\n"+text[:position.start()]
        units=list(re.finditer(r"单位[：:]\s*(?:人民币)?\s*(千元|万元|元)",before))
        if not units or "主要会计数据" not in before:
            continue
        unit=units[-1][1]; multiplier={"千元":1000,"万元":10000,"元":1}[unit]
        # Later quarterly tables must never be treated as annual comparatives.
        section=re.split(r"[（(]2[）)]|分季度主要",text[position.end():])[0]
        for metric, meta in METRICS.items():
            if any(f["metric"]==metric for f in facts):
                continue
            for alias in meta["aliases"]:
                match=re.search(re.escape(alias)+r"\s+([+-]?[\d,]+(?:\.\d+)?)\s+([+-]?[\d,]+(?:\.\d+)?)",section)
                if not match:
                    continue
                eid=f"doc-{metric}"
                evidence.append({"id":eid,"kind":"document","title":doc["title"],"document_id":doc["id"],
                    "page":index+1,"source_url":doc.get("source_url"),"sha256":doc["sha256"],
                    "announced_date":doc["announced_date"],"retrieved_at":doc["created_at"],"quote":match[0],
                    "unit":unit,"locator":f"PDF 第 {index+1} 页 · 主要会计数据表；单位见该表或前页"})
                for offset in (0,1):
                    facts.append({"id":f"{metric}-{year-offset}","metric":metric,"label":meta["label"],"year":year-offset,
                        "value":str(Decimal(match[offset+1].replace(",",""))*multiplier),"original_value":match[offset+1],
                        "original_unit":unit,"currency":"CNY","scope":"consolidated","period_type":"annual",
                        "announced_date":doc["announced_date"],"evidence_ids":[eid],"status":"document_only","missing_reason":None})
                break
    return facts,evidence,pages

def search_pages(pages,queries,doc):
    results=[]
    for query in queries[:4]:
        count=0
        for index,page in enumerate(pages):
            if query and query in page:
                start=max(0,page.find(query)-100)
                results.append({"query":query,"page":index+1,"text":page[start:start+1600],"document_id":doc["id"]})
                count+=1
                if count>=2: break
    return results


def public_document(doc):
    return {k:v for k,v in doc.items() if k not in ("path",)}


def text_pages(content):
    with PDF_LOCK, pypdfium2.PdfDocument(content) as pdf:
        if not 1<=len(pdf)<=MAX_PAGES: raise ValueError(f"PDF 页数必须在 1–{MAX_PAGES} 页之间")
        result=[]
        for i in range(len(pdf)):
            page=pdf[i]
            try:
                text=page.get_textpage()
                try: result.append(text.get_text_range().replace("\r\n","\n"))
                finally: text.close()
            finally: page.close()
        return result


def register_pdf(content,ticker,year,title,announced_date=None,source_url=None,kind="upload",company=None,annual=False):
    if not re.fullmatch(r"\d{6}\.(SZ|SH|BJ)",ticker): raise ValueError("请填写带交易所后缀的证券代码")
    if not 2000<=year<=date.today().year: raise ValueError("报告年度无效")
    if announced_date:
        if date.fromisoformat(announced_date)>date.today(): raise ValueError("公告日期不能晚于今天")
    if len(content)>MAX_BYTES: raise ValueError("PDF 不得超过 100 MB")
    if not content.startswith(b"%PDF"): raise ValueError("请选择有效的 PDF 文件")
    try: pages=text_pages(content)
    except ValueError: raise
    except Exception: raise ValueError("PDF 无法解析，可能损坏、加密或格式不受支持") from None
    front=re.sub(r"\s+","","\n".join(pages[:20]))
    code=ticker.split(".")[0]
    codes=re.findall(r"(?:证券|股票|公司)代码[：:]?(\d{6})",front)
    if codes and code not in codes: raise ValueError("PDF 中的证券代码与所选公司不一致，请检查研究对象")
    annual_years=re.findall(r"(20\d{2})年?年度报告",front[:16000])
    if annual_years and str(year) not in annual_years: raise ValueError("PDF 首页的年报年度与所选年度不一致")
    identified=code in front or bool(company and re.sub(r"\s+","",company) in front)
    if annual and (str(year) not in annual_years or not identified):
        raise ValueError("下载文件未通过公司与年报年度检查，未接入研究")
    digest=hashlib.sha256(content).hexdigest()
    for old in store.list_documents():
        if old.get("sha256")==digest and old["ticker"]==ticker and old["year"]==year and old.get("announced_date")==announced_date:
            ensure_index(old);return old
    identity=uuid.uuid4().hex[:16];folder=store.DATA/"documents";folder.mkdir(parents=True,exist_ok=True)
    path=folder/(identity+".pdf");path.write_bytes(content)
    readable=sum(bool(p.strip()) for p in pages)
    doc={"id":identity,"title":Path(title).name[:180],"ticker":ticker,"year":year,
        "announced_date":announced_date,"created_at":store.now(),"sha256":digest,"path":str(path),
        "source_url":source_url,"kind":kind,"page_count":len(pages),"text_pages":readable,
        "index_status":"ready" if readable==len(pages) else "partial" if readable else "needs_ocr","identity_status":"matched" if identified else "user_declared",
        "date_basis":"public_disclosure" if kind=="public_report" else "user_declared" if announced_date else "unknown"}
    (folder/(identity+".pages.json")).write_text(json.dumps(pages,ensure_ascii=False),encoding="utf-8")
    put_document(doc)
    return doc


def ensure_index(doc):
    folder=store.DATA/"documents";folder.mkdir(parents=True,exist_ok=True)
    path=folder/(doc["id"]+".pages.json")
    if path.exists(): pages=competition.read_json(path)
    else:
        pages=text_pages(competition.read_bytes(doc["path"]))
        temp=path.with_suffix("."+uuid.uuid4().hex+".tmp")
        temp.write_text(json.dumps(pages,ensure_ascii=False),encoding="utf-8");temp.replace(path)
    raw_count=sum(bool(p.strip()) for p in pages)
    ocr_path=folder/(doc["id"]+".ocr.json")
    ocr_data=competition.read_json(ocr_path) if ocr_path.exists() else {}
    for page,item in ocr_data.items():
        if 1<=int(page)<=len(pages) and not pages[int(page)-1].strip(): pages[int(page)-1]=item["text"]
    missing=[i+1 for i,p in enumerate(pages) if not p.strip()]
    update={"page_count":len(pages),"text_pages":raw_count,"ocr_pages":[int(p) for p in ocr_data],"unreadable_pages":missing,
        "index_status":"partial" if missing and len(missing)<len(pages) else "needs_ocr" if missing else "ready"}
    if any(doc.get(k)!=v for k,v in update.items()): doc.update(update);put_document(doc)
    return pages


def attached_documents(chat):
    ids=list(chat.get("document_ids",[]))
    if chat.get("run_id"):
        run=store.get_run(chat["run_id"])
        ids+=run.get("document_ids",[])+([run["document_id"]] if run.get("document_id") else [])
    return [store.get_document(i) for i in dict.fromkeys(ids)]


def refresh_material_availability(run,doc,pages):
    """Resolve a current annual-material gap, never rewrite a saved report."""
    request=run.get('request',{})
    if doc.get('ticker')!=request.get('ticker') or doc.get('year')!=request.get('year'): return
    front=re.sub(r"\s+","","\n".join(pages[:20]))[:16000]
    annual=bool(re.search(fr"{doc['year']}年?年度报告",front))
    if not annual: return
    # An annual-report summary does not resolve a missing full-report limitation.
    summary="摘要" in doc.get("title","") or bool(re.search(fr"{doc['year']}年?年度报告摘要",front[:3000]))
    if summary: return
    def resolved(gap):
        if gap.get('code')!='no_document': return False
        if gap.get('year',doc['year'])!=doc['year'] or gap.get('ticker',doc['ticker'])!=doc['ticker']: return False
        message=str(gap.get('message',''))
        years=re.findall(r'20\d{2}',message)
        return (bool(re.search(r'年度报告|年报',message))
                and not re.search(r'半年|季度|季报|中期',message)
                and all(year==str(doc['year']) for year in years))
    run['gaps']=[gap for gap in run.get('gaps',[]) if not resolved(gap)]


def attach(cid,doc_id,allow_running=False):
    chat=store.get_conversation(cid);doc=store.get_document(doc_id)
    if chat["state"]=="running" and not allow_running: raise ValueError("请等待当前回复结束再接入材料")
    if chat.get("run_id"):
        run=store.get_run(chat["run_id"]);req=run["request"]
        if run["state"] in ("queued","running"): raise ValueError("请等待财务底稿完成后再接入新材料")
        if doc["ticker"]!=req["ticker"] or doc["year"]!=req["year"]: raise ValueError("材料的公司或年度与当前研究不一致")
        if doc.get("announced_date") and doc["announced_date"]>req["as_of"]: raise ValueError("材料的公告日期晚于当前研究截止日")
    pages=ensure_index(doc)
    def add(value):
        ids=value.setdefault("document_ids",[])
        if doc_id not in ids: ids.append(doc_id)
        refs=value.setdefault("attached_documents",[])
        if not any(d["id"]==doc_id for d in refs): refs.append(public_document(doc))
    def already_attached(value):
        return doc_id in value.get('document_ids',[]) and any(d['id']==doc_id for d in value.get('attached_documents',[]))
    if not already_attached(chat): store.update_conversation(cid,add)
    if chat.get("run_id") and not already_attached(run):
        def add_to_run(value):
            add(value)
            refresh_material_availability(value,doc,pages)
        store.update_run(chat["run_id"],add_to_run)
    return {"artifact":"document","document":public_document(doc),"page_count":len(pages),
        "note":"已接入当前对话，支持全文检索和按页查阅；扫描页按需使用本机 OCR，结果需核对原图。接入材料不改写已保存报告或财务事实，可在对话中修订并生成新的报告版本。"}


def search_attached(chat,queries,document_id=None):
    if not isinstance(queries,list): raise ValueError("关键词必须是列表")
    queries=[q.strip() for q in queries if isinstance(q,str) and 2<=len(q.strip())<=40][:6]
    if not queries: raise ValueError("请提供 2–40 字的检索关键词")
    docs=attached_documents(chat)
    if document_id:
        docs=[d for d in docs if d["id"]==document_id]
        if not docs: raise ValueError("该文件尚未接入当前对话")
    snippets=[];unreadable=[];indexed=[]
    for doc in docs:
        for i,text in enumerate(ensure_index(doc)):
            # Keep original offsets so hits after PDF line breaks still open the right excerpt.
            positions=[n for n,char in enumerate(text) if not char.isspace()]
            compact="".join(text[n] for n in positions).lower()
            indexed.append((doc,i,text,compact,positions))
        if doc.get("unreadable_pages"): unreadable.append({"document_id":doc["id"],"pages":doc["unreadable_pages"][:80]})
    groups=[]
    for query in queries:
        terms=list(dict.fromkeys(t.lower() for t in re.split(r"[\s,，、;；]+",query) if t))
        needle=re.sub(r"\s+","",query).lower();candidates=[]
        weights={t:1+math.log((len(indexed)+1)/(1+sum(t in p[3] for p in indexed))) for t in terms}
        for doc,i,text,compact,positions in indexed:
            matched=[t for t in terms if t in compact]
            exact=needle in compact
            if not matched and not exact: continue
            # Coverage first; rare terms and nearby matches distinguish product pages from tables.
            starts=[compact.find(t) for t in matched]
            proximity=2/(1+(max(starts)-min(starts))/160) if len(starts)>1 else 0
            score=12*len(matched)/len(terms)+sum(weights[t]*(1+min(compact.count(t),5)*.12) for t in matched)
            score+=3*exact+proximity-(10 if "目录" in text[:250] else 0)
            at=compact.find(needle) if exact else min(starts)
            start=max(0,positions[at]-240)
            candidates.append((score,{"query":query,"matched_terms":matched,"match_type":"phrase" if exact else "all_terms" if len(matched)==len(terms) else "partial_terms",
                "document_id":doc["id"],"title":doc["title"],"page":i+1,
                "text":text[start:start+2300],"source_url":doc.get("source_url"),"extraction":"windows_ocr" if i+1 in doc.get("ocr_pages",[]) else "pdf_text",
                "url":f"/api/documents/{doc['id']}/file#page={i+1}"}))
        groups.append([v for _,v in sorted(candidates,key=lambda v:v[0],reverse=True)[:3]])
    # Give every requested topic a first result before filling remaining slots; merge repeated pages.
    seen={}
    for rank in range(3):
        for group in groups:
            if len(group)<=rank: continue
            item=group[rank];key=(item['document_id'],item['page'])
            if key in seen:
                seen[key].setdefault('matched_queries',[seen[key]['query']]).append(item['query'])
            elif len(snippets)<14:
                seen[key]=item;snippets.append(item)
    next_action="缺失关键词可能位于扫描页。对 pages_without_text 中连续的财务页调用 read_document，自动进行本机 OCR；不要重复相同检索。" if unreadable else None
    if not snippets and docs and not unreadable:
        next_action="未匹配到关键词。改用更短的产品名或术语，或读取目录定位业务章节；不要重复原查询。"
    return {"artifact":"document_search","snippets":snippets,"document_count":len(docs),"pages_without_text":unreadable,
        "next_action":next_action,
        "note":"片段保留 PDF 页码；OCR 金额可能误识别，须核对原图；尚未自动并入结构化事实。" if docs else "当前对话没有材料，可点击上传 PDF、接入资料或查找公开年报。"}


def read_attached(chat,doc_id,page,count=1):
    doc=next((d for d in attached_documents(chat) if d["id"]==doc_id),None)
    if not doc: raise ValueError("只能读取当前对话已接入的文件")
    if not isinstance(page,int) or not isinstance(count,int) or not 1<=count<=3: raise ValueError("每次可读取连续 1–3 页")
    pages=ensure_index(doc)
    if not 1<=page<=len(pages): raise ValueError("页码超出文件范围")
    missing=[i+1 for i in range(page-1,min(page-1+count,len(pages))) if not pages[i].strip()]
    if missing:
        from .ocr import recognize
        with PDF_LOCK: recognized=recognize(doc["path"],missing)
        path=store.DATA/"documents"/(doc_id+".ocr.json")
        old=competition.read_json(path) if path.exists() else {}
        old.update({str(n):{"text":text,"engine":"Windows OCR zh-Hans-CN","recognized_at":store.now()} for n,text in recognized.items()})
        temp=path.with_suffix("."+uuid.uuid4().hex+".tmp");temp.write_text(json.dumps(old,ensure_ascii=False),encoding="utf-8");temp.replace(path)
        pages=ensure_index(doc)
    return {"artifact":"document_search","snippets":[{"document_id":doc_id,"title":doc["title"],"page":i+1,
        "text":pages[i][:16000],"extraction":"windows_ocr" if i+1 in doc.get("ocr_pages",[]) else "pdf_text",
        "url":f"/api/documents/{doc_id}/file#page={i+1}"} for i in range(page-1,min(page-1+count,len(pages)))],
        "note":"PDF 物理页码可能不同于印刷页码。windows_ocr 为扫描识别，不是核验事实；不得猜测错字、缺位、列关系不清的金额，必须打开原图核对。"}
