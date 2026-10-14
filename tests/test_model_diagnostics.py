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
