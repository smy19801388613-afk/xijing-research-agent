"""Recovery must not mix drafts between users, tasks, or published versions."""
from copy import deepcopy

import pytest

from app.research_draft import select_working_draft, select_recoverable_review


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


@pytest.fixture
def archived_review(scenario):
    run, chat = scenario
    run.update(report_revision=4, memo_updated_at='2026-10-16T11:00:00+00:00')
    run['memo'] = {'title': '最新正式稿', 'review': {'issues': []}}
    attempt = run['deep_research_attempts'][0]
    attempt.update(run_id='run', chat_id='chat', turn_id='old', task_turn_id='old',
                   step_id='report-step', base_report_revision=1,
                   review={'issues': ['历史口径疑问', '历史现金流疑问']})
    chat['steps'][0]['tool'] = 'build_investment_memo'
    chat['turn_contexts']['next'] = {'run_id': 'run', 'task_turn_id': 'next', 'task_turn_ids': ['next']}
    chat['messages'] = [{'role': 'user', 'turn_id': 'next',
                         'content': '只修改标题，并恢复此前实际反方审阅意见到审阅区；其余正文和投资建议保持不变。'}]
    return run, chat


def test_explicit_historical_review_recovery_never_reactivates_superseded_draft(archived_review):
    run, chat = archived_review
    before = deepcopy(archived_review)
    assert select_working_draft(run, chat) is None
    review = select_recoverable_review(run, chat)
    assert [i['text'] for i in review['issues']] == ['历史口径疑问', '历史现金流疑问']
    assert all(i['status'] == 'needs_review' and i['first_seen_revision'] == 5 for i in review['issues'])
    assert all(i['origin_attempt_id'] == run['deep_research_attempts'][0]['id'] for i in review['issues'])
    assert review['recovered_from_unpublished_attempts'][0]['base_report_revision'] == 1
    assert review['independent_review_performed'] is False and review['reviewed_revision'] is None
    review['issues'].clear()
    assert archived_review == before


@pytest.mark.parametrize('text', ['只修改标题。', '不要恢复此前反方审阅意见，只修改标题。',
                                '无需保留未发布草稿的反方意见。', '不需要复用未发布的最新草稿。'])
def test_ordinary_or_negative_user_turn_cannot_import_archive(archived_review, text):
    run, chat = archived_review
    chat['messages'][0]['content'] = text
    chat['messages'].append({'role': 'assistant', 'turn_id': 'next', 'content': '恢复此前实际反方审阅意见'})
    assert select_recoverable_review(run, chat) is None


@pytest.mark.parametrize('text', ['复用未发布的最新深度研究草稿，保存完整报告。保留既有反方审阅与来源。',
                                '复用未发布的最新深度研究草稿并保存完整报告。'])
def test_explicit_unpublished_draft_request_can_retain_its_review_without_loading_old_text(archived_review, text):
    run, chat = archived_review
    chat['messages'][0]['content'] = text
    assert select_working_draft(run, chat) is None
    assert len(select_recoverable_review(run, chat)['issues']) == 2


@pytest.mark.parametrize('field,value', [('run_id', 'other'), ('chat_id', 'other'), ('turn_id', 'other'),
                                       ('step_id', 'other'), ('saved_report_revision', 2),
                                       ('mode', 'source_refinement'), ('task_turn_id', 'other')])
def test_review_recovery_requires_strong_consistent_audit_ownership(archived_review, field, value):
    run, chat = archived_review
    run['deep_research_attempts'][0][field] = value
    assert select_recoverable_review(run, chat) is None


@pytest.mark.parametrize('change', ['missing-id', 'missing-owner', 'running', 'missing-end', 'outside-time', 'wrong-tool', 'duplicate-step'])
def test_incomplete_or_ambiguous_review_audit_is_not_guessed(archived_review, change):
    run, chat = archived_review
    attempt, step = run['deep_research_attempts'][0], chat['steps'][0]
    if change == 'missing-id': attempt.pop('id')
    elif change == 'missing-owner': attempt.pop('chat_id')
    elif change == 'running': step['state'] = 'running'
    elif change == 'missing-end': step.pop('finished_at')
    elif change == 'outside-time': step['finished_at'] = '2026-10-16T10:00:05+00:00'
    elif change == 'wrong-tool': step['tool'] = 'refine_investment_memo'
    elif change == 'duplicate-step': chat['steps'].append(deepcopy(step))
    assert select_recoverable_review(run, chat) is None


def test_review_merge_keeps_published_issues_and_origin_without_duplicate_ids(archived_review):
    from app import review_state
    run, chat = archived_review
    recovered = select_recoverable_review(run, chat)
    prior = {'issues': ['已发表的另一意见']}
    merged = review_state.combined(prior, recovered)
    again = review_state.combined(merged, recovered)
    assert len(again['issues']) == 3
    assert len(again['recovered_from_unpublished_attempts']) == 1
    assert all(i['status'] == 'needs_review' for i in review_state.after_refinement(again, 4)['issues'])
