import io
import copy
import json
import zipfile
from datetime import date,datetime,timezone
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from app import store,research,documents,main,agent,disclosures
from app.config import FIXTURES
from app.schemas import RunRequest

@pytest.fixture
def isolated(tmp_path,monkeypatch):
    for m in (store,research,main,agent): monkeypatch.setattr(m,"DATA",tmp_path)
    documents.seed()
    return tmp_path

@pytest.fixture
def run(isolated):
    r=store.create_run(RunRequest(as_of=date(2026,9,30)).model_dump(mode="json"))
    r.update(state="completed_with_gaps",company="美的集团")
    store.save_run(r)
    return r

def test_delete_and_restore_run_cascades_only_its_live_chats(run):
    c=store.new_conversation(run['id']);old=store.new_conversation(run['id']);other=store.new_conversation()
    store.trash_item('conversation',old['id'])
    store.trash_item('run',run['id'])
    assert not store.list_runs()
    assert [v['id'] for v in store.conversations()]==[other['id']]
    with pytest.raises(KeyError): store.get_run(run['id'])
    with pytest.raises(ValueError): store.restore_item('conversation',c['id'])
    store.restore_item('run',run['id'])
    assert store.get_run(run['id'])['company']=='美的集团'
    assert store.get_conversation(c['id'])['run_id']==run['id']
    with pytest.raises(KeyError): store.get_conversation(old['id'])
    assert store.get_document(documents.SAMPLE_ID)

def test_cannot_delete_active_chat_or_its_research(run):
    c=store.new_conversation(run['id']);store.begin_turn(c['id'],'test','demo','tushare')
    for kind,identity in [('conversation',c['id']),('run',run['id'])]:
        with pytest.raises(ValueError): store.trash_item(kind,identity)
    assert not store.list_trash()

def test_upload_indexes_and_attaches_without_new_research(run):
    c=store.new_conversation(run['id'])
    before=len(store.list_runs())
    with TestClient(main.app) as client:
        response=client.post('/api/documents',data={'ticker':'000333.SZ','year':'2025','conversation_id':c['id']},
            files={'file':('annual.pdf',(FIXTURES/'midea-2025-annual-summary.pdf').read_bytes(),'application/pdf')})
        assert response.status_code==200,response.text
        d=response.json();assert 'path' not in d and d['page_count']==7 and d['index_status']=='ready'
        assert d['announced_date'] is None
        attached=client.get('/api/conversations/'+c['id']+'/documents').json()
        assert attached[0]['id']==d['id']
        hits=documents.search_attached(store.get_conversation(c['id']),['营业收入'])
        assert hits['snippets'] and any(s['page']==5 for s in hits['snippets'])
        page=documents.read_attached(store.get_conversation(c['id']),d['id'],5)
        assert '营业收入' in page['snippets'][0]['text']
        assert '#page=5' in page['snippets'][0]['url']
        assert len(store.list_runs())==before and not store.get_run(run['id'])['facts']
        export=client.get('/api/runs/'+run['id']+'/export?format=bundle')
        with zipfile.ZipFile(io.BytesIO(export.content)) as z: assert 'documents/'+d['id']+'.pdf' in z.namelist()

def test_document_mismatch_cutoff_and_wrong_pdf_rejected(run):
    c=store.new_conversation(run['id'])
    content=(FIXTURES/'midea-2025-annual-summary.pdf').read_bytes()
    for ticker,year in [('601899.SH',2025),('000333.SZ',2024)]:
        with pytest.raises(ValueError): documents.register_pdf(content,ticker,year,'wrong.pdf')
    d=store.get_document(documents.SAMPLE_ID)
    store.put_document(d|{'id':'too-late','announced_date':'2026-10-01'})
    with pytest.raises(ValueError): documents.attach(c['id'],'too-late')
    with pytest.raises(ValueError): documents.read_attached(c,documents.SAMPLE_ID,1)
    with pytest.raises(ValueError): documents.register_pdf(b'<html>error</html>','000333.SZ',2025,'invalid.pdf')

def test_no_text_is_marked_ocr_not_readable(isolated,monkeypatch):
    monkeypatch.setattr(documents,'text_pages',lambda content:['',''])
    d=documents.register_pdf(b'%PDF-scanned','000333.SZ',2025,'scan.pdf')
    assert d['index_status']=='needs_ocr'
    c=store.new_conversation();result=documents.attach(c['id'],d['id'])
    assert 'OCR' in result['note'] and not documents.search_attached(store.get_conversation(c['id']),['收入'])['snippets']

def test_scan_pages_ocr_on_read_then_searchable_with_source_marker(isolated,monkeypatch):
    monkeypatch.setattr(documents,'text_pages',lambda content:['封面',''])
    d=documents.register_pdf(b'%PDF-hybrid','000333.SZ',2025,'hybrid.pdf')
    c=store.new_conversation();documents.attach(c['id'],d['id']);c=store.get_conversation(c['id'])
    first=documents.search_attached(c,['现金流量'])
    assert first['pages_without_text'][0]['pages']==[2]
    calls=[]
    def recognize(path,pages):
        calls.append(pages);return {2:'合并现金流量表\n人民币元\n经营活动产生的现金流量净额 100 80'}
    monkeypatch.setattr('app.ocr.recognize',recognize)
    result=documents.read_attached(c,d['id'],2)
    assert result['snippets'][0]['extraction']=='windows_ocr'
    assert '核对' in result['note']
    documents.read_attached(c,d['id'],2)
    assert calls==[[2]]
    found=documents.search_attached(c,['现金流量'])
    assert found['snippets'][0]['extraction']=='windows_ocr' and not found['pages_without_text']

def test_ocr_failure_does_not_invent_page_text(isolated,monkeypatch):
    monkeypatch.setattr(documents,'text_pages',lambda content:[''])
    d=documents.register_pdf(b'%PDF-badscan','000333.SZ',2025,'scan.pdf')
    c=store.new_conversation();documents.attach(c['id'],d['id']);c=store.get_conversation(c['id'])
    def fail(*args): raise ValueError('OCR unavailable')
    monkeypatch.setattr('app.ocr.recognize',fail)
    with pytest.raises(ValueError): documents.read_attached(c,d['id'],1)
    assert not documents.search_attached(c,['收入'])['snippets']

def test_annual_report_discovery_filters_year_identity_summary_and_cutoff(monkeypatch):
    stamp=int(datetime(2026,3,21,tzinfo=timezone.utc).timestamp()*1000)
    base={'secCode':'601899','announcementId':'1234','announcementTitle':'紫金矿业2025年年度报告','announcementTime':stamp,'adjunctUrl':'finalpage/2026-03-21/1234.PDF'}
    rows=[base,base|{'announcementTitle':'紫金矿业2025年年度报告摘要'},base|{'secCode':'000333'},
        base|{'announcementTitle':'紫金矿业2025年半年度报告'},base|{'announcementTitle':'紫金矿业2024年年度报告'},
        base|{'adjunctUrl':'https://127.0.0.1/private'},base|{'announcementTime':stamp+300*86400000}]
    def request(self,method,url,**kw):
        if 'topSearch' in url: result=[{'code':'601899','orgId':'org','category':'A股','zwjc':'紫金矿业'}]
        else:
            assert kw['data']['stock']=='601899,org'
            result={'totalAnnouncement':len(rows),'announcements':rows}
        return httpx.Response(200,json=result,request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.Client,'request',request)
    found=disclosures.annual_candidates('601899.SH',2025,'2026-09-30')
    assert len(found['items'])==1 and found['items'][0]['announcement_id']=='1234'

def test_agent_reads_uploaded_file_and_does_not_need_bound_run(isolated,monkeypatch):
    c=store.new_conversation();documents.attach(c['id'],documents.SAMPLE_ID)
    store.begin_turn(c['id'],'请查阅 PDF 的收入与经营现金流。','test','documents')
    answers=iter([{'action':'tool','tool':'search_documents','arguments':{'queries':['营业收入','经营活动产生的现金流量']}},
        {'action':'tool','tool':'read_document','arguments':{'document_id':documents.SAMPLE_ID,'page':5}},
        {'action':'respond','message':'已查阅 PDF 第 5 页。'}])
    def model(*args,**kwargs):
        assert args[1]['attached_documents']
        assert '上传 PDF' in args[1]['product_capabilities']['pdf_upload']
        return next(answers),{'model':'test'}
    monkeypatch.setattr(agent,'model_json',model)
    agent.execute(c['id'])
    saved=store.get_conversation(c['id'])
    assert saved['state']=='idle' and all(s['state']=='completed' for s in saved['steps'])
    assert saved['steps'][-1]['result']['snippets'][0]['page']==5


def test_reading_with_negative_report_instruction_can_finish(run,monkeypatch):
    message='继续查阅 PDF，读取对应页码。请本轮总结已找到的依据，保持不估值、不修改报告。'
    assert 'report' not in agent.requested_artifacts(message)
    assert 'valuation' not in agent.requested_artifacts('讨论 PE 的适用性，不重新计算。')
    assert 'report' in agent.requested_artifacts('把这些观点写进报告。')
    c=store.new_conversation(run['id']);documents.attach(c['id'],documents.SAMPLE_ID)
    store.begin_turn(c['id'],message,'test','documents')
    replies=iter([{'action':'tool','tool':'read_document','arguments':{'document_id':documents.SAMPLE_ID,'page':5}},
        {'action':'respond','message':'已查阅 PDF 第 5 页，报告保持原版本。'}])
    monkeypatch.setattr(agent,'model_json',lambda *a,**kw:(next(replies),{'model':'test'}))
    agent.execute(c['id'])
    saved=store.get_conversation(c['id'])
    assert saved['state']=='idle' and len(saved['model_calls'])==2
    assert store.get_run(run['id'])['report_revision']==1


def test_explicit_page_read_requires_read_tool_and_keeps_citations(run,monkeypatch):
    c=store.new_conversation(run['id']);documents.attach(c['id'],documents.SAMPLE_ID)
    store.begin_turn(c['id'],'查阅 PDF 第 5 页原文，不修改报告。','test','documents')
    replies=iter([{'action':'tool','tool':'search_documents','arguments':{'queries':['营业收入']}},
        {'action':'respond','message':'已读完。'},
        {'action':'tool','tool':'read_document','arguments':{'document_id':documents.SAMPLE_ID,'page':5}},
        {'action':'respond','message':'已查阅第 5 页。'}])
    monkeypatch.setattr(agent,'model_json',lambda *a,**kw:(next(replies),{'model':'test'}))
    agent.execute(c['id']);saved=store.get_conversation(c['id'])
    assert saved['state']=='idle' and len(saved['model_calls'])==4
    assert saved['steps'][-1]['tool']=='read_document'
    assert '/api/documents/'+documents.SAMPLE_ID+'/file#page=5' in saved['messages'][-1]['content']


def test_currency_share_capital_cannot_be_relabelled_as_shares():
    results=[{'result':{'snippets':[{'text':'2025 年度 人民币元\n三、股本余额 2,657,788,894 2,658,973,314',
        'extraction':'pdf_text','page':244,'url':'/api/documents/report/file#page=244'}]}}]
    assert '单位是元' in agent.document_unit_feedback('股本余额为 **2,658,973,314 股**。',results)
    assert not agent.document_unit_feedback('股本金额为 2,658,973,314 元，总股数待核实。',results)
    results[0]['result']['snippets'].append({'text':'单位：股\n股份总数 2,658,973,314','extraction':'pdf_text','page':80})
    assert not agent.document_unit_feedback('股份总数 2,658,973,314 股。',results)


def test_repeated_unit_conflict_finishes_with_safe_gap_not_wrong_number(run,monkeypatch):
    c=store.new_conversation(run['id']);store.begin_turn(c['id'],'读取第 244 页，核对股本，不修改报告。','test','documents')
    result={'artifact':'document_search','snippets':[{'text':'2025 年度 人民币元\n三、股本余额 2,658,973,314',
        'extraction':'pdf_text','title':'年报','page':244,'url':'/api/documents/report/file#page=244'}]}
    replies=iter([{'action':'tool','tool':'read_document','arguments':{}},
        {'action':'respond','message':'股本为 2,658,973,314 股。'},
        {'action':'respond','message':'确认是 2,658,973,314 股。'}])
    monkeypatch.setattr(agent,'tool',lambda *a:result)
    monkeypatch.setattr(agent,'model_json',lambda *a,**kw:(next(replies),{'model':'test'}))
    agent.execute(c['id']);saved=store.get_conversation(c['id'])
    assert saved['state']=='idle'
    assert '2658973314 的单位是元' in saved['messages'][-1]['content']
    assert '2,658,973,314 股' not in saved['messages'][-1]['content']
    assert '/file#page=244' in saved['messages'][-1]['content']

def test_request_to_fetch_report_cannot_end_with_only_a_plan(run,monkeypatch):
    c=store.new_conversation(run['id']);store.begin_turn(c['id'],'请找到当前公司的年报全文并接入。','test','tushare')
    monkeypatch.setattr(disclosures,'fetch_annual',lambda *a:store.get_document(documents.SAMPLE_ID))
    replies=iter([{'action':'respond','message':'请自行上传'},
        {'action':'tool','tool':'fetch_annual_report','arguments':{}},
        {'action':'respond','message':'已找到并接入。'}])
    monkeypatch.setattr(agent,'model_json',lambda *a,**kw:(next(replies),{'model':'test'}))
    agent.execute(c['id'])
    saved=store.get_conversation(c['id'])
    assert saved['state']=='idle' and saved['steps'][0]['tool']=='fetch_annual_report'
    assert documents.SAMPLE_ID in saved['document_ids']

def test_history_routes_hidden_until_restore(run):
    c=store.new_conversation(run['id'])
    with TestClient(main.app) as client:
        assert client.delete('/api/conversations/'+c['id']).status_code==200
        assert client.get('/api/conversations/'+c['id']).status_code==404
        assert client.get('/api/trash').json()[0]['kind']=='conversation'
        assert client.post('/api/trash/conversation/'+c['id']+'/restore').status_code==200
        assert client.get('/api/conversations/'+c['id']).status_code==200


def test_reupload_full_report_preserves_saved_memo_and_is_idempotent(run,monkeypatch):
    # Reproduce CATL: a 2025 annual reupload used to rewrite the saved 2026
    # half-year limitation in place, while still showing the same revision.
    run.update(report_revision=2,memo={
        'title':'已保存研究报告','summary':'已查阅2025年度原文',
        'limitations':['未取得宁德时代2026年半年报/三季报官方原文，2026年公司自身经营数据未核验。',
                       '未获取年度报告全文，无法进行深入归因分析。']})
    run['memo_revisions']=[{'revision':1,'memo':{'summary':'原版本'}}]
    run['gaps']=[{'code':'no_document','message':'尚未提供匹配的年度报告 PDF'},
                 {'code':'no_document','message':'尚未取得2026年半年报官方原文'}]
    store.save_run(run);old=copy.deepcopy(run)
    monkeypatch.setattr(documents,'text_pages',lambda content:['证券代码：000333\n2025 年年度报告全文','财务报表及附注'])
    c=store.new_conversation(run['id'])
    with TestClient(main.app) as client:
        def upload():
            return client.post('/api/documents',data={'ticker':'000333.SZ','year':'2025','conversation_id':c['id']},
                files={'file':('annual.pdf',b'%PDF-regression-full-annual','application/pdf')})
        first=upload();assert first.status_code==200,first.text
        first_run=store.get_run(run['id']);first_chat=store.get_conversation(c['id'])
        assert first_run['gaps']==[old['gaps'][1]]
        assert first_run['memo']==old['memo'] and first_run['memo_revisions']==old['memo_revisions']
        assert first_run['report_revision']==2
        count=len(store.list_documents())
        second=upload();assert second.status_code==200,second.text
        assert second.json()['id']==first.json()['id'] and len(store.list_documents())==count
        assert store.get_run(run['id'])==first_run
        assert store.get_conversation(c['id'])==first_chat


@pytest.mark.parametrize('gap',[
    {'code':'no_document','message':'尚未取得2026年年度报告'},
    {'code':'no_document','message':'尚未取得2025年半年报和第三季度报告'},
    {'code':'no_document','message':'尚未取得年度报告','year':2024},
    {'code':'no_document','message':'尚未取得年度报告','ticker':'601899.SH'},
    {'code':'no_document','message':'材料仍需补充，报告类型未明确'},
    {'code':'missing_note','message':'尚未取得2025年年度报告相关附注'},
])
def test_annual_attachment_resolves_only_matching_material_gap(run,gap):
    value=copy.deepcopy(run);value['gaps']=[gap]
    before=copy.deepcopy(value)
    doc={'ticker':'000333.SZ','year':2025,'title':'2025年年度报告.pdf'}
    documents.refresh_material_availability(value,doc,['2025年年度报告全文'])
    assert value==before
