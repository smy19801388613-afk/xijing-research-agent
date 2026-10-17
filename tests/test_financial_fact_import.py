"""Annual ledger imports are deterministic, atomic, and provenance preserving."""
from copy import deepcopy

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import financial_periods as fp, research, store


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    value = store.create_run({'ticker': '600887.SH', 'year': 2025, 'as_of': '2026-10-17', 'mode': 'live'})
    value.update(state='completed', memo={'title': '原报告'}, valuation_history=[{
        'id': 'preserved', 'method': 'pe', 'method_name': 'PE', 'assumptions': {}, 'output': {}}])
    for year, revenue, profit, cash in ((2024, '100000000', '20000000', '30000000'), (2025, '120000000', '22000000', '25000000')):
        common = {'ts_code': '600887.SH', 'end_date': f'{year}1231', 'ann_date': f'{year+1}0420', 'report_type': '1'}
        research.add_api_facts(value, 'income', year, common | {'revenue': revenue, 'n_income_attr_p': profit}, 'test')
        research.add_api_facts(value, 'cashflow', year, common | {'n_cashflow_act': cash}, 'test')
    store.save_run(value)
    return value


def fact_ids(run):
    return [f['id'] for f in run['facts']]


def alter(run, **fields):
    return store.update_run(run['id'], lambda r: r['facts'][0].update(fields))


def test_imports_all_six_annual_facts_without_llm_transcription_and_preserves_state(run):
    result = fp.import_facts(run['id'], fact_ids(run))
    assert len(result['records']) == 6
    facts = {f['id']: f for f in run['facts']}
    for row in result['records']:
        fact = facts[row['imported_fact_id']]
        assert row['value'] == row['value_base'] == fact['value']
        assert (row['period'], row['period_type'], row['unit'], row['currency'], row['scope']) == ('FY', 'cumulative', '元', 'CNY', 'consolidated')
        assert row['source_status'] == 'fact_ledger' and row['imported_fact_status'] == 'api_only'
        assert row['imported_fact_verification']['state'] == 'api_single'
        assert row['original_source_ids'] == fact['evidence_ids']
        assert row['announced_date'] == f"{fact['year'] + 1}-04-20"
        assert row['attribution'] == ('attributable' if row['metric'] == 'n_income_attr_p' else 'all_owners')
    before_after = store.get_run(run['id'])
    for field in ('facts', 'evidence', 'memo', 'valuation_history'):
        assert before_after[field] == run[field]
    annual = [c for c in result['analysis']['comparisons'] if c['year'] == 2025 and c['period_type'] == 'cumulative']
    assert {c['metric']: c['value'] for c in annual} == {'revenue': '20.0', 'n_income_attr_p': '10.0', 'n_cashflow_act': '-16.66666666666666666666666667'}
    assert result['analysis']['signals'][0]['source_status'] == 'fact_ledger'


def test_import_is_idempotent_without_revisions_of_unchanged_records(run):
    first = fp.import_facts(run['id'], fact_ids(run))['records']
    second = fp.import_facts(run['id'], list(reversed(fact_ids(run))), origin='human')['records']
    assert second == first and all('history' not in r for r in second)


def test_document_fact_converted_from_thousands_reuses_canonical_value_without_fake_pdf_quote(run):
    def change(r):
        f = r['facts'][0]
        f.update(value='1234000', original_value='1,234', original_unit='千元', status='document_only', evidence_ids=['doc-row'])
        r['evidence'].append({'id': 'doc-row', 'kind': 'document', 'quote': '营业收入 1,234 千元',
            'announced_date': f['announced_date'], 'extraction': 'pdf_text'})
    store.update_run(run['id'], change)
    row = fp.import_facts(run['id'], [run['facts'][0]['id']])['records'][0]
    assert row['value'] == '1234000' and row['unit'] == '元'
    assert '不是重新摘录PDF' in row['quote'] and row['source_status'] == 'fact_ledger'
    assert row['imported_fact_verification']['state'] == 'document_single'
    assert store.get_run(run['id'])['evidence'][-1]['quote'] == '营业收入 1,234 千元'


@pytest.mark.parametrize('change', [
    {'status': 'conflict'}, {'status': 'ocr_pending'}, {'status': 'missing'}, {'status': 'unknown'},
    {'value': None}, {'value': 'NaN'}, {'evidence_ids': []}, {'evidence_ids': ['nonexistent']},
    {'announced_date': None}, {'announced_date': '2027-04-20'}, {'announced_date': '2025-04-21'},
    {'currency': 'USD'}, {'scope': 'parent'}, {'period_type': 'quarter'},
    {'metric': 'total_assets'}, {'original_value': '999'},
])
def test_invalid_fact_rejects_whole_batch_without_partial_records(run, change):
    altered = alter(run, **change)
    before = deepcopy(altered)
    ids = [altered['facts'][1]['id'], altered['facts'][0]['id']]
    with pytest.raises(ValueError): fp.import_facts(run['id'], ids)
    assert store.get_run(run['id']) == before


@pytest.mark.parametrize('change', [{'announced_date': None}, {'announced_date': '2027-04-20'},
    {'extraction': 'windows_ocr'}, {'read_status': 'search_snippet'}, {'quote': ''}, {'status': 'conflict'}])
def test_invalid_original_source_blocks_import_even_if_fact_label_is_usable(run, change):
    updated = store.update_run(run['id'], lambda r: r['evidence'][0].update(change))
    with pytest.raises(ValueError): fp.import_facts(run['id'], [run['facts'][0]['id']])
    assert store.get_run(run['id']) == updated


@pytest.mark.parametrize('target', ['fact_value', 'fact_state', 'original_quote', 'original_date'])
def test_later_fact_or_source_changes_invalidate_import_without_mutating_stored_record(run, target):
    fp.import_facts(run['id'], [run['facts'][0]['id']])
    original = deepcopy(store.get_run(run['id'])['financial_period_records'])
    def change(r):
        if target == 'fact_value': r['facts'][0].update(value='130000000', original_value='130000000')
        elif target == 'fact_state': r['facts'][0]['status'] = 'conflict'
        elif target == 'original_quote': r['evidence'][0]['quote'] += ' source updated'
        else: r['evidence'][0]['announced_date'] = '20250421'
    changed = store.update_run(run['id'], change)
    view = fp.snapshot(changed)
    assert view['records'][0]['source_status'] == 'source_changed'
    assert view['analysis']['cumulative'][0]['value'] is None
    assert store.get_run(run['id'])['financial_period_records'] == original


def test_reimport_of_changed_valid_fact_updates_same_record_with_prior_history(run):
    first = fp.import_facts(run['id'], [run['facts'][0]['id']])['records'][0]
    alter(run, value='130000000', original_value='130000000')
    current = fp.import_facts(run['id'], [run['facts'][0]['id']])['records']
    assert len(current) == 1 and current[0]['id'] == first['id']
    assert current[0]['value'] == '130000000' and current[0]['history'][0]['value'] == first['value']


@pytest.mark.parametrize('ids', [[], None, 'revenue-2025', ['missing'], ['revenue-2025', 'revenue-2025'], [None]])
def test_import_requires_existing_unique_fact_ids(run, ids):
    before = store.get_run(run['id'])
    with pytest.raises(ValueError): fp.import_facts(run['id'], ids)
    assert store.get_run(run['id']) == before


def test_normal_record_route_cannot_relabel_or_change_controlled_ledger_amount(run):
    row = fp.import_facts(run['id'], [run['facts'][0]['id']])['records'][0]
    for changes in ({'metric': 'n_income_attr_p'}, {'period': 'Q1'}, {'value': '2024'}, {'unit': '万元'}):
        with pytest.raises(ValueError): fp.save_records(run['id'], [dict(row, **changes)], origin='model')


def test_http_accepts_fact_ids_and_rejects_ambiguous_mixed_payload(run):
    app = FastAPI(); app.include_router(fp.router)
    @app.exception_handler(ValueError)
    async def invalid(request: Request, error): return JSONResponse({'detail': str(error)}, status_code=400)
    client = TestClient(app)
    path = '/api/runs/' + run['id'] + '/financial-periods'
    result = client.post(path, json={'fact_ids': fact_ids(run)})
    assert result.status_code == 200 and len(result.json()['records']) == 6
    assert all(r['origin'] == 'human' for r in result.json()['records'])
    before = store.get_run(run['id'])
    assert client.post(path, json={'fact_ids': fact_ids(run), 'records': []}).status_code == 400
    assert store.get_run(run['id']) == before
