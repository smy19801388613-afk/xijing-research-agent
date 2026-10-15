from app import agent, competition, store
from app.providers import ProviderError


def test_agent_preserves_failed_provider_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    chat = store.new_conversation()
    turn = store.begin_turn(chat['id'], '不要用任何工具，解释研究方法', 'stub', 'documents')
    diagnostics = {'status': 'length_exhausted', 'attempt_count': 2,
                   'usage_total': {'completion_tokens': 10200},
                   'attempts': [{'finish_reason': 'length', 'actual_limit': 6800}]}
    def fail(*args, **kwargs):
        raise ProviderError('生成仍超过上限，未保存不完整草稿', diagnostics)
    monkeypatch.setattr(agent, 'model_json', fail)
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert saved['state'] == 'failed'
    assert saved['model_calls'] == [diagnostics | {'turn_id': turn['turn_id']}]
    assert saved['messages'][-1]['delivery']['can_resume']


def test_competition_audit_keeps_numeric_usage_without_exposing_tokens():
    value = {'usage': {'completion_tokens': 123, 'prompt_tokens': 456,
                      'completion_tokens_details': {'reasoning_tokens': 100, 'api_key': 'private'}},
             'requested_output_tokens': 7800, 'max_tokens': 32768,
             'tushare_token': 'private', 'api_key': 'private',
             'other': {'completion_tokens': 'private', 'max_tokens': 'private',
                       'completion_tokens_details': 'private', 'reasoning_tokens': True}}
    clean = competition.clean(value)
    assert clean['usage']['completion_tokens'] == 123
    assert clean['usage']['completion_tokens_details'] == {'reasoning_tokens': 100, 'api_key': '[REDACTED]'}
    assert clean['requested_output_tokens'] == 7800
    assert clean['max_tokens'] == 32768
    assert clean['tushare_token'] == clean['api_key'] == '[REDACTED]'
    assert set(clean['other'].values()) == {'[REDACTED]'}


def test_competition_audit_preserves_turn_ledger_but_redacts_nested_credentials(tmp_path, monkeypatch):
    import json
    import sqlite3
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(competition, 'POLICY', {'enabled': True})
    ledger = {'total_tokens': 1000000, 'reported_tokens': 12000, 'estimated_tokens': 3000,
              'reserved_tokens': 5000, 'used_tokens': 15000, 'remaining_tokens': 980000,
              'request_count': 3, 'completed_requests': 2, 'uncertain_requests': 1}
    competition.audit('model_response', budget_usage=ledger,
        execution_token_usage=ledger | {'api_key': 'private', 'unknown_token': 123,
            'nested': {'api_key': 'private', 'unknown_token': 'private'},
            'invalid': {'reported_tokens': 'private', 'estimated_tokens': True,
                        'reserved_tokens': -1, 'used_tokens': {'api_key': 'private'},
                        'remaining_tokens': 1.5, 'execution_token_usage': 'private'}})
    with sqlite3.connect(tmp_path / 'competition-audit.sqlite3') as connection:
        saved = json.loads(connection.execute('SELECT payload FROM audit').fetchone()[0])
    assert saved['budget_usage'] == ledger
    assert {key: saved['execution_token_usage'][key] for key in ledger} == ledger
    assert saved['execution_token_usage']['api_key'] == '[REDACTED]'
    assert saved['execution_token_usage']['unknown_token'] == '[REDACTED]'
    assert set(saved['execution_token_usage']['nested'].values()) == {'[REDACTED]'}
    assert set(saved['execution_token_usage']['invalid'].values()) == {'[REDACTED]'}
    assert 'private' not in json.dumps(saved)
