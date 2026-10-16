"""Recovery must not mix drafts between users, tasks, or published versions."""
from copy import deepcopy

import pytest

from app.research_draft import select_working_draft


@pytest.fixture
def scenario():
    attempt = {'id': '2026-10-16T10:01:00+00:00', 'question': '继续保存深度报告',
               'draft': {'title': '初稿'}, 'repaired_draft': {'title': '待修复稿'},
               'review': {'issues': ['现金口径待核实']}}
    run = {'id': 'run', 'report_revision': 1, 'memo': {'title': '已发布报告'},
           'deep_research_attempts': [attempt]}
    chat = {'id': 'chat', 'run_id': 'run', 'turn_id': 'next',
            'turn_contexts': {
                'old': {'run_id': 'run', 'task_turn_id': 'old', 'original_request': '继续保存深度报告'},
                'next': {'run_id': 'run', 'task_turn_id': 'old', 'task_turn_ids': ['old', 'next'],
                         'resume_from_turn_id': 'old'}},
            'steps': [{'id': 'report-step', 'turn_id': 'old', 'tool': 'refine_investment_memo',
                       'state': 'failed', 'started_at': '2026-10-16T10:00:00+00:00',
                       'finished_at': '2026-10-16T10:03:00+00:00',
                       'arguments': {'question': '模型改写过的请求'},
                       'result': {'error': '引用摘录未匹配'}}]}
    return run, chat


def test_legacy_recovery_requires_failed_step_time_and_exact_task_question(scenario):
    run, chat = scenario
    before = deepcopy(scenario)
    selected = select_working_draft(run, chat)
    assert selected['working_draft']['title'] == '待修复稿'
    assert selected['stage'] == 'repaired_draft' and selected['unpublished'] is True
    assert selected['review']['issues'] == ['现金口径待核实']
    assert selected['validation_feedback'] == '引用摘录未匹配'
    selected['working_draft']['title'] = '只修改返回副本'
    selected['review']['issues'].clear()
    assert scenario == before


@pytest.mark.parametrize('field,value', [('run_id', 'other-run'), ('chat_id', 'other-chat'),
                                       ('turn_id', 'other-turn'), ('task_turn_id', 'other-task'),
                                       ('step_id', 'other-step'), ('saved_report_revision', 2)])
def test_conflicting_ownership_or_saved_attempt_never_falls_back_to_time(scenario, field, value):
    run, chat = scenario
    run['deep_research_attempts'][0][field] = value
    assert select_working_draft(run, chat) is None


@pytest.mark.parametrize('change', ['wrong-question', 'outside-time', 'missing-end', 'missing-timezone',
                                   'running', 'ambiguous', 'different-run', 'ordinary-new-task'])
def test_legacy_ambiguity_is_not_guessed(scenario, change):
    run, chat = scenario
    attempt, step = run['deep_research_attempts'][0], chat['steps'][0]
    if change == 'wrong-question': attempt['question'] = '其他研究'
    elif change == 'outside-time': attempt['id'] = '2026-10-16T10:04:00+00:00'
    elif change == 'missing-end': step.pop('finished_at')
    elif change == 'missing-timezone': attempt['id'] = '2026-10-16T10:01:00'
    elif change == 'running': step['state'] = 'running'
    elif change == 'ambiguous': chat['steps'].append(dict(step, id='another-step'))
    elif change == 'different-run': chat['run_id'] = 'other'
    elif change == 'ordinary-new-task': chat['turn_contexts']['next'].pop('resume_from_turn_id')
    assert select_working_draft(run, chat) is None


def test_explicit_step_link_can_recover_legacy_attempt_without_question_match(scenario):
    run, chat = scenario
    run['deep_research_attempts'][0].update(step_id='report-step', question='工具改写')
    assert select_working_draft(run, chat)['step_id'] == 'report-step'


def test_latest_failed_draft_and_same_task_review_are_preserved(scenario):
    run, chat = scenario
    run['deep_research_attempts'].append({'id': '2026-10-16T10:05:00+00:00',
        'run_id': 'run', 'chat_id': 'chat', 'turn_id': 'next', 'task_turn_id': 'old',
        'step_id': 'retry-step', 'draft': {'title': '最近未保存稿'}, 'question': '具体修订'})
    chat['steps'].append({'id': 'retry-step', 'turn_id': 'next', 'tool': 'refine_investment_memo',
        'state': 'failed', 'started_at': '2026-10-16T10:04:00+00:00',
        'finished_at': '2026-10-16T10:06:00+00:00'})
    selected = select_working_draft(run, chat)
    assert selected['stage'] == 'draft'
    assert selected['working_draft']['title'] == '最近未保存稿'
    assert selected['review']['issues'] == ['现金口径待核实']


def test_completed_report_supersedes_older_unpublished_work(scenario):
    run, chat = scenario
    chat['steps'].append({'id': 'saved', 'turn_id': 'old', 'tool': 'refine_investment_memo',
        'state': 'completed', 'finished_at': '2026-10-16T10:04:00+00:00',
        'result': {'artifact': 'report', 'revision': 2}})
    assert select_working_draft(run, chat) is None


def test_completed_at_does_not_prove_persistence(scenario):
    run, chat = scenario
    run['deep_research_attempts'][0]['completed_at'] = '2026-10-16T10:02:00+00:00'
    assert select_working_draft(run, chat) is not None


def test_empty_repair_falls_back_to_draft_but_empty_attempt_is_ignored(scenario):
    run, chat = scenario
    run['deep_research_attempts'][0]['repaired_draft'] = {}
    assert select_working_draft(run, chat)['stage'] == 'draft'
    run['deep_research_attempts'][0]['draft'] = {}
    assert select_working_draft(run, chat) is None


def test_other_chat_newer_published_revision_rejects_stale_owned_draft(scenario):
    run, chat = scenario
    run['deep_research_attempts'][0].update(base_report_revision=1, chat_id='chat', run_id='run')
    assert select_working_draft(run, chat) is not None
    run['report_revision'] = 2
    assert select_working_draft(run, chat) is None


def test_other_chat_memo_timestamp_rejects_stale_legacy_draft(scenario):
    run, chat = scenario
    run['memo_updated_at'] = '2026-10-16T10:00:30+00:00'
    assert select_working_draft(run, chat) is not None
    run['memo_updated_at'] = '2026-10-16T10:02:00+00:00'
    assert select_working_draft(run, chat) is None
