import copy
import pytest

from app import agent, store


SOURCE = {'id': 'half-cash', 'kind': 'pdf', 'title': '示例公司2026年半年度报告',
          'url': 'https://example.com/half.pdf#page=7', 'page': 7,
          'published_at': '2026-07-25', 'read_status': 'pdf_text',
          'text': '示例公司2026年半年度报告\n本报告期 上年同期 同比增减\n'
                  '经营活动产生的现金流量净额（千元） 60,216,851 31,218,976 92.89%'}
BAD = '2026年上半年经营现金流60.22亿元。'
GOOD = '2026年上半年经营现金流602.17亿元。'


def research_run():
    return {'request': {'as_of': '2026-10-16', 'year': 2025}, 'facts': [], 'evidence': [],
            'research_sources': [copy.deepcopy(SOURCE)], 'report_revision': 3,
            'memo': {'sources': [copy.deepcopy(SOURCE)]}}


def test_final_response_amount_guard_only_checks_observed_or_cited_sources():
    run = research_run()
    assert not agent.response_amount_feedback(BAD, run, [])
    assert '602.17亿元' in agent.response_amount_feedback(BAD + ' [half-cash]', run, [])
    rows = [{'tool': 'read_public_pdf', 'result': {'results': [SOURCE]}}]
    assert '602.17亿元' in agent.response_amount_feedback(BAD, run, rows)
    assert not agent.response_amount_feedback(GOOD, run, rows)
    assert not agent.response_amount_feedback('假设2026年上半年经营现金流60.22亿元。', run, rows)
    assert not agent.response_amount_feedback(BAD, run, [{'tool': 'web_search', 'result': {'results': [SOURCE]}}])
    assert not agent.response_amount_feedback(BAD, run, [{'tool': 'read_public_pdf', 'result': {'error': '读取失败', 'results': [SOURCE]}}])
    assert '602.17亿元' in agent.response_amount_feedback(BAD, run,
        [{'tool': 'refine_investment_memo', 'result': {'artifact': 'report', 'revision': 3}}])
    assert not agent.response_amount_feedback(BAD, run,
        [{'tool': 'refine_investment_memo', 'result': {'artifact': 'report', 'revision': 2}}])


@pytest.mark.parametrize('corrects', [True, False])
def test_final_response_correction_uses_existing_evidence_without_new_tools(tmp_path, monkeypatch, corrects):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    run = store.create_run({'ticker': '300750.SZ', 'year': 2025, 'as_of': '2026-10-16',
                            'mode': 'sample', 'question': '经营现金流'})
    store.update_run(run['id'], lambda r: r.update(research_run()))
    chat = store.new_conversation(run['id'])
    chat = store.begin_turn(chat['id'], '简要说明已读半年报经营现金流，不执行工具。', 'stub', 'documents')
    seen = []
    def model(system, payload, *args, **kwargs):
        seen.append(copy.deepcopy(payload))
        return {'action': 'respond', 'message': (GOOD if corrects and len(seen) > 1 else BAD) + ' [half-cash]'}, {'model': 'stub'}
    monkeypatch.setattr(agent, 'model_json', model)
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    assert len(seen) == 2
    assert any('最终答复的金额单位换算未通过' in r.get('validation_feedback', '') for r in seen[-1]['tool_results'])
    if corrects:
        assert GOOD in saved['messages'][-1]['content'] and BAD not in saved['messages'][-1]['content']
    else:
        assert '原答复未采用' in saved['messages'][-1]['content']
        assert saved['messages'][-1]['incomplete']
    assert saved['state'] == 'idle' and not saved['steps']


@pytest.mark.parametrize('corrects', [True, False])
def test_period_amount_summary_repairs_or_falls_back_without_repeating_analysis(tmp_path, monkeypatch, corrects):
    from test_financial_period_answers import results as period_results
    monkeypatch.setattr(store, 'DATA', tmp_path)
    run = store.create_run({'ticker': '600887.SH', 'year': 2025, 'as_of': '2026-10-17', 'mode': 'sample'})
    snapshot = period_results()[0]['result']
    snapshot['run_id'] = run['id']
    chat = store.new_conversation(run['id'])
    chat = store.begin_turn(chat['id'], '分析财务期间并说明2025和2024营业收入，不修改报告，不联网。', 'stub', 'documents')
    calls, tools = [], []
    bad = '营业收入：2025年1145.45亿元，2024年1143.02亿元。'
    good = '营业收入：2025年1156.36亿元，2024年1153.93亿元。'
    def model(system, payload, *args, **kwargs):
        calls.append(copy.deepcopy(payload))
        if len(calls) == 1:
            return {'action': 'tool', 'tool': 'analyze_financial_periods', 'arguments': {}}, {'model': 'stub'}
        return {'action': 'respond', 'message': good if corrects and len(calls) > 2 else bad}, {'model': 'stub'}
    def tool(chat, name, args, *rest):
        tools.append(name)
        return copy.deepcopy(snapshot)
    monkeypatch.setattr(agent, 'model_json', model)
    monkeypatch.setattr(agent, 'tool', tool)
    agent.execute(chat['id'])
    saved = store.get_conversation(chat['id'])
    message = saved['messages'][-1]['content']
    assert tools == ['analyze_financial_periods'] and len(calls) == 3
    assert '1145.45' not in message and '1143.02' not in message
    assert '1156.36' in message or '115636231250.05' in message
    if not corrects:
        assert '原总结未采用' in message and 'fact-ledger-revenue-2025' in message
    assert saved['state'] == 'idle'
