import asyncio
import hashlib
import html
import io
import json
import logging
import re
import uuid
import zipfile
from datetime import date
from pathlib import Path
from contextlib import asynccontextmanager,suppress
from urllib.parse import urlparse
from fastapi import FastAPI,HTTPException,UploadFile,File,Form,Request,Body,Query
from fastapi.responses import FileResponse,Response,StreamingResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
import pdfplumber
import pypdfium2
from .config import ROOT,DATA,VERSION,settings
from . import store,documents,connections,securities,valuation,disclosures,competition,runtime,materials
from .schemas import RunRequest,ScenarioRequest,ReviewRequest,ProfileRequest,PreferencesRequest,ValuationRequest,ConversationRequest,TurnRequest,ReportRequest
from .providers import ProviderError,model_json,list_models
from .finance import scenario,audit_text
from .exporting import markdown,printable

@asynccontextmanager
async def lifespan(app):
    store.connection().close()
    documents.seed()
    await asyncio.to_thread(runtime.reconcile)
    async def reconcile_workers():
        while True:
            await asyncio.sleep(5)
            try: await asyncio.to_thread(runtime.reconcile)
            except Exception: logging.exception('Unable to reconcile research worker states')
    watcher=asyncio.create_task(reconcile_workers())
    try: yield
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError): await watcher

app=FastAPI(title="析鲸AI · 对话投研 Agent",version=VERSION,lifespan=lifespan)
from . import financial_periods, valuation_analysis, research_tracking
app.include_router(financial_periods.router)
app.include_router(valuation_analysis.router)
app.include_router(research_tracking.router)

@app.middleware("http")
async def local_origin(request:Request,call_next):
    if request.method not in ("GET","HEAD","OPTIONS"):
        origin=request.headers.get("origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1","localhost"):
            return JSONResponse({"detail":"只接受本机工作台发起的操作"},status_code=403)
    context={}
    match=re.match(r"^/api/(runs|conversations)/([a-f0-9]{16})(?:/|$)", request.url.path)
    if match: context['run_id' if match[1]=='runs' else 'conversation_id']=match[2]
    with competition.scope(**context):
        response = await call_next(request)
        if request.method not in ("GET","HEAD","OPTIONS"):
            competition.audit("api_action",method=request.method,path=request.url.path,status=response.status_code)
    if request.url.path in ('/', '/index.html', '/api/health', '/api/settings'):
        # A desktop launcher must fetch the current shell and running version.
        # Hashed JS/CSS assets can retain their normal browser caching behavior.
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.exception_handler(KeyError)
async def not_found(request,error):
    return JSONResponse({"detail":"未找到该研究任务或材料"},status_code=404)

@app.exception_handler(ValueError)
async def bad_request(request,error):
    return JSONResponse({"detail":str(error)},status_code=400)

@app.exception_handler(ProviderError)
async def provider_error(request,error):
    return JSONResponse({"detail":str(error)},status_code=502)

def launch(run):
    try: runtime.launch('run',run['id'])
    except Exception:
        raise ValueError('无法启动本地研究进程，任务已保留') from None
    return run

@app.get("/api/health")
def health():
    return {"status":"ok","service":"Research Desk","version":VERSION,"competition":competition.POLICY["enabled"],"data_directory":str(DATA.resolve())}

@app.get("/api/settings")
def configuration():
    return connections.public_settings()

@app.post("/api/connections/profiles")
def profile(request:ProfileRequest):
    return connections.save_profile(request.model_dump())

@app.post("/api/connections/preferences")
def preferences(request:PreferencesRequest):
    return connections.save_preferences(request.model_dump())

@app.post("/api/connections/{profile_id}/test")
def test_model(profile_id:str):
    value,meta=model_json('Return JSON {"connected":true}.',{"purpose":"connection_test"},120,profile_id=profile_id)
    if value.get("connected") is not True: raise ProviderError("接口返回成功，但未通过 JSON 输出测试")
    return {"status":"connected","detail":"已完成一次真实模型调用","meta":meta}

@app.get("/api/connections/{profile_id}/models")
def models(profile_id:str): return list_models(profile_id)

@app.get("/api/securities")
def securities_search(q:str=""):
    if len(q)>80: raise ValueError("搜索词过长")
    return securities.search(q)

@app.post("/api/securities/refresh")
def securities_refresh():
    result=securities.search("",refresh=True)
    return result

@app.get("/api/runs")
def runs(): return store.list_runs()

@app.delete("/api/runs/{run_id}")
def delete_run(run_id:str): return store.trash_item("run",run_id)

@app.delete("/api/conversations/{cid}")
def delete_conversation(cid:str): return store.trash_item("conversation",cid)

@app.get("/api/trash")
def trash(): return store.list_trash()

@app.post("/api/trash/{kind}/{identity}/restore")
def restore(kind:str,identity:str): return store.restore_item(kind,identity)

def material_preflight(payload):
    if payload['mode']=='live':
        checked=materials.preflight(payload)
        if not checked['ready']: raise ValueError('；'.join(checked['errors']))
        return checked
    for identity in payload.get('material_ids',[]): materials.read_material(identity)
    return None

@app.post("/api/runs",status_code=202)
def create(request:RunRequest):
    if request.document_id: store.get_document(request.document_id)
    payload=request.model_dump(mode="json");config=connections.load()
    if payload.get('execution_budget') is None: payload['execution_budget']=config.get('execution_budget')
    if payload["data_source"]=="default": payload["data_source"]=config["data_source"]
    if competition.POLICY["enabled"] and payload["mode"]=="live": payload["data_source"]="documents"
    payload["model_profile"]=payload["model_profile"] or config["active_model"]
    if payload['use_model']: connections.resolve_model(payload["model_profile"])
    preflight=material_preflight(payload)
    run=store.create_run(payload)
    if preflight: run=store.update_run(run['id'],lambda r:r.update(material_preflight=preflight))
    return launch(run)

@app.get("/api/runs/{run_id}/valuation-options")
def valuation_options(run_id:str): return valuation.options(store.get_run(run_id))

@app.post("/api/runs/{run_id}/valuations")
def calculate_valuation(run_id:str,request:ValuationRequest):
    return valuation.save_result(run_id,request.method,request.assumptions)

@app.get("/api/conversations")
def conversation_list(run_id:str|None=None): return store.conversations(run_id)

@app.post("/api/conversations",status_code=201)
def conversation_create(request:ConversationRequest): return store.new_conversation(request.run_id)

@app.get("/api/conversations/{cid}")
def conversation_get(cid:str):
    from .coverage_view import conversation_view
    chat=store.get_conversation(cid)
    try: run=store.get_run(chat['run_id']) if chat.get('run_id') else None
    except KeyError: run=None
    return conversation_view(chat,run)

@app.get("/api/conversations/{cid}/documents")
def conversation_documents(cid:str):
    return [documents.public_document(d) for d in documents.attached_documents(store.get_conversation(cid))]

@app.post("/api/conversations/{cid}/documents/{doc_id}")
def attach_document(cid:str,doc_id:str): return documents.attach(cid,doc_id)

@app.post("/api/conversations/{cid}/annual-report")
def fetch_report(cid:str,request:ReportRequest):
    chat=store.get_conversation(cid)
    if chat["state"]=="running": raise ValueError("请等待当前回复结束后再接入材料")
    if chat.get("run_id"):
        req=store.get_run(chat["run_id"])["request"]
        if request.ticker!=req["ticker"] or request.year!=req["year"] or request.as_of.isoformat()!=req["as_of"]:
            raise ValueError("公司、年度或截止日与当前研究不一致")
    doc=disclosures.fetch_annual(request.ticker,request.year,request.as_of.isoformat())
    return documents.attach(cid,doc["id"])

@app.post("/api/conversations/{cid}/messages",status_code=202)
def conversation_turn(cid:str,request:TurnRequest):
    if not request.message.strip(): raise ValueError("请输入研究问题")
    profile=connections.resolve_model(request.profile_id)
    if not profile["model"] or (not profile["key"] and profile["provider"]!="ollama"):
        raise ValueError("请在连接设置中配置当前模型")
    config=connections.load()
    budget=request.execution_budget if request.execution_budget is not None else config.get('execution_budget')
    chat=store.begin_turn(cid,request.message.strip(),profile["id"],config["data_source"],resume_from_turn_id=request.resume_from_turn_id,
                          execution_budget=budget)
    try:
        runtime.launch('conversation',cid,chat['turn_id'])
    except Exception:
        from .agent import finish_error
        finish_error(cid,chat["turn_id"],"无法启动本地 Agent 进程，请重试","failed")
        raise ValueError("无法启动本地 Agent 进程") from None
    return chat

@app.post("/api/conversations/{cid}/cancel")
def conversation_cancel(cid:str,payload:dict|None=Body(default=None)):
    tid=(payload or {}).get('turn_id')
    chat=store.cancel_conversation(cid,tid)
    if chat.get('state')=='cancelled' and (not tid or chat.get('turn_id')==tid):
        target_turn=chat.get('turn_id','')
        result=runtime.stop_conversation(cid,target_turn)
        chat=store.record_termination(cid,target_turn,result)
    return chat

@app.get("/api/conversations/{cid}/events")
async def conversation_events(cid:str):
    store.get_conversation(cid)
    async def generate():
        last=None
        while True:
            value=store.get_conversation(cid)
            if value["updated_at"]!=last:
                last=value["updated_at"]
                from .coverage_view import conversation_view
                try: run=store.get_run(value['run_id']) if value.get('run_id') else None
                except KeyError: run=None
                yield "data: "+json.dumps(conversation_view(value,run),ensure_ascii=False)+"\n\n"
            if value["state"]!="running": break
            await asyncio.sleep(.7)
    return StreamingResponse(generate(),media_type="text/event-stream",headers={"Cache-Control":"no-cache"})

@app.get("/api/runs/{run_id}")
def get(run_id:str):
    from .report_state import report_valuation_status
    run=store.get_run(run_id)
    from .fact_state import enrich
    from .questions import snapshot,report_sync
    return enrich(run) | {"report_valuation_status":report_valuation_status(run),"question_coverage":snapshot(run),"report_sync":report_sync(run)}

@app.get("/api/runs/{run_id}/audit")
def execution_events(run_id:str,limit:int=Query(default=200,ge=1,le=200)):
    from .execution_audit import events_for_run
    records=events_for_run(store.get_run(run_id))
    return {'events':records[-limit:],'total':len(records)}

@app.post("/api/runs/{run_id}/retry",status_code=202)
def retry(run_id:str):
    old=store.get_run(run_id)
    if old["state"] in ("queued","running"): raise ValueError("请先等待或取消当前任务")
    preflight=material_preflight(old['request'])
    run=store.create_run(old['request'],parent=run_id)
    if preflight: run=store.update_run(run['id'],lambda r:r.update(material_preflight=preflight))
    return launch(run)

@app.post("/api/runs/{run_id}/cancel")
def cancel(run_id:str):
    run=store.cancel_run(run_id)
    result=None
    if run['state']=='cancelled':
        result=runtime.stop('run',run_id)
        owner=run.get('execution_owner')
        if owner:
            chat=store.cancel_conversation(owner['conversation_id'],owner['turn_id'])
            if chat.get('turn_id')==owner['turn_id'] and chat.get('state')=='cancelled':
                owned_result=runtime.stop_conversation(owner['conversation_id'],owner['turn_id'])
                store.record_termination(owner['conversation_id'],owner['turn_id'],owned_result)
                if owned_result['retryable']: result=owned_result
    return {"status":run['state'],"detail":"研究已停止，已经完成的结果仍保留" if run['state']=='cancelled' else '研究已结束',
            "termination":result}

@app.get("/api/runs/{run_id}/events")
async def events(run_id:str):
    store.get_run(run_id)
    async def generate():
        last=None
        while True:
            run=get(run_id)
            if run["updated_at"]!=last:
                last=run["updated_at"]
                yield "data: "+json.dumps(run,ensure_ascii=False)+"\n\n"
            if run["state"] not in ("queued","running"): break
            await asyncio.sleep(1)
    return StreamingResponse(generate(),media_type="text/event-stream",headers={"Cache-Control":"no-cache"})

@app.post("/api/runs/{run_id}/scenario")
def calculate_scenario(run_id:str,request:ScenarioRequest):
    run=store.get_run(run_id)
    if run["state"] in ("queued","running"): raise ValueError("请等待财务核对完成后调整情景")
    fact=next((f for f in run["facts"] if f["metric"]=="n_income_attr_p" and f["year"]==run["request"]["year"]),None)
    if not fact or fact["status"]=="conflict": raise ValueError("缺少无冲突的归母净利润，暂不能计算 PE 情景")
    output=scenario(fact["value"],request.growth_pct,request.pe,request.shares_100m)
    result={"inputs":request.model_dump(),"output":output,"base_fact_id":fact["id"],"forecast_year":run["request"]["year"]+1,"created_at":store.now()}
    run["scenario_history"].append(result)
    run["scenario_history"]=run["scenario_history"][-30:]
    store.save_run(run)
    return result

@app.post("/api/runs/{run_id}/review")
def review(run_id:str,request:ReviewRequest):
    run=store.get_run(run_id)
    if not run["facts"]: raise ValueError("当前任务没有财务事实可用于核查")
    from .fact_state import claims
    issues=audit_text(request.text,run["facts"],run["calculations"],run["request"]["year"])+claims(request.text,run)
    return {"issues":issues,"scope":"仅检查本期核心三项指标的金额、现金流增长方向、部分大小关系和过强结论；不代表全文已核验。",
        "status":"issues_found" if issues else "no_rule_hits"}

@app.get("/api/runs/{run_id}/compare/{previous_id}")
def compare(run_id:str,previous_id:str):
    current=store.get_run(run_id);previous=store.get_run(previous_id)
    if current["request"]["ticker"]!=previous["request"]["ticker"]: raise ValueError("版本对比需要相同公司")
    old={f["id"]:f for f in previous["facts"]};new={f["id"]:f for f in current["facts"]}
    changes=[]
    for key in sorted(set(old)|set(new)):
        a=old.get(key);b=new.get(key)
        if not a or not b or a["value"]!=b["value"] or a["status"]!=b["status"]:
            changes.append({"id":key,"before":a,"after":b})
    return {"changes":changes,"memo_changed":current.get("memo")!=previous.get("memo"),
        "note":"比较保存的研究快照；不代表自动发现了新公告"}

@app.get("/api/runs/{run_id}/export")
def export(run_id:str,format:str="markdown",preview:bool=False):
    run=store.get_run(run_id);stem=f"research-{run_id}"
    if format=="json":
        return Response(json.dumps(run,ensure_ascii=False,indent=2),media_type="application/json",headers={"Content-Disposition":f'attachment; filename="{stem}.json"'})
    if format in ("markdown","audit-markdown"):
        audit=format=="audit-markdown"
        return Response(markdown(run,audit=audit),media_type="text/markdown; charset=utf-8",headers={"Content-Disposition":f'attachment; filename="{stem}{"-audit" if audit else ""}.md"'})
    if format in ("html","audit-html"):
        audit=format=="audit-html"
        disposition="inline" if preview else "attachment"
        return Response(printable(run,audit=audit),media_type="text/html",headers={"Content-Disposition":f'{disposition}; filename="{stem}{"-audit" if audit else ""}.html"'})
    if format=="bundle":
        from .exporting import evidence_bundle
        content=evidence_bundle(run,store.get_document,DATA/"runs"/run_id)
        return Response(content,media_type="application/zip",headers={"Content-Disposition":f'attachment; filename="{stem}.zip"'})
    raise ValueError("不支持该导出格式")

@app.get("/api/documents")
def list_docs():
    return [documents.public_document(d) for d in store.list_documents()]

@app.post("/api/documents")
def upload(file:UploadFile=File(...),ticker:str=Form(...),year:int=Form(...),announced_date:str|None=Form(None),conversation_id:str|None=Form(None)):
    if conversation_id:
        chat=store.get_conversation(conversation_id)
        if chat["state"]=="running": raise ValueError("请等待当前回复结束再上传材料")
        if chat.get("run_id"):
            req=store.get_run(chat["run_id"])["request"]
            if ticker!=req["ticker"] or year!=req["year"]: raise ValueError("材料公司或年度与当前研究不一致")
            if announced_date and announced_date>req["as_of"]: raise ValueError("公告日期晚于当前研究截止日")
    content=file.file.read(documents.MAX_BYTES+1)
    doc=documents.register_pdf(content,ticker,year,file.filename or "上传报告.pdf",announced_date or None)
    if conversation_id: documents.attach(conversation_id,doc["id"])
    return documents.public_document(doc)

@app.get("/api/documents/{doc_id}/file")
def source(doc_id:str):
    path=competition.check_file(store.get_document(doc_id)["path"])
    competition.read_bytes(path)
    return FileResponse(path,media_type="application/pdf")

@app.get("/api/documents/{doc_id}/pages/{page}")
def page_image(doc_id:str,page:int):
    doc=store.get_document(doc_id);folder=DATA/"page-images";folder.mkdir(exist_ok=True)
    output=folder/f"{doc_id}-{page}.png"
    if not output.exists():
        with documents.PDF_LOCK, pypdfium2.PdfDocument(competition.read_bytes(doc["path"])) as pdf:
            if not 1<=page<=len(pdf): raise HTTPException(404,"页码不存在")
            pdf_page=pdf[page-1]
            try:
                bitmap=pdf_page.render(scale=1.4)
                try: bitmap.to_pil().save(output)
                finally: bitmap.close()
            finally: pdf_page.close()
    return FileResponse(output,media_type="image/png")

@app.get("/api/runs/{run_id}/questions")
def research_questions(run_id:str):
    from .questions import snapshot
    return snapshot(store.get_run(run_id))

@app.get("/api/runs/{run_id}/question-sources")
def research_question_sources(run_id:str):
    from .questions import bounded_catalog
    return bounded_catalog(store.get_run(run_id))

@app.post("/api/runs/{run_id}/questions")
def research_questions_plan(run_id:str,request:dict):
    from .questions import plan
    ensure_questions_editable(run_id)
    return plan(run_id,request.get('questions',[]))

def ensure_questions_editable(run_id):
    if store.get_run(run_id)['state'] in ('queued','running') or any(c['state']=='running' for c in store.conversations(run_id)):
        raise ValueError('请等待当前研究或对话完成后编辑问题')

@app.patch("/api/runs/{run_id}/questions/{question_id}")
def research_question_edit(run_id:str,question_id:str,request:dict):
    from .questions import edit
    ensure_questions_editable(run_id)
    return edit(run_id,question_id,request,'human')

@app.post("/api/runs/{run_id}/question-answers")
def research_questions_update(run_id:str,request:dict):
    from .questions import update
    ensure_questions_editable(run_id)
    return update(run_id,request.get('answers',[]),'human')

@app.get("/api/runtime-policy")
@app.get("/api/competition")
def competition_status(): return competition.status()

@app.post("/api/materials/preflight")
@app.post("/api/competition/preflight")
def competition_preflight(request:RunRequest):
    return materials.preflight(request.model_dump(mode='json'))

@app.get("/api/materials")
@app.get("/api/competition/materials")
def competition_materials(): return materials.materials()

@app.get("/api/materials/{identity}")
def material_contents(identity:str):
    meta,rows=materials.read_material(identity)
    return {'metadata':{k:v for k,v in meta.items() if k!='path'},'rows':rows}

@app.post("/api/materials")
@app.post("/api/competition/materials")
def competition_import(file:UploadFile=File(...),ticker:str=Form(...),year:int=Form(...),announced_date:str|None=Form(None)):
    return materials.register_material(file.file.read(10*1024*1024+1),file.filename or "material.json",ticker,year,announced_date or None)

@app.get("/api/runs/{run_id}/submission")
def competition_export(run_id:str):
    run=store.get_run(run_id)
    if run['state'] in ('queued','running'): raise ValueError('请等待任务结束后导出提交包')
    return Response(competition.submission_bundle(run),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="submission-{run_id}.zip"'})

dist=ROOT/"web"/"dist"
app.mount("/",StaticFiles(directory=dist,html=True,check_dir=False),name="web")
