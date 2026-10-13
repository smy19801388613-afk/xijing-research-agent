import copy
import hashlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from app import main, store
from app.exporting import markdown, printable, evidence_bundle, calculation_change
from app.schemas import RunRequest


@pytest.fixture
def run(tmp_path, monkeypatch):
    for module in (store, main): monkeypatch.setattr(module, 'DATA', tmp_path)
    result=store.create_run(RunRequest().model_dump(mode='json'))
    result.update(company='交付测试',state='completed_with_gaps',report_revision=7,
        memo={'title':'当前判断','summary':'当前核心判断仅此一次','stance':'谨慎观察','sources':[
            {'id':'pdf','title':'半年度报告 · 第 3 页','kind':'pdf','url':'/api/documents/local-one/file#page=3'},
            {'id':'web','title':'外链公告','kind':'pdf','url':'https://example.com/external.pdf#page=4'}],
            'sections':[{'key':'risks','title':'风险','body':'需继续核验','evidence_ids':['pdf','web']}], 'theses':[]},
        memo_revisions=[{'revision':i,'change_note':'历史判断','replaced_at':'2026-10-01','memo':{'summary':f'旧稿独占文字{i}','sources':[]}} for i in range(1,7)],
        document_ids=['local-one'],calculations=[{'metric':'n_income_attr_p','value':None,'change':'320000000'}])
    document=tmp_path/'local.pdf';document.write_bytes(b'%PDF-1.4\nlocal fixture')
    store.put_document({'id':'local-one','path':str(document),'title':'半年报','source_url':'https://example.com/original.pdf'})
    store.save_run(result)
    return result


def test_current_report_is_separate_from_full_audit(run):
    original=copy.deepcopy(run)
    for render in (markdown,printable):
        current=render(run); audit=render(run,audit=True)
        assert current.count('当前核心判断仅此一次')==1
        assert '旧稿独占文字' not in current
        assert all(f'旧稿独占文字{i}' in audit for i in range(1,7))
        assert '金额变化 +3.20 亿元' in current
    assert run==original


def test_bundle_is_portable_and_keeps_original_audit_data(run,tmp_path):
    run['memo']['sources'].append({'id':'original','title':'原URL引用','kind':'pdf','url':'https://example.com/original.pdf#page=2'})
    before=copy.deepcopy(run)
    content=evidence_bundle(run,store.get_document,tmp_path)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in ('report.md','report.html','audit.md','audit.html'):
            text=archive.read(name).decode()
            assert 'documents/local-one.pdf#page=3' in text
            assert 'documents/local-one.pdf#page=2' in text
            assert '/api/documents/local-one/file' not in text
            assert 'https://example.com/external.pdf#page=4' in text
        assert json.loads(archive.read('run.json'))==before
        manifest=json.loads(archive.read('manifest.json'))
        assert manifest['documents'][0]['original_url']=='https://example.com/original.pdf'
        external=next(r for r in manifest['references'] if r['original_url'].endswith('external.pdf#page=4'))
        assert external['requires_network'] and external['access']=='network_required'
        for file in manifest['files']:
            assert hashlib.sha256(archive.read(file['path'])).hexdigest()==file['sha256']
    assert run==before


def test_export_api_and_unavailable_document_are_read_only(run):
    run['document_id']='missing-doc'
    store.save_run(run)
    before=store.get_run(run['id'])
    with TestClient(main.app) as client:
        for kind in ('markdown','html','audit-markdown','audit-html','json','bundle'):
            response=client.get(f"/api/runs/{run['id']}/export?format={kind}")
            assert response.status_code==200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert 'missing-doc' in json.loads(archive.read('manifest.json'))['unavailable_document_ids']
    assert store.get_run(run['id'])==before


def test_change_does_not_force_a_percentage():
    assert calculation_change({'value':None,'change':'-100000000'})=='金额变化 -1.00 亿元'
    assert calculation_change({'value':None})=='不适用/缺失'
    assert calculation_change({'value':'0','change':'0'})=='+0.00%'


def test_question_export_marks_stale_report_and_keeps_archived_audit(run):
    from app import questions
    q = questions.create('当前问题独占文字', priority='high')
    q.update(answer='等待核验', next_action='取得合同原件')
    archived = questions.create('已归档问题独占文字')
    archived['archived'] = True
    run['research_questions'] = [q, archived]
    run['memo'] = questions.freeze_report(run, run['memo'])
    q['next_action'] = '取得合同原件并核验签字'
    for render in (markdown, printable):
        current = render(run)
        audit = render(run, audit=True)
        assert '报告正文保留原版本' in current
        assert '取得合同原件并核验签字' in current
        assert '已归档问题独占文字' not in current
        assert '已归档问题独占文字' in audit
