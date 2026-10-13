import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import date
import pytest
from fastapi.testclient import TestClient
from app import competition as comp, store, documents, research, main, providers, web_research, securities
from app.schemas import RunRequest

@pytest.fixture
def controlled(tmp_path,monkeypatch):
    for module in (store,research,main,securities): monkeypatch.setattr(module,'DATA',tmp_path)
    monkeypatch.setattr(comp,'POLICY',{'enabled':True,'model_origins':['https://model.example:443'],'retrieval_origins':[]})
    return tmp_path

@pytest.mark.parametrize('url',['https://model.example/v1/chat/completions','https://model.example:443/v1/messages'])
def test_model_and_retrieval_permissions_are_separate(controlled,url):
    comp.check_network(url,'model')
    with pytest.raises(comp.PolicyError): comp.check_network(url,'retrieval')

@pytest.mark.parametrize('url',['https://model.example.evil/v1','http://model.example/v1','https://model.example:8443','https://user:password@model.example/v1'])
def test_origin_cannot_escape(controlled,url):
    with pytest.raises(comp.PolicyError): comp.check_network(url,'model')


def test_block_before_dns_and_network(controlled,monkeypatch):
    monkeypatch.setattr(web_research,'_public_addresses',lambda *_:pytest.fail('must not resolve blocked host'))
    with pytest.raises(comp.PolicyError):web_research._fetch('https://blocked.example/')


def test_file_escape(controlled,tmp_path):
    outside=tmp_path.parent/'competition-outside.txt';outside.write_text('private')
    for path in (outside,tmp_path/'connections.json'):
        with pytest.raises(comp.PolicyError):comp.read_bytes(path)
    inside=tmp_path/'data.txt';inside.write_text('material')
    assert comp.read_bytes(inside)==b'material'

def test_symlink_escape(controlled,tmp_path):
    outside=tmp_path.parent/'competition-outside-link.txt';outside.write_text('private')
    linked=tmp_path/'link.txt'
    try: linked.symlink_to(outside)
    except OSError as exc:
        if getattr(exc,'winerror',None)==1314: pytest.skip('Windows账户无创建符号链接权限；普通路径边界另行测试')
        raise
    with pytest.raises(comp.PolicyError): comp.read_bytes(linked)


def test_policy_requires_workspace_and_freezes_separately(controlled,monkeypatch):
    monkeypatch.setenv('RESEARCH_COMPETITION_MODE','1');monkeypatch.delenv('RESEARCH_DATA_DIR',raising=False)
    with pytest.raises(comp.PolicyError):comp.load_policy()
    monkeypatch.setenv('RESEARCH_DATA_DIR',str(controlled));monkeypatch.setenv('RESEARCH_COMPETITION_POLICY_JSON',json.dumps({'model_origins':['https://model.example'],'retrieval_origins':[]}))
    assert comp.load_policy()['model_origins']==['https://model.example:443']


def rows():
    return [{'metric':m,'year':y,'value':v,'unit':'元','currency':'CNY','scope':'consolidated','period_type':'annual','announced_date':'2026-03-31'}
        for m,v in [('revenue',100),('n_income_attr_p',10),('n_cashflow_act',5)] for y in (2025,2024)]


def make_run(content=None):
    material=comp.register_material(json.dumps(content or rows()).encode(),'fixed.json','600001.SH',2025)
    r=store.create_run(RunRequest(ticker='600001.SH',year=2025,as_of=date(2026,9,28),mode='live',data_source='documents',use_model=False,material_ids=[material['id']]).model_dump(mode='json'))
    research.execute(r['id']);return store.get_run(r['id'])


def test_supplied_material_flow_never_calls_financial_api(controlled,monkeypatch):
    monkeypatch.setattr(research,'tushare',lambda *_:pytest.fail('no data API allowed'))
    run=make_run()
    assert run['state']=='completed_with_gaps' and len(run['facts'])==6
    assert all(f['status']=='document_only' for f in run['facts'])
    assert all(c['value']=='0' for c in run['calculations'])


def test_unknown_date_does_not_become_historical_fact(controlled):
    data=rows();data[0].pop('announced_date');data[1]['announced_date']='2027-01-01'
    run=make_run(data)
    assert len(run['facts'])==4 and any(g['code'].startswith('material-date') for g in run['gaps'])


def test_material_integrity_and_scope(controlled):
    item=comp.register_material(json.dumps(rows()).encode(),'fixed.json','600001.SH',2025)
    meta,_=comp.read_material(item['id']);from pathlib import Path
    Path(meta['path']).write_text('[]')
    with pytest.raises(ValueError,match='哈希'):comp.read_material(item['id'])
    with pytest.raises(ValueError):comp.read_material('../connections')


def test_submission_hashes_and_no_credentials(controlled):
    run=make_run();(controlled/'connections.json').write_text('PRIVATE_KEY_SENTINEL')
    comp.audit('test',api_key='PRIVATE_KEY_SENTINEL',headers={'Authorization':'Bearer SECRET'})
    with zipfile.ZipFile(io.BytesIO(comp.submission_bundle(run))) as z:
        assert 'competition/audit.json' in z.namelist()
        assert any(p.startswith('materials/') for p in z.namelist())
        manifest=json.loads(z.read('manifest.json'))
        assert manifest['run_id']==run['id']
        for entry in manifest['files']:assert hashlib.sha256(z.read(entry['path'])).hexdigest()==entry['sha256']
        assert b'PRIVATE_KEY_SENTINEL' not in z.read('competition/audit.json')
        assert not any(p.endswith('connections.json') for p in z.namelist())


def test_material_import_and_submission_api(controlled):
    with TestClient(main.app) as client:
        assert client.get('/api/health').json()['data_directory']==str(controlled.resolve())
        response=client.post('/api/competition/materials',data={'ticker':'600001.SH','year':'2025'},files={'file':('fixed.json',json.dumps(rows()).encode(),'application/json')})
        assert response.status_code==200
        assert client.get('/api/competition').json()['enabled'] is True
        run=make_run();assert client.get('/api/runs/'+run['id']+'/submission').status_code==200


def test_non_windows_secret_store_rejects_plaintext():
    import os
    from app import connections
    if os.name!='nt':
        with pytest.raises(ValueError,match='DPAPI'):connections.protect('test-secret')
