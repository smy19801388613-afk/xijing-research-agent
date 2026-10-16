"""Shared material entry points must work without enabling network restrictions."""
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import agent, competition, connections, execution_audit, main, materials, research, securities, store
from app.schemas import RunRequest


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for module in (store, connections, main, research, securities, agent):
        monkeypatch.setattr(module, 'DATA', tmp_path)
    monkeypatch.setattr(competition, 'POLICY', {'enabled':False,'model_origins':[],'retrieval_origins':[]})
    monkeypatch.setattr(connections, 'load', lambda:{'active_model':None,'data_source':'tushare'})
    monkeypatch.setattr(main, 'launch', lambda run:run)
    monkeypatch.setattr(securities, 'search', lambda *_:{'items':[]})
    monkeypatch.setattr(research, 'tushare', lambda *_:pytest.fail('Materials research must not call financial APIs'))
    return tmp_path


def rows():
    return [{'metric':metric,'year':year,'value':value,'unit':'万元','currency':'CNY',
             'scope':'consolidated','period_type':'annual'}
            for metric,value in [('revenue',100),('n_income_attr_p',10),('n_cashflow_act',5)]
            for year in (2025,2024)]


def register(data=None, **kwargs):
    return materials.register_material(json.dumps(rows() if data is None else data).encode(),
        '财务材料.json', kwargs.get('ticker','600001.SH'), kwargs.get('year',2025), '2026-03-31')


def payload(ids=(), **kwargs):
    request={'ticker':'600001.SH','year':2025,'as_of':'2026-09-28','mode':'live',
             'data_source':'documents','use_model':False,'material_ids':list(ids)}
    return RunRequest(**(request|kwargs)).model_dump(mode='json')


def test_normal_material_import_preflight_and_research(workspace):
    client=TestClient(main.app)
    imported=client.post('/api/materials',data={'ticker':'600001.SH','year':'2025','announced_date':'2026-03-31'},
                         files={'file':('data.json',json.dumps(rows()).encode(),'application/json')})
    assert imported.status_code==200
    item=imported.json()
    assert 'path' not in item
    assert (workspace/'structured-materials'/(item['id']+'.json')).is_file()
    assert not (workspace/'competition-materials').exists()
    assert client.get('/api/materials').json()==[item]
    detail=client.get('/api/materials/'+item['id']).json()
    assert detail['metadata']==item and detail['rows']==rows()
    request=payload([item['id']])
    checked=client.post('/api/materials/preflight',json=request)
    assert checked.status_code==200 and checked.json()['ready']
    response=client.post('/api/runs',json=request)
    assert response.status_code==202
    run=response.json()
    assert run['material_preflight']['materials'][0]['accepted_rows']==6
    research.execute(run['id'])
    result=store.get_run(run['id'])
    assert result['state']=='completed_with_gaps' and len(result['facts'])==6
    assert all(fact['status']=='document_only' for fact in result['facts'])
    assert next(fact for fact in result['facts'] if fact['id']=='revenue-2025')['value']=='1000000'


def test_legacy_materials_remain_readable_without_migration(workspace):
    content=json.dumps(rows()).encode()
    folder=workspace/'competition-materials';folder.mkdir()
    identity='0123456789abcdef';path=folder/(identity+'.json');path.write_bytes(content)
    meta={'id':identity,'filename':'legacy.json','ticker':'600001.SH','year':2025,'announced_date':'2026-03-31',
          'path':str(path),'sha256':hashlib.sha256(content).hexdigest(),'row_count':6,'created_at':'2026-09-28T00:00:00Z'}
    (folder/(identity+'.meta.json')).write_text(json.dumps(meta),encoding='utf-8')
    fresh=register()
    assert {item['id'] for item in materials.materials()}=={identity,fresh['id']}
    assert materials.read_material(identity)==(meta,rows())
    assert materials.preflight(payload([identity,fresh['id']]))['ready']
    assert path.read_bytes()==content


@pytest.mark.parametrize('path',['/api/materials','/api/competition/materials'])
def test_old_and_new_import_routes_share_the_same_normal_library(workspace,path):
    client=TestClient(main.app)
    response=client.post(path,data={'ticker':'600001.SH','year':'2025','announced_date':'2026-03-31'},
                         files={'file':('data.json',json.dumps(rows()).encode(),'application/json')})
    assert response.status_code==200
    assert client.get('/api/materials').json()==client.get('/api/competition/materials').json()
    request=payload([response.json()['id']])
    assert client.post('/api/materials/preflight',json=request).json()==client.post('/api/competition/preflight',json=request).json()
    assert client.get('/api/runtime-policy').json()==client.get('/api/competition').json()


@pytest.mark.parametrize('change',[{'ticker':'600002.SH'},{'year':2024},{'as_of':'2026-01-01'}])
def test_normal_start_rejects_materials_outside_company_year_or_cutoff(workspace,change):
    item=register();client=TestClient(main.app)
    request=payload([item['id']],**change)
    assert not client.post('/api/materials/preflight',json=request).json()['ready']
    assert client.post('/api/runs',json=request).status_code==400
    assert store.list_runs()==[]


def test_normal_start_rechecks_hash_after_successful_preflight(workspace):
    item=register();client=TestClient(main.app);request=payload([item['id']])
    assert client.post('/api/materials/preflight',json=request).json()['ready']
    meta,_=materials.read_material(item['id'])
    Path(meta['path']).write_text('[]',encoding='utf-8')
    assert client.get('/api/materials/'+item['id']).status_code==400
    assert client.post('/api/runs',json=request).status_code==400
    assert store.list_runs()==[]


def test_retry_rechecks_material_and_preserves_preflight(workspace):
    item=register();client=TestClient(main.app)
    created=client.post('/api/runs',json=payload([item['id']])).json()
    store.update_run(created['id'],lambda run:run.update(state='completed_with_gaps'))
    retried=client.post('/api/runs/'+created['id']+'/retry')
    assert retried.status_code==202
    assert retried.json()['material_preflight']['ready']
    meta,_=materials.read_material(item['id'])
    Path(meta['path']).write_text('[]',encoding='utf-8')
    assert client.post('/api/runs/'+created['id']+'/retry').status_code==400
    assert len(store.list_runs())==2


def test_default_source_resolves_and_only_material_workflows_require_upload(workspace,monkeypatch):
    client=TestClient(main.app)
    request=payload(data_source='default')
    assert client.post('/api/materials/preflight',json=request).json()['ready']
    assert client.post('/api/runs',json=request).status_code==202
    monkeypatch.setattr(connections,'load',lambda:{'active_model':None,'data_source':'documents'})
    assert not client.post('/api/materials/preflight',json=request).json()['ready']
    assert client.post('/api/runs',json=request).status_code==400
    monkeypatch.setattr(competition,'POLICY',{'enabled':True,'model_origins':[],'retrieval_origins':[]})
    assert not client.post('/api/materials/preflight',json=payload(data_source='tushare')).json()['ready']


def test_explicit_invalid_materials_also_block_api_data_source(workspace):
    item=register(ticker='600002.SH');client=TestClient(main.app)
    assert client.post('/api/runs',json=payload([item['id']],data_source='tushare')).status_code==400


def test_normal_preflight_and_execution_agree_on_excluded_rows(workspace):
    data=rows();data[0]['announced_date']='2027-01-01';data[1]['announced_date']='invalid'
    item=register(data);request=payload([item['id']]);checked=materials.preflight(request)
    assert checked['ready'] and checked['materials'][0]['accepted_rows']==4
    run=store.create_run(request);materials.apply_materials(run)
    assert len(run['facts'])==4 and len(run['gaps'])==2


def test_invalid_announcement_date_is_rejected_before_writing(workspace):
    with pytest.raises(ValueError,match='公告日期'):
        materials.register_material(json.dumps(rows()).encode(),'data.json','600001.SH',2025,'invalid')
    assert not (workspace/'structured-materials').exists()


def test_run_audit_is_bounded_and_does_not_include_other_runs(workspace):
    run=store.create_run(payload(data_source='tushare'))
    other=store.create_run(payload(data_source='tushare'))
    for index in range(4): execution_audit.audit('test_step',run_id=run['id'],index=index)
    execution_audit.audit('other_step',run_id=other['id'])
    expected=execution_audit.events_for_run(run)
    client=TestClient(main.app)
    response=client.get('/api/runs/'+run['id']+'/audit?limit=2')
    assert response.status_code==200
    result=response.json()
    assert result['total']==len(expected) and [event['index'] for event in result['events']]==[2,3]
    assert not any(event['kind']=='other_step' for event in expected)
    assert client.get('/api/runs/'+run['id']+'/audit?limit=201').status_code==422
    assert client.get('/api/runs/missing/audit').status_code==404


def test_chat_can_start_normal_research_with_explicit_structured_material(workspace,monkeypatch):
    from app import deep_research, providers
    def no_model(*args,**kwargs): pytest.fail('Selected-material financial research must not call a model')
    for module in (agent,research,deep_research,providers): monkeypatch.setattr(module,'model_json',no_model)
    item=register()
    chat=store.new_conversation()
    chat=store.begin_turn(chat['id'],'使用所选结构化材料创建 600001.SH 的 2025 年财务研究，核对收入、归母净利润和经营现金流。',
                          'unused-test-model','documents')
    result=agent.tool(chat,'start_research',{'ticker':'600001.SH','year':2025,'material_ids':[item['id']]})
    assert result['state']=='completed_with_gaps' and result['fact_count']==6
    run=store.get_run(result['run_id']);saved_chat=store.get_conversation(chat['id'])
    assert run['request']['material_ids']==[item['id']] and run['request']['data_source']=='documents'
    assert run['request']['use_model'] is False
    assert run['material_preflight']['ready'] and run['material_preflight']['materials'][0]['accepted_rows']==6
    assert saved_chat['run_id']==run['id'] and saved_chat['turn_contexts'][chat['turn_id']]['run_id']==run['id']
    assert all(fact['status']=='document_only' for fact in run['facts'])
    assert next(fact for fact in run['facts'] if fact['id']=='revenue-2025')['value']=='1000000'


@pytest.mark.parametrize('problem',['wrong_company','changed_hash'])
def test_chat_rejects_ineligible_material_before_creating_or_rebinding_run(workspace,monkeypatch,problem):
    item=register(ticker='600002.SH' if problem=='wrong_company' else '600001.SH')
    if problem=='changed_hash':
        meta,_=materials.read_material(item['id'])
        Path(meta['path']).write_text('[]',encoding='utf-8')
    old=store.create_run(payload(data_source='tushare',ticker='600003.SH'))
    old=store.update_run(old['id'],lambda run:run.update(state='completed_with_gaps'))
    chat=store.new_conversation(old['id'])
    chat=store.begin_turn(chat['id'],'新建 600001.SH 的 2025 年财务研究，使用选定结构化材料。',
                          'unused-test-model','documents')
    monkeypatch.setattr(research,'execute',lambda *_:pytest.fail('Rejected material must not start a research run'))
    before_runs=store.list_runs();before_chat=store.get_conversation(chat['id']);before_old=store.get_run(old['id'])
    with pytest.raises(ValueError,match='不匹配|内容已变化'):
        agent.tool(chat,'start_research',{'ticker':'600001.SH','year':2025,'material_ids':[item['id']]})
    assert store.list_runs()==before_runs
    assert store.get_run(old['id'])==before_old
    assert store.get_conversation(chat['id'])==before_chat


def test_explicit_midea_structured_material_does_not_add_unselected_sample_pdf(workspace,monkeypatch):
    from app import documents
    documents.seed()
    assert store.get_document(documents.SAMPLE_ID)
    item=register(ticker='000333.SZ');request=payload([item['id']],ticker='000333.SZ')
    client=TestClient(main.app)
    checked=client.post('/api/materials/preflight',json=request).json()
    assert checked['ready'] and [(entry['kind'],entry['id']) for entry in checked['materials']]==[('structured',item['id'])]
    response=client.post('/api/runs',json=request)
    assert response.status_code==202
    monkeypatch.setattr(research,'extract',lambda *_:pytest.fail('An unselected sample PDF must not enter structured-only research'))
    research.execute(response.json()['id'])
    run=store.get_run(response.json()['id'])
    assert run['state']=='completed_with_gaps' and not run.get('document_id')
    assert run['material_preflight']['materials']==checked['materials']
    assert {fact['id']:fact['value'] for fact in run['facts']}=={
        f"{row['metric']}-{row['year']}":str(row['value']*10000) for row in rows()}
    assert len(run['evidence'])==6 and all(entry['kind']=='material' for entry in run['evidence'])
    assert all(fact['status']=='document_only' for fact in run['facts'])


@pytest.mark.parametrize('conflicting',[False,True])
def test_explicit_midea_pdf_and_json_preserve_both_sources_and_conflicts(workspace,conflicting):
    from app import documents
    from decimal import Decimal
    documents.seed()
    document=store.get_document(documents.SAMPLE_ID)
    pdf_facts,_,_=documents.extract(document,'000333.SZ',2025,'2026-09-28')
    assert len(pdf_facts)==6
    data=[{key:fact[key] for key in ('metric','year','value','currency','scope','period_type')}
          |{'unit':'元','announced_date':document['announced_date']} for fact in pdf_facts]
    if conflicting: data[0]['value']=str(Decimal(data[0]['value'])+Decimal('10000'))
    item=register(data,ticker='000333.SZ')
    request=payload([item['id']],ticker='000333.SZ',document_id=documents.SAMPLE_ID)
    client=TestClient(main.app)
    checked=client.post('/api/materials/preflight',json=request).json()
    assert checked['ready'] and {entry['kind'] for entry in checked['materials']}=={'structured','pdf'}
    response=client.post('/api/runs',json=request)
    assert response.status_code==202
    research.execute(response.json()['id'])
    run=store.get_run(response.json()['id'])
    assert run['document_id']==documents.SAMPLE_ID
    assert {fact['id']:fact['value'] for fact in run['facts']}=={fact['id']:fact['value'] for fact in pdf_facts}
    assert {entry['kind'] for entry in run['evidence']}=={'material','document'}
    assert all(len(fact['evidence_ids'])==2 for fact in run['facts'])
    expected_conflict=pdf_facts[0]['id'] if conflicting else None
    assert {fact['id'] for fact in run['facts'] if fact['status']=='conflict'}==({expected_conflict} if conflicting else set())
    assert all(fact['status'] in ('conflict','document_only') for fact in run['facts'])


def test_selected_material_research_never_refreshes_security_directory(workspace, monkeypatch):
    monkeypatch.setattr(securities, 'search', lambda *_:pytest.fail('No online company lookup for material-only execution'))
    monkeypatch.setattr(securities, 'tushare', lambda *_:pytest.fail('No online stock directory for material-only execution'))
    item=register()
    run=store.create_run(payload([item['id']]))
    research.execute(run['id'])
    saved=store.get_run(run['id'])
    assert saved['state']=='completed_with_gaps' and len(saved['facts'])==6
    assert not any(e['kind']=='network_access' for e in execution_audit.events_for_run(saved))
