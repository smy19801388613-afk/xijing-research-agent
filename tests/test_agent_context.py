from copy import deepcopy
import json

from app import agent_context as ctx


def sample_run():
    question = {'id': 'q-capex', 'question': '资本开支是否挤压可分配现金流？',
                'completion_criteria': '核对资本开支和经营现金流口径', 'status': 'pending',
                'answer': '', 'bindings': [], 'gaps': ['缺少现金流桥接'],
                'next_action': '读取年报第120页，并核对现金流表单位', 'priority': 'high',
                'history': [{'answer': '旧答案' * 10000}] * 10}
    return {'id': 'run1', 'request': {'ticker': '601899.SH', 'year': 2025, 'as_of': '2026-10-15', 'question': '研究现金流'},
            'company': '紫金矿业', 'state': 'completed', 'report_revision': 2,
            'facts': [{'id': 'f1', 'metric': 'revenue', 'label': '营业收入', 'value': '100',
                       'year': 2025, 'status': 'api_only', 'evidence_ids': []}],
            'research_questions': [question], 'evidence': [], 'calculations': [],
            'research_sources': [{'id': 'pdf120', 'kind': 'document', 'document_id': 'annual',
                 'url': '/api/documents/annual/file#page=120', 'page': 120, 'title': '年度报告',
                 'text': '单位：人民币元。经营活动产生的现金流量。' * 1000,
                 'published_at': '2026-03-21', 'read_status': 'pdf_text'}],
            'memo': {'schema_version': 2, 'title': '已有报告', 'summary': '仍需核对现金流',
                     'sections': [{'title': '现金流', 'body': '旧报告长正文' * 20000, 'evidence_ids': ['pdf120']}],
                     'question_coverage': {'items': [question]},
                     'question_snapshot': {'questions': {'q-capex': {'history': '旧数据' * 5000}}}}}


def test_research_view_preserves_current_question_source_and_fact_without_history():
    run = sample_run()
    original = deepcopy(run)
    view = ctx.bounded_context({'run_id': 'run1'}, run)
    encoded = json.dumps(view, ensure_ascii=False)
    assert run == original
    assert ctx.size(view) <= 18000
    assert view['report_revision'] == 2
    assert view['facts'][0]['verification']['state'] == 'api_single'
    question = view['question_coverage']['items'][0]
    assert question['question'] == run['research_questions'][0]['question']
    assert question['next_action'] == run['research_questions'][0]['next_action']
    assert 'history' not in encoded and 'question_snapshot' not in encoded
    source = next(s for s in view['research_sources'] if s['id'] == 'pdf120')
    assert source['page'] == 120 and source['read_status'] == 'pdf_text'
    assert source['text'] in run['research_sources'][0]['text']
    assert source['excerpt_only'] is True


def test_duplicate_pdf_results_are_single_observation_without_losing_recent_feedback():
    reading = {'tool': 'read_document', 'result': {'artifact': 'document_search', 'snippets': [
        {'document_id': 'annual', 'page': 120, 'url': '/api/documents/annual/file#page=120',
         'text': '单位：人民币元。' * 800}]}}
    results = [deepcopy(reading) for _ in range(28)]
    results += [{'validation_feedback': '不要再次读取第120页，请更新问题答案。'}]
    original = deepcopy(results)
    small = ctx.compact_tool_results(results)
    assert results == original
    assert ctx.size(small) < 10000
    assert len(small) == 2
    assert small[-1]['validation_feedback'] == results[-1]['validation_feedback']
    assert small[0]['repeated_observations'] == 28
    assert small[0]['result']['snippets'][0]['page'] == 120
    assert small[0]['result']['snippets'][0]['text'] == reading['result']['snippets'][0]['text']


def test_artifacts_errors_proposal_and_attachment_remain_addressable():
    rows = [{'tool': 'refine_investment_memo', 'result': {'artifact': 'report', 'revision': 3, 'run_id': 'r'}},
            {'tool': 'read_webpage', 'result': {'error': '原文无法访问'}},
            {'tool': 'propose_valuation', 'result': {'artifact': 'proposal', 'proposal': {'id': 'p1', 'method': 'pe', 'assumptions': {'multiple': 10}}}},
            {'tool': 'attach_document', 'result': {'artifact': 'document', 'document': {'id': 'doc1', 'title': '年报'}, 'page_count': 200}}]
    small = ctx.compact_tool_results(rows)
    by_tool = {r['tool']: r['result'] for r in small}
    assert by_tool['refine_investment_memo']['revision'] == 3
    assert by_tool['read_webpage']['error'] == '原文无法访问'
    assert by_tool['propose_valuation']['proposal']['id'] == 'p1'
    assert by_tool['attach_document']['document']['id'] == 'doc1'


def test_full_projection_keeps_latest_human_request_and_policy():
    run = sample_run()
    messages = [{'role': 'user', 'content': '开展研究，不要修改估值参数。'}]
    messages += [{'role': 'assistant', 'content': '过去回答' * 9000} for _ in range(30)]
    messages += [{'role': 'user', 'content': '继续现金流部分，禁止联网。'}]
    payload = {'context': ctx.bounded_context({'run_id': 'run1'}, run), 'conversation': messages,
        'original_request': '开展研究，不要修改估值参数。',
        'user_constraints': {'no_network': True, 'no_calculation': True},
        'available_tools': ['read_document', 'update_research_question'],
        'research_plan': {'pending_questions': run['research_questions'], 'pending_obligations': ['question_progress']},
        'tool_results': [{'tool': 'read_document', 'result': {'artifact': 'document_search',
             'snippets': run['research_sources']}}] * 30}
    original = deepcopy(payload)
    compact = ctx.compact_payload(payload)
    assert payload == original
    assert ctx.size(compact) <= 35000
    assert compact['original_request'] == payload['original_request']
    assert compact['user_constraints'] == payload['user_constraints']
    assert compact['available_tools'] == payload['available_tools']
    assert any(m['content'] == messages[-1]['content'] for m in compact['conversation'])
    assert compact['research_plan']['pending_questions'][0]['id'] == 'q-capex'
    assert 'history' not in json.dumps(compact, ensure_ascii=False)


def test_sources_deduplicate_same_page_but_keep_distinct_pages_and_dates():
    rows = [{'id': 's1', 'url': 'https://example.com/a.pdf#page=1', 'page': 1, 'text': 'A' * 600,
             'read_status': 'pdf_text', 'published_at': '2026-01-01'},
            {'id': 's2', 'url': 'https://example.com/a.pdf#page=2', 'page': 2, 'text': 'B' * 600,
             'read_status': 'windows_ocr', 'published_at': None}]
    small = ctx.compact_sources(rows + [rows[0]])
    assert len(small) == 2
    assert {r['page'] for r in small} == {1, 2}
    assert next(r for r in small if r['id'] == 's2')['read_status'] == 'windows_ocr'
    assert next(r for r in small if r['id'] == 's2')['published_at'] is None


def test_latest_pdf_pages_keep_headers_and_tail_even_when_budget_is_small():
    pages = [{'document_id': 'annual', 'page': p, 'url': f'/api/documents/annual/file#page={p}',
              'text': '单位：万元。' + '表格中间行。' * 1600 + f'第{p}页末：现金流差额为负数。'} for p in (120, 121, 122)]
    payload = {'context': {'memo': {'summary': '旧报告' * 20000}}, 'conversation': [],
               'tool_results': [{'tool': 'refine_investment_memo', 'result': {'artifact': 'report', 'revision': 3}},
                                {'tool': 'web_search', 'result': {'error': '本次检索服务不可用'}},
                                {'tool': 'read_document', 'result': {'artifact': 'document_search', 'snippets': pages}},
                                {'validation_feedback': '下一步更新问题答案，不重复读取。'}]}
    small = ctx.compact_payload(payload, max_chars=10000)
    read = next(r for r in small['tool_results'] if r.get('tool') == 'read_document')
    assert [s['text'] for s in read['result']['snippets']] == [s['text'] for s in pages]
    assert small['tool_results'][-1]['validation_feedback'].startswith('下一步更新')
    assert next(r for r in small['tool_results'] if r.get('tool') == 'refine_investment_memo')['result']['revision'] == 3
    assert next(r for r in small['tool_results'] if r.get('tool') == 'web_search')['result']['error'] == '本次检索服务不可用'


def test_latest_user_message_and_all_pending_question_texts_survive_small_target():
    latest = '保留用户最新研究限定。' * 800
    result = ctx.compact_conversation([{'role': 'assistant', 'content': '旧报告' * 5000},
                                      {'role': 'user', 'content': latest}], max_chars=500)
    assert result[-1]['content'] == latest
    run = sample_run()
    run['research_questions'] = [dict(run['research_questions'][0], id=f'q{i}',
                                    question=f'第{i}个问题原文：' + '要求分析现金流。' * 20) for i in range(30)]
    view = ctx.bounded_context({'run_id': 'run1'}, run, max_chars=8000)
    assert {q['id']: q['question'] for q in view['question_coverage']['items']} == {
        q['id']: q['question'] for q in run['research_questions']}


def test_unreadable_latest_page_does_not_displace_last_full_body():
    body = '完整表格。' * 1600 + '页尾：仍需核对现金流。'
    rows = [
        {'tool': 'read_document', 'result': {'snippets': [{'document_id': 'annual', 'page': 120, 'text': body}]}},
        {'tool': 'read_public_pdf', 'result': {'status': 'needs_ocr', 'results': [], 'url': 'https://example.com/scan.pdf'}},
    ]
    compact = ctx.compact_tool_results(rows, max_chars=500)
    prior = next(r for r in compact if r.get('tool') == 'read_document')
    assert prior['full_read_observation']
    assert prior['result']['snippets'][0]['text'] == body


def test_long_source_locators_are_exact_in_context_and_tool_results():
    source = {'id': 'source-' + 'a' * 450, 'document_id': 'document-' + 'b' * 450,
              'url': 'https://example.com/report.pdf?signature=' + 'c' * 700 + '#page=120',
              'source_url': 'https://example.com/archive/' + 'd' * 700,
              'locator': '披露原文定位：' + 'e' * 450, 'page': 120, 'text': '原文'}
    sources = ctx.compact_sources([source], limit=5000)
    result = ctx.compact_tool_results([{'tool': 'read_document', 'result': {'snippets': [source]}}])
    for projected in (sources[0], result[0]['result']['snippets'][0]):
        for field in ('id', 'document_id', 'url', 'source_url', 'locator'):
            assert projected[field] == source[field]
