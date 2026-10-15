"""Multi-goal PDF work must not finish after an arbitrary single page."""
import copy

import pytest

from app import agent, resume_plan, store


REQUEST = ('请逐页查阅已接入的2025年年报，核对营业收入、归母净利润、经营现金流的2025与2024年数据、'
           '同比、元与亿元、合并口径及PDF物理页码。本轮只核对原文，不联网、不估值、不修改研究报告。')
ROWS = {
    6: '2025年 2024年 合并口径 单位：元\n营业收入 168838102514.79 170899152276.34 -1.21%',
    60: '2025年 2024年 合并利润表 单位：元\n归属于上市公司股东的净利润 82320067101.68 86228146421.62 -4.53%',
    64: '2025年 2024年 合并现金流量表 单位：元\n经营活动产生的现金流量净额 61522204989.35 92463692168.43 -33.46%',
    54: '2025年审计报告：营业收入为16883810.25万元。主营业务收入16877458.52万元，占99.96%。',
}


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.put_document({'id': 'annual', 'title': '甲公司2025年年报', 'ticker': '600001.SH', 'year': 2025, 'page_count': 150})
    current = store.new_conversation()
    store.update_conversation(current['id'], lambda c: c.update(document_ids=['annual']))
    return current


def snippet(page, text=None, document_id='annual'):
    return {'document_id': document_id, 'page': page, 'text': ROWS[page] if text is None else text,
            'url': f'/api/documents/{document_id}/file#page={page}'}


def begin(chat, text=REQUEST):
    return store.begin_turn(chat['id'], text, 'stub', 'documents')


def add_read(chat, page):
    store.update_conversation(chat['id'], lambda c: c['steps'].append({
        'id': f'read-{page}', 'turn_id': c['turn_id'], 'tool': 'read_document', 'state': 'completed',
        'arguments': {'document_id': 'annual', 'page': page, 'count': 1},
        'result': {'snippets': [snippet(page)]}}))


def test_three_metrics_continue_beyond_first_page_and_old_two_attempt_allowance(chat, monkeypatch):
    current = begin(chat)
    add_read(current, 54)
    seen = []
    actions = []
    def model(system, payload, *args, **kwargs):
        seen.append(copy.deepcopy(payload))
        assert payload['user_constraints']['no_network'] and payload['user_constraints']['no_report']
        incomplete = any(not row['read_candidate_available'] for row in payload['research_plan']['document_checks'])
        message = ('目前只读到营业收入一项；归母净利润、经营现金流还需要继续查阅。下一步我继续核对。'
                   if incomplete else '已逐项列出2025与2024合并口径的营业收入、归母净利润和经营现金流、同比、元/亿元与页码。')
        return {'action': 'respond', 'message': message}, {'model': 'stub'}
    def tool(c, name, args, *rest):
        actions.append((name, args))
        if name == 'search_documents': return {'snippets': [snippet(p) for p in (6, 60, 64)]}
        assert name == 'read_document'
        return {'snippets': [snippet(args['page'])]}
    monkeypatch.setattr(agent, 'model_json', model)
    monkeypatch.setattr(agent, 'tool', tool)
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert [a[0] for a in actions] == ['search_documents', 'read_document', 'read_document', 'read_document']
    assert [a[1]['page'] for a in actions[1:]] == [6, 60, 64]
    assert saved['state'] == 'idle' and saved['messages'][-1]['delivery']['status'] == 'completed'
    assert len(seen) == 5
    assert saved['messages'][-1]['coverage']['document_pages'] == 4


def test_single_audit_revenue_does_not_cover_comparative_requirements(chat):
    current = begin(chat)
    add_read(current, 54)
    checks = resume_plan.document_checks(store.get_conversation(chat['id']))
    assert len(checks) == 3 and not any(c['read_candidate_available'] for c in checks)
    assert 'document_targets' in resume_plan.plan(store.get_conversation(chat['id']))['pending_obligations']


def test_all_requested_rows_on_one_page_are_enough(chat, monkeypatch):
    current = begin(chat)
    store.update_conversation(chat['id'], lambda c: c['steps'].append({
        'id': 'summary', 'turn_id': current['turn_id'], 'tool': 'read_document', 'state': 'completed',
        'arguments': {'document_id': 'annual', 'page': 6},
        'result': {'snippets': [snippet(6, '\n'.join(ROWS[p] for p in (6, 60, 64)))]}}))
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: ({'action': 'respond', 'message': '已核对三项年度数据和同比，均见合并主要会计数据表。'}, {'model': 'stub'}))
    monkeypatch.setattr(agent, 'tool', lambda *a, **k: pytest.fail('already read targets should not be read again'))
    agent.execute(chat['id'])
    assert store.get_conversation(chat['id'])['messages'][-1]['delivery']['status'] == 'completed'


def test_genuinely_missing_material_returns_partial_without_retry_loop(chat, monkeypatch):
    begin(chat)
    actions = []
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: ({'action': 'respond', 'message': '已检索当前年报，没有命中所需三项指标；现有材料无法核对，保留缺口。'}, {'model': 'stub'}))
    def tool(c, name, args, *rest):
        actions.append(name)
        return {'snippets': [], 'document_count': 1, 'note': '全文没有匹配行'}
    monkeypatch.setattr(agent, 'tool', tool)
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert actions == ['search_documents']
    assert saved['state'] == 'idle' and len(saved['model_calls']) == 2
    assert saved['messages'][-1]['delivery']['can_resume']
    assert saved['messages'][-1]['delivery']['status'] == 'partial'


def test_foreign_unattached_document_is_not_selected_to_fill_missing_goals(chat):
    current = begin(chat)
    store.update_conversation(chat['id'], lambda c: c['steps'].append({
        'id': 'search', 'turn_id': current['turn_id'], 'tool': 'search_documents', 'state': 'completed',
        'arguments': {'queries': ['营业收入', '归属于上市公司股东的净利润', '经营活动产生的现金流量净额']},
        'result': {'snippets': [snippet(6, document_id='other-company')]}}))
    current = store.get_conversation(chat['id'])
    assert resume_plan.recovery(current, None, resume_plan.plan(current)) is None


def test_genuine_scope_clarification_is_not_forced_into_more_reading(chat, monkeypatch):
    begin(chat, '请逐页核对年报营业收入和经营现金流。')
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: ({'action': 'respond', 'message': '请确认您指合并口径还是母公司口径？'}, {'model': 'stub'}))
    # Ambiguity can legitimately require clarification before the first read.
    monkeypatch.setattr(agent, 'tool', lambda *a, **k: pytest.fail('clarification must wait for the user'))
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert saved['state'] == 'idle' and len(saved['model_calls']) == 1
    assert '请确认' in saved['messages'][-1]['content']


def test_false_background_promise_is_revised_without_tool_invention(chat, monkeypatch):
    begin(chat, '解释已读资料中的限制，不要使用任何工具。')
    answers = iter(['下一步我继续查阅原文。', '当前没有执行进一步查阅，本轮已结束，缺口保留。'])
    monkeypatch.setattr(agent, 'model_json', lambda *a, **k: ({'action': 'respond', 'message': next(answers)}, {'model': 'stub'}))
    monkeypatch.setattr(agent, 'tool', lambda *a, **k: pytest.fail('no-tools constraint must hold'))
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert len(saved['model_calls']) == 2 and '本轮已结束' in saved['messages'][-1]['content']


@pytest.mark.parametrize('prompt', ['怎么核对年报营业收入和归母净利润？', '营业收入和经营现金流有什么区别？'])
def test_method_questions_have_no_execution_checklist(chat, prompt):
    current = begin(chat, prompt)
    assert resume_plan.document_checks(current) == []
