"""Research exports must not become an archive of unrelated workspace activity."""
import io
import json
import sqlite3
import zipfile

import pytest

from app import competition, execution_audit, store
from app.schemas import RunRequest


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(competition, 'POLICY',
                        {'enabled': False, 'model_origins': [], 'retrieval_origins': []})
    return tmp_path


def payloads(events):
    return json.dumps(events, ensure_ascii=False)


def completed_run(material_ids=()):
    run = store.create_run(RunRequest(material_ids=list(material_ids)).model_dump(mode='json'))
    run['state'] = 'completed_with_gaps'
    store.save_run(run)
    return run


@pytest.mark.parametrize('controlled', [False, True])
def test_audit_is_available_without_enabling_access_restrictions(workspace, monkeypatch, controlled):
    monkeypatch.setattr(competition, 'POLICY',
                        {'enabled': controlled, 'model_origins': [], 'retrieval_origins': []})
    execution_audit.audit('test_activity', run_id='run-a', detail='OWN_ACTIVITY')
    assert (workspace / 'execution-audit.sqlite3').exists()
    assert 'OWN_ACTIVITY' in payloads(execution_audit.events_for_run({'id': 'run-a'}))


def test_export_selection_never_expands_to_all_turns_of_a_shared_chat(workspace):
    with execution_audit.scope(conversation_id='shared-chat', turn_id='old-turn'):
        execution_audit.audit('model_request', detail='OTHER_TURN_PRIVATE_MESSAGE')
        execution_audit.audit('run_created', run_id='run-other')
    with execution_audit.scope(conversation_id='shared-chat', turn_id='current-turn'):
        execution_audit.audit('model_request', detail='OWN_UNBOUND_PREPARATION')
        execution_audit.audit('run_created', run_id='run-a')
        execution_audit.audit('tool_result', run_id='run-other', detail='OTHER_RUN_SAME_TURN')
    execution_audit.audit('model_request', conversation_id='other-chat', turn_id='current-turn',
                          detail='OTHER_CHAT_PRIVATE_MESSAGE')
    execution_audit.audit('unattributed', detail='WORKSPACE_PRIVATE_MESSAGE')

    selected = payloads(execution_audit.events_for_run({'id': 'run-a'}))
    assert 'OWN_UNBOUND_PREPARATION' in selected
    for private in ('OTHER_TURN_PRIVATE_MESSAGE', 'OTHER_RUN_SAME_TURN',
                    'OTHER_CHAT_PRIVATE_MESSAGE', 'WORKSPACE_PRIVATE_MESSAGE'):
        assert private not in selected


def test_persisted_execution_owner_recovers_pre_run_records_only_for_its_turn(workspace):
    execution_audit.audit('preparation', conversation_id='chat-a', turn_id='turn-a', detail='OWN_PREP')
    execution_audit.audit('preparation', conversation_id='chat-a', turn_id='turn-b', detail='OTHER_PREP')
    run = {'id': 'run-a', 'execution_owner': {'conversation_id': 'chat-a', 'turn_id': 'turn-a'}}
    selected = payloads(execution_audit.events_for_run(run))
    assert 'OWN_PREP' in selected and 'OTHER_PREP' not in selected


def test_scope_is_restored_when_a_tool_fails(workspace):
    with pytest.raises(ValueError):
        with execution_audit.scope(run_id='run-a'):
            execution_audit.audit('tool_start', detail='OWN_STARTED')
            raise ValueError('tool failure')
    execution_audit.audit('unrelated_activity', detail='LATER_WORKSPACE_ACTIVITY')
    selected = payloads(execution_audit.events_for_run({'id': 'run-a'}))
    assert 'OWN_STARTED' in selected and 'LATER_WORKSPACE_ACTIVITY' not in selected


def test_tool_trace_uses_actual_agent_argument_order_without_recording_chat_history(workspace):
    run = completed_run()
    created = store.new_conversation(run['id'])
    chat = store.update_conversation(created['id'], lambda value: value.update(turn_id='turn-a',
        messages=[{'role': 'user', 'content': 'UNRELATED_CHAT_HISTORY'}]))

    @execution_audit.traced('tool')
    def tool(chat, name, args):
        return {'detail': 'CURRENT_TOOL_RESULT'}

    tool(chat, 'read_document', {'page': 3})
    events = execution_audit.events_for_run(run)
    assert 'CURRENT_TOOL_RESULT' in payloads(events)
    assert 'UNRELATED_CHAT_HISTORY' not in payloads(events)
    traced = [event for event in events if event['kind'].startswith('tool_')]
    assert traced and all(event['tool'] == 'read_document' for event in traced)


def test_late_old_turn_event_cannot_inherit_new_turn_research(workspace):
    run = completed_run()
    chat = store.new_conversation(run['id'])
    store.update_conversation(chat['id'], lambda value: value.update(turn_id='new-turn'))
    with execution_audit.scope(conversation_id=chat['id'], turn_id='old-turn'):
        execution_audit.audit('tool_result', result='OLD_TURN_PRIVATE_OUTPUT')
    selected = payloads(execution_audit.events_for_run(run))
    assert 'OLD_TURN_PRIVATE_OUTPUT' not in selected


def test_legacy_log_is_filtered_and_redacted_without_rewriting_history(workspace):
    original = [
        {'time': '2026-10-01T00:00:00Z', 'kind': 'run_created', 'run_id': 'run-a',
         'conversation_id': 'chat-a', 'turn_id': 'turn-a'},
        {'time': '2026-10-01T00:00:01Z', 'kind': 'model_response', 'run_id': 'run-a',
         'result': 'OWN_LEGACY_RESULT', 'api_key': 'LEGACY_PRIVATE_CREDENTIAL'},
        {'time': '2026-10-01T00:00:02Z', 'kind': 'model_request', 'run_id': 'run-other',
         'conversation_id': 'chat-a', 'turn_id': 'turn-other', 'detail': 'OTHER_LEGACY_MESSAGE'},
        {'time': '2026-10-01T00:00:03Z', 'kind': 'model_request',
         'conversation_id': 'chat-a', 'detail': 'UNKNOWN_TURN_PRIVATE_MESSAGE'},
    ]
    legacy = workspace / 'competition-audit.sqlite3'
    with sqlite3.connect(legacy) as connection:
        connection.execute('CREATE TABLE audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)')
        connection.executemany('INSERT INTO audit(payload) VALUES (?)',
                               [(json.dumps(event),) for event in original])
    before = legacy.read_bytes()

    selected = payloads(execution_audit.events_for_run({'id': 'run-a'}))
    assert 'OWN_LEGACY_RESULT' in selected
    assert '[REDACTED]' in selected
    for private in ('LEGACY_PRIVATE_CREDENTIAL', 'OTHER_LEGACY_MESSAGE', 'UNKNOWN_TURN_PRIVATE_MESSAGE'):
        assert private not in selected
    assert legacy.read_bytes() == before


def test_nested_credentials_are_removed_but_usage_numbers_remain(workspace):
    execution_audit.audit('model_response', run_id='run-a',
        headers={'Authorization': 'Bearer HEADER_PRIVATE_CREDENTIAL'},
        credentials={'api_key': 'NESTED_PRIVATE_CREDENTIAL', 'tushare_token': 'DATA_PRIVATE_CREDENTIAL'},
        detail='https://example.test/data?api_key=URL_PRIVATE_CREDENTIAL&year=2025',
        usage={'prompt_tokens': 120, 'completion_tokens': 34},
        token='OPAQUE_PRIVATE_CREDENTIAL')
    selected = execution_audit.events_for_run({'id': 'run-a'})
    text = payloads(selected)
    assert selected[0]['usage'] == {'prompt_tokens': 120, 'completion_tokens': 34}
    for private in ('HEADER_PRIVATE_CREDENTIAL', 'NESTED_PRIVATE_CREDENTIAL',
                    'DATA_PRIVATE_CREDENTIAL', 'URL_PRIVATE_CREDENTIAL', 'OPAQUE_PRIVATE_CREDENTIAL'):
        assert private not in text


def test_zip_contains_only_selected_research_materials_and_execution_content(workspace):
    own_bytes = json.dumps([{'label': 'OWN_SELECTED_MATERIAL'}]).encode()
    other_bytes = json.dumps([{'label': 'UNSELECTED_PRIVATE_MATERIAL'}]).encode()
    own_material = competition.register_material(own_bytes, 'selected.json', '600001.SH', 2025)
    competition.register_material(other_bytes, 'unselected.json', '600002.SH', 2025)
    own = completed_run([own_material['id']])
    other = completed_run()
    with execution_audit.scope(run_id=own['id']):
        execution_audit.audit('tool_result', result='OWN_RESEARCH_RESULT', api_key='ZIP_PRIVATE_CREDENTIAL')
    with execution_audit.scope(run_id=other['id']):
        execution_audit.audit('model_response', result='OTHER_RESEARCH_PRIVATE_MESSAGE')
    (workspace / 'connections.json').write_text('CONNECTION_STORE_PRIVATE_CREDENTIAL')

    with zipfile.ZipFile(io.BytesIO(competition.submission_bundle(own))) as archive:
        assert archive.read(f"materials/{own_material['id']}.json") == own_bytes
        events = json.loads(archive.read('execution/audit.json'))
        assert 'OWN_RESEARCH_RESULT' in payloads(events)
        assert 'OWN_SELECTED_MATERIAL' in b'\n'.join(archive.read(name) for name in archive.namelist()).decode()
        content = b'\n'.join(archive.read(name) for name in archive.namelist())
        for private in ('OTHER_RESEARCH_PRIVATE_MESSAGE', 'UNSELECTED_PRIVATE_MATERIAL',
                        'ZIP_PRIVATE_CREDENTIAL', 'CONNECTION_STORE_PRIVATE_CREDENTIAL'):
            assert private.encode() not in content
        assert archive.read('competition/audit.json') == archive.read('execution/audit.json')


def test_own_model_request_does_not_reexport_other_research_chat_history(workspace):
    run = completed_run()
    execution_audit.audit('model_request', run_id=run['id'], model='test-model',
        payload={'original_request': 'CURRENT_RESEARCH_REQUEST',
                 'conversation': [{'role': 'user', 'content': 'OTHER_RUN_PRIVATE_CHAT'},
                                  {'role': 'assistant', 'content': 'OTHER_RUN_PRIVATE_ANSWER'}],
                 'available_documents': [{'id': 'other-document', 'title': 'OTHER_COMPANY_PRIVATE_DOCUMENT'}]})

    with zipfile.ZipFile(io.BytesIO(competition.submission_bundle(run))) as archive:
        content = b'\n'.join(archive.read(name) for name in archive.namelist())
        assert b'test-model' in archive.read('execution/audit.json')
        for private in ('OTHER_RUN_PRIVATE_CHAT', 'OTHER_RUN_PRIVATE_ANSWER', 'OTHER_COMPANY_PRIVATE_DOCUMENT'):
            assert private.encode() not in content
    with sqlite3.connect(workspace / 'execution-audit.sqlite3') as connection:
        local = '\n'.join(row[0] for row in connection.execute('SELECT payload FROM audit'))
    assert 'CURRENT_RESEARCH_REQUEST' in local
