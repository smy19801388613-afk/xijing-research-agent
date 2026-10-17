"""Market snapshots prove observed provenance, never invent current prices."""
from datetime import date
import json

import httpx
import pytest

from app import competition, evidence_policy, execution_budget, market_data as market, store


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(market, '_today', lambda: date(2026, 10, 17))
    monkeypatch.setattr(market, '_token', lambda: 'test-token-never-persist')
    value = store.create_run({'ticker': '600887.SH', 'year': 2025, 'as_of': '2026-10-17', 'mode': 'live'})
    return store.update_run(value['id'], lambda r: r.update(state='completed_with_gaps',
        memo={'executive_summary': '原报告'}, valuation_history=[{'id': 'unchanged'}]))


def api(monkeypatch, rows=None, payload=None, status=200):
    calls = []
    if payload is None:
        payload = {'code': 0, 'msg': None, 'data': {'fields': ['ts_code', 'trade_date', 'close'], 'items': rows or []}}
    def post(url, **kwargs):
        calls.append({'url': url, **kwargs})
        return httpx.Response(status, json=payload, request=httpx.Request('POST', url))
    monkeypatch.setattr(market.httpx, 'post', post)
    return calls


def test_latest_observed_close_is_saved_with_original_record_and_no_report_change(run, monkeypatch):
    calls = api(monkeypatch, [['600887.SH', '20261015', 28.19], ['600887.SH', '20261016', 28.46]])
    result = market.fetch_quote(run['id'])
    assert result['artifact'] == 'market_quote' and result['status'] == 'available'
    assert (result['price'], result['trade_date'], result['price_age_days']) == ('28.46', '2026-10-16', 1)
    assert result['age_days'] == result['price_age_days']
    assert result['currency'] == 'CNY' and result['unit'] == '元/股'
    assert len(calls) == 1 and calls[0]['url'] == 'https://api.tushare.pro'
    assert calls[0]['json']['fields'] == 'ts_code,trade_date,close'
    assert calls[0]['json']['params'] == {'ts_code': '600887.SH', 'start_date': '20260917', 'end_date': '20261017'}
    current = store.get_run(run['id'])
    assert current['memo'] == run['memo'] and current['valuation_history'] == run['valuation_history']
    assert current['market_snapshot'] == result and 'test-token-never-persist' not in json.dumps(current)
    ref = market.reference(result)
    assert ref['kind'] == 'api' and ref['status'] == 'api_only' and ref['read_status'] == 'original_record'
    assert ref['source_type'] == 'market_data' and ref['published_at'] == '2026-10-16'
    assert all(part in ref['text'] for part in ('600887.SH', '28.46', '2026-10-16', '元/股'))
    assert evidence_policy.eligibility(ref, run)['supports_claim']


def test_same_observation_has_stable_source_id_but_history_keeps_each_attempt(run, monkeypatch):
    api(monkeypatch, [['600887.SH', '20261016', '28.4600']])
    first = market.fetch_quote(run['id'])
    second = market.fetch_quote(run['id'])
    assert first['source_id'] == second['source_id']
    assert store.get_run(run['id'])['market_snapshot_history'] == [first]
    assert evidence_policy.source_fingerprint(market.reference(first), run) == evidence_policy.source_fingerprint(market.reference(second), run)


def test_decimal_precision_is_not_rounded_by_formatting(run, monkeypatch):
    price = '28.1234567890123456789012345678901234'
    api(monkeypatch, [['600887.SH', '20261016', price]])
    assert market.fetch_quote(run['id'])['price'] == price


def test_failed_new_attempt_does_not_serve_old_price_as_new_quote(run, monkeypatch):
    api(monkeypatch, [['600887.SH', '20261016', '28.46']])
    first = market.fetch_quote(run['id'])
    api(monkeypatch, payload={'code': 2002, 'msg': '权限不足'})
    result = market.fetch_quote(run['id'])
    assert result['status'] == 'missing' and result['price'] is None and result['source_id'] is None
    assert '2002' in result['gaps'][0] and '权限不足' in result['gaps'][0]
    assert market.reference(result) is None
    assert store.get_run(run['id'])['market_snapshot_history'] == [first]


def test_long_gap_is_visible_without_guessing_holiday_or_suspension(run, monkeypatch):
    api(monkeypatch, [['600887.SH', '20261001', '28.46']])
    result = market.fetch_quote(run['id'])
    assert result['status'] == 'available' and result['price_age_days'] == 16
    assert any('16 个自然日' in n and '未核验' in n for n in result['notes'])


def test_historical_cutoff_selects_only_its_window_and_not_today(run, monkeypatch):
    store.update_run(run['id'], lambda r: r['request'].update(as_of='2025-12-31'))
    calls = api(monkeypatch, [['600887.SH', '20251231', '28.46']])
    result = market.fetch_quote(run['id'])
    assert result['as_of'] == result['query_as_of'] == result['trade_date'] == '2025-12-31'
    assert calls[0]['json']['params']['end_date'] == '20251231'


def test_future_run_cutoff_stops_at_actual_today(run, monkeypatch):
    store.update_run(run['id'], lambda r: r['request'].update(as_of='2026-11-30'))
    calls = api(monkeypatch, [['600887.SH', '20261016', '28.46']])
    result = market.fetch_quote(run['id'])
    assert result['as_of'] == '2026-11-30' and result['query_as_of'] == '2026-10-17'
    assert calls[0]['json']['params']['end_date'] == '20261017'
    assert any('未补造未来' in n for n in result['notes'])


def test_explicit_older_date_is_disclosed_and_cannot_claim_latest_cutoff_price(run, monkeypatch):
    calls = api(monkeypatch, [['600887.SH', '20260930', '28.46']])
    result = market.fetch_quote(run['id'], as_of='2026-09-30')
    assert result['as_of'] == '2026-10-17' and result['query_as_of'] == '2026-09-30'
    assert '不代表截止日最新报价' in result['notes'][-1]
    assert calls[0]['json']['params']['end_date'] == '20260930'


@pytest.mark.parametrize('value', ['2026-10-18', '2027-01-01', '2026-02-31', '20261017', 20261017])
def test_invalid_or_future_query_is_rejected_before_network_or_mutation(run, monkeypatch, value):
    calls = api(monkeypatch)
    with pytest.raises(ValueError): market.fetch_quote(run['id'], as_of=value)
    assert not calls and store.get_run(run['id']) == run


@pytest.mark.parametrize('row, reason', [
    (['688981.SH', '20261016', '28.46'], '代码'),
    (['600887.SH', '20261018', '28.46'], '日期超出'),
    (['600887.SH', '20260916', '28.46'], '日期超出'),
    (['600887.SH', '20260231', '28.46'], '日期无效'),
    (['600887.SH', '2026-10-16', '28.46'], '日期格式'),
    (['600887.SH', '20261016', 'NaN'], '有限正数'),
    (['600887.SH', '20261016', 'Infinity'], '有限正数'),
    (['600887.SH', '20261016', '0'], '有限正数'),
    (['600887.SH', '20261016', '-1'], '有限正数'),
    (['600887.SH', '20261016', True], '有效数字'),
    (['600887.SH', '20261016', None], '有效数字'),
    (['600887.SH', '20261016', '1e999999999'], '数量级异常'),
    (['600887.SH', '20261016', '1e-999999999'], '数量级异常'),
    (['600887.SH', '20261016', '1' * 81], '长度异常'),
])
def test_malformed_records_never_become_eligible_quotes(run, monkeypatch, row, reason):
    api(monkeypatch, [['600887.SH', '20261015', '28.45'], row])
    result = market.fetch_quote(run['id'])
    assert result['status'] == 'missing' and reason in result['gaps'][0]
    assert market.reference(result) is None


def test_duplicate_conflicting_latest_close_blocks_whole_response(run, monkeypatch):
    api(monkeypatch, [['600887.SH', '20261016', '28.46'], ['600887.SH', '20261016', '28.47']])
    result = market.fetch_quote(run['id'])
    assert result['status'] == 'missing' and '冲突' in result['gaps'][0]


def test_empty_window_has_explicit_gap_and_no_fabricated_price(run, monkeypatch):
    api(monkeypatch)
    result = market.fetch_quote(run['id'])
    assert result['status'] == 'missing' and '30 日窗口内未返回记录' in result['gaps'][0]
    assert result['price'] is None


def test_no_token_never_requests_network(run, monkeypatch):
    monkeypatch.setattr(market, '_token', lambda: '')
    calls = api(monkeypatch)
    result = market.fetch_quote(run['id'])
    assert not calls and result['status'] == 'missing' and '未配置' in result['gaps'][0]


def test_unreadable_credential_is_a_missing_snapshot_without_network(run, monkeypatch):
    monkeypatch.setattr(market, '_token', lambda: (_ for _ in ()).throw(ValueError('当前 Windows 用户无法解密')))
    calls = api(monkeypatch)
    result = market.fetch_quote(run['id'])
    assert not calls and result['status'] == 'missing' and '无法解密' in result['gaps'][0]


def test_provider_error_redacts_secrets_without_erasing_error_code(run, monkeypatch):
    api(monkeypatch, payload={'code': 2002, 'msg': '权限不足 token=test-token-never-persist abcdefghijklmnopqrstuvwxyz1234567890'})
    result = market.fetch_quote(run['id'])
    text = json.dumps(result, ensure_ascii=False)
    assert '2002' in text and '权限不足' in text
    assert 'test-token-never-persist' not in text and 'abcdefghijklmnopqrstuvwxyz1234567890' not in text


@pytest.mark.parametrize('payload', [[], {}, {'code': '0', 'data': {}}, {'code': 0, 'data': None},
    {'code': 0, 'data': {'fields': ['ts_code', 'close'], 'items': []}},
    {'code': 0, 'data': {'fields': ['ts_code', 'trade_date', 'close', 'close'], 'items': []}},
    {'code': 0, 'data': {'fields': ['ts_code', 'trade_date', 'close'], 'items': [['600887.SH', '20261016']]}}])
def test_invalid_response_contract_is_a_missing_snapshot(run, monkeypatch, payload):
    api(monkeypatch, payload=payload)
    result = market.fetch_quote(run['id'])
    assert result['status'] == 'missing' and result['gaps']


@pytest.mark.parametrize('error', [httpx.ReadTimeout('timeout'), httpx.ConnectError('network')])
def test_network_failure_is_bounded_without_hidden_retries(run, monkeypatch, error):
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        raise error
    monkeypatch.setattr(market.httpx, 'post', post)
    result = market.fetch_quote(run['id'])
    assert result['status'] == 'missing' and len(calls) == 1


def test_http_failure_is_specific_and_never_includes_response_body(run, monkeypatch):
    api(monkeypatch, payload={'secret': 'test-token-never-persist'}, status=403)
    result = market.fetch_quote(run['id'])
    assert result['status'] == 'missing' and 'HTTP 403' in result['gaps'][0]
    assert 'test-token-never-persist' not in json.dumps(result)


def test_shared_deadline_bounds_http_timeout(run, monkeypatch):
    monkeypatch.setattr(execution_budget, 'deadline', lambda: market.time.monotonic() + 1.5)
    calls = api(monkeypatch, [['600887.SH', '20261016', '28.46']])
    assert market.fetch_quote(run['id'])['status'] == 'available'
    assert 0 < calls[0]['timeout'] <= 1.5


def test_expired_budget_does_not_mutate_or_call_network(run, monkeypatch):
    monkeypatch.setattr(execution_budget, 'deadline', lambda: market.time.monotonic() - 1)
    calls = api(monkeypatch)
    with pytest.raises(execution_budget.BudgetExhausted): market.fetch_quote(run['id'])
    assert not calls and store.get_run(run['id']) == run


def test_cancellation_during_http_does_not_overwrite_previous_snapshot(run, monkeypatch):
    def post(*args, **kwargs):
        monkeypatch.setattr(store, 'check_execution', lambda: (_ for _ in ()).throw(store.ExecutionStopped()))
        return httpx.Response(200, json={'code': 0}, request=httpx.Request('POST', market.ENDPOINT))
    monkeypatch.setattr(market.httpx, 'post', post)
    with pytest.raises(store.ExecutionStopped): market.fetch_quote(run['id'])
    assert store.get_run(run['id']) == run


def test_network_policy_block_propagates_before_http_without_mutation(run, monkeypatch):
    monkeypatch.setattr(competition, 'check_network', lambda *a, **k: (_ for _ in ()).throw(competition.PolicyError('blocked')))
    calls = api(monkeypatch)
    with pytest.raises(competition.PolicyError): market.fetch_quote(run['id'])
    assert not calls and store.get_run(run['id']) == run


def test_concurrent_request_identity_change_prevents_old_quote_save(run, monkeypatch):
    original = market._daily
    def changed(*args):
        store.update_run(run['id'], lambda r: r['request'].update(ticker='688981.SH'))
        return original(*args)
    monkeypatch.setattr(market, '_daily', changed)
    api(monkeypatch, [['600887.SH', '20261016', '28.46']])
    with pytest.raises(ValueError, match='已变化'): market.fetch_quote(run['id'])
    assert 'market_snapshot' not in store.get_run(run['id'])


@pytest.mark.parametrize('changes', [{'currency': 'USD'}, {'price': 'NaN'}, {'trade_date': '2026-10-18'},
    {'as_of': '2026-09-01'}, {'source_id': None}, {'status': 'missing'}, {'provider': 'guessed'}, {'text': ''}])
def test_reference_rejects_invalid_available_snapshot(run, monkeypatch, changes):
    api(monkeypatch, [['600887.SH', '20261016', '28.46']])
    result = market.fetch_quote(run['id'])
    assert market.reference(result | changes) is None


def test_data_credential_loading_does_not_resolve_a_model(monkeypatch):
    monkeypatch.setattr(market.connections, 'load', lambda: {'tushare_legacy': False, 'tushare_secret': 'encrypted'})
    monkeypatch.setattr(market.connections, 'protect', lambda value, decrypt: 'test-only' if value == 'encrypted' and decrypt else '')
    assert market._token() == 'test-only'
