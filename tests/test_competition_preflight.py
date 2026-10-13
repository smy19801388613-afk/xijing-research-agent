import csv
import hashlib
import io
import json
import zipfile
from datetime import date

import pytest
from fastapi.testclient import TestClient
from app import competition as comp, connections, main, research, securities, store
from app.schemas import RunRequest


@pytest.fixture
def workspace(tmp_path,monkeypatch):
    for module in (store,main,research,securities): monkeypatch.setattr(module,'DATA',tmp_path)
    monkeypatch.setattr(comp,'POLICY',{'enabled':True,'model_origins':['https://model.example:443'],'retrieval_origins':[]})
    monkeypatch.setattr(connections,'load',lambda:{'active_model':'test','data_source':'documents'})
    monkeypatch.setattr(connections,'resolve_model',lambda *_:{'name':'Test model','model':'test','key':'test-key','base':'https://model.example','provider':'custom'})
    return tmp_path


def sample_rows():
    return [{'metric':m,'year':year,'value':value,'unit':'元','currency':'CNY','scope':'consolidated',
             'period_type':'annual','announced_date':'2026-03-31'}
            for m,value in [('revenue',100),('n_income_attr_p',10),('n_cashflow_act',5)] for year in (2025,2024)]


def request_for(ids,**changes):
    return RunRequest(ticker='600001.SH',year=2025,as_of=date(2026,9,28),mode='live',data_source='documents',
                      use_model=False,material_ids=ids).model_dump(mode='json')|changes


@pytest.mark.parametrize('suffix',['json','csv'])
def test_export_preserves_original_material_bytes(workspace,suffix):
    rows=sample_rows()
    if suffix=='json': content=json.dumps({'rows':rows},ensure_ascii=False,indent=3).encode('utf-8-sig')
    else:
        stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        content=stream.getvalue().encode('utf-8-sig')
    item=comp.register_material(content,'财务材料.'+suffix,'600001.SH',2025)
    run=store.create_run(request_for([item['id']]))
    research.execute(run['id']);run=store.get_run(run['id'])
    with zipfile.ZipFile(io.BytesIO(comp.submission_bundle(run))) as archive:
        raw=archive.read(f"materials/{item['id']}.{suffix}")
        meta=json.loads(archive.read(f"materials/{item['id']}.meta.json"))
        assert raw==content
        assert hashlib.sha256(raw).hexdigest()==meta['sha256']
        for entry in json.loads(archive.read('manifest.json'))['files']:
            assert hashlib.sha256(archive.read(entry['path'])).hexdigest()==entry['sha256']


def test_preflight_and_execution_share_row_eligibility(workspace):
    rows=sample_rows();rows[0].pop('announced_date');rows[1]['announced_date']='2027-01-01';rows[2]['announced_date']='invalid'
    item=comp.register_material(json.dumps(rows).encode(),'mixed.json','600001.SH',2025)
    request=request_for([item['id']]);check=comp.preflight(request)
    assert check['ready'] and check['materials'][0]['status']=='partial'
    assert check['materials'][0]['accepted_rows']==3 and check['materials'][0]['excluded_rows']==3
    run=store.create_run(request);research.execute(run['id']);run=store.get_run(run['id'])
    assert len(run['facts'])==3 and len([g for g in run['gaps'] if g['code'].startswith('material-date')])==3


def test_preflight_blocks_wrong_company_and_invalid_units(workspace):
    rows=sample_rows();rows[0]['unit']='美元'
    item=comp.register_material(json.dumps(rows).encode(),'invalid.json','600001.SH',2025)
    check=comp.preflight(request_for([item['id']]))
    assert not check['ready'] and any('口径' in e for e in check['errors'])
    check=comp.preflight(request_for([item['id']],ticker='600002.SH'))
    assert not check['ready'] and check['materials'][0]['status']=='excluded'
    assert any('公司' in e for e in check['errors'])


def test_preflight_model_readiness_is_not_connection_verification(workspace,monkeypatch):
    item=comp.register_material(json.dumps(sample_rows()).encode(),'data.json','600001.SH',2025)
    request=request_for([item['id']],use_model=True)
    check=comp.preflight(request)
    assert check['ready'] and check['model']['configured'] and check['model']['allowed']
    assert check['model']['connection_verified'] is False
    monkeypatch.setattr(comp,'POLICY',{'enabled':True,'model_origins':[],'retrieval_origins':[]})
    check=comp.preflight(request)
    assert not check['ready'] and check['model']['configured'] and not check['model']['allowed']
    assert 'test-key' not in json.dumps(check)


def test_create_rechecks_material_and_keeps_selection_record(workspace,monkeypatch):
    item=comp.register_material(json.dumps(sample_rows()).encode(),'data.json','600001.SH',2025)
    request=request_for([item['id']])
    monkeypatch.setattr(main,'launch',lambda run:run)
    monkeypatch.setattr(connections,'resolve_model',lambda *_:pytest.fail('material-only task must not require a model'))
    with TestClient(main.app) as client:
        check=client.post('/api/competition/preflight',json=request)
        assert check.status_code==200 and check.json()['ready']
        response=client.post('/api/runs',json=request)
        assert response.status_code==202
        run=store.get_run(response.json()['id'])
        assert run['material_preflight']['materials'][0]['accepted_rows']==6
        meta,_=comp.read_material(item['id'])
        from pathlib import Path
        Path(meta['path']).write_text('[]')
        assert client.post('/api/runs',json=request).status_code==400


def test_pdf_dates_checked_before_start(workspace):
    path=workspace/'material.pdf';path.write_bytes(b'%PDF-test')
    doc={'id':'test-doc','ticker':'600001.SH','year':2025,'title':'Test PDF','announced_date':'2027-01-01',
         'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'created_at':store.now()}
    store.put_document(doc)
    check=comp.preflight(request_for([],document_id='test-doc'))
    assert not check['ready'] and any('晚于' in e for e in check['errors'])
    doc['announced_date']=None;store.put_document(doc)
    check=comp.preflight(request_for([],document_id='test-doc'))
    assert check['ready'] and check['materials'][0]['status']=='partial' and check['warnings']
