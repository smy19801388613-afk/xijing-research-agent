import copy
from recommendation_fixture import watch_recommendation

import pytest

from app import deep_research as deep, report_evidence, store


def source(sid, body, page=1, url=None, **fields):
    return {'id': sid, 'kind': 'pdf', 'title': '2025年年度报告',
            'url': url or f'https://example.com/annual.pdf#page={page}',
            'text': body, 'page': page, 'published_at': '2026-04-17',
            'read_status': 'pdf_text', 'source_type': 'company_disclosure', **fields}


def crowded_run():
    return {'request': {'as_of': '2026-10-15', 'question': '核对合同负债'},
            'facts': [], 'calculations': [], 'research_questions': [],
            'research_sources': [source(f'noisy-{i}', '合并利润表 信用减值损失\n' + '其他已读内容' * 700, i)
                                 for i in range(1, 40)]}


def citations(*ids):
    return {'theses': [{'evidence_ids': list(ids)}], 'sections': []}


def test_repair_preserves_previously_cited_balance_and_observed_table_header():
    run = crowded_run()
    target = source('annual-p58', '年度报告\n合同负债 29 8,006,739,780.94 9,592,453,014.66', 58)
    header = source('annual-p57', '合并资产负债表\n单位：元 币种：人民币\n项目 2025年12月31日 2024年12月31日', 57)
    run['research_sources'].extend([target, header])
    original = copy.deepcopy(run)
    # Reproduce the original source-ranking loss before adding draft priority.
    assert target['id'] not in {s['id'] for s in deep.bounded_sources(deep.reference_catalog(run), 55000)}
    sources, extra = deep.generation_evidence(run, drafts=(citations(target['id']),))
    selected = {s['id']: s for s in sources + extra}
    for expected in (target, header):
        assert selected[expected['id']]['text'] == expected['text']
        for field in ('url', 'page', 'published_at', 'read_status', 'source_type'):
            assert selected[expected['id']][field] == expected[field]
    assert sum(len(s['text']) for s in sources + extra) <= 65000
    assert run == original


def test_refinement_recovers_source_lost_by_previous_repair_without_trusting_old_claim():
    run = crowded_run()
    run['research_sources'].append(source('balance', '合同负债原文，应保持原数值', 58))
    run['memo'] = citations('noisy-1')
    run['deep_research_attempts'] = [{'draft': citations('balance'), 'repaired_draft': citations('noisy-1')}]
    sources, _ = deep.generation_evidence(run)
    assert next(s for s in sources if s['id'] == 'balance')['text'] == '合同负债原文，应保持原数值'


def test_empty_failed_attempts_do_not_hide_last_available_draft_citations():
    run = crowded_run()
    run['research_sources'].append(source('balance', '合同负债原文，应保持原数值', 58))
    run['memo'] = citations('noisy-1')
    run['deep_research_attempts'] = [
        {'draft': citations('balance'), 'repaired_draft': citations('noisy-1')},
        {'failure': {'message': '仅返回推理，无最终正文'}}, {'calls': [], 'input_source_ids': []}]
    sources, _ = deep.generation_evidence(run)
    assert next(s for s in sources if s['id'] == 'balance')['text'] == '合同负债原文，应保持原数值'


def test_active_metric_recovers_read_rows_after_saved_report_already_lost_citation():
    run = crowded_run()
    run['request']['year'] = 2025
    row = '合同负债 29 8,006,739,780.94 9,592,453,014.66'
    note = '2025年年度报告\n29、 合同负债\n(1).合同负债情况\n单位：元 币种：人民币\n项目 期末余额 期初余额\n预收货款 8,006,739,780.94 9,592,453,014.66\n'
    run['research_sources'].extend([
        source('balance', '2025年年度报告\n' + row, 58),
        source('note', note, 102), source('note-duplicate', note, 102, url='https://example.com/public.pdf#page=102'),
        source('audit-only', '2025年年度报告\n结合合同负债函证确认销售额', 54),
        source('related-party', '合同负债 关联企业 61,053,517.70 10,247,940.71', 135),
        source('half-year', '2025年半年度报告\n合同负债 26 5,506,895,535.57 9,592,453,014.66', 25,
               title='2025年半年度报告'),
    ])
    run['research_questions'] = [{'question': '2025年末合同负债余额及同比变化如何？', 'status': 'partial'}]
    run['memo'] = citations('noisy-1')
    run['deep_research_attempts'] = [{'draft': citations('noisy-1')}]
    selected_hints = dict(report_evidence.metric_passages(run, deep.reference_catalog(run)))
    assert 'balance' in selected_hints and row in selected_hints['balance']
    assert len(selected_hints) == 2 and {'note', 'note-duplicate'} & selected_hints.keys()
    assert not {'audit-only', 'related-party', 'half-year'} & selected_hints.keys()
    sources, _ = deep.generation_evidence(run)
    assert next(s for s in sources if s['id'] == 'balance')['text'].endswith(row)


def test_archived_or_unrelated_questions_cannot_pin_metric_rows():
    run = crowded_run()
    run['research_sources'].append(source('balance', '合同负债 29 8,006,739,780.94 9,592,453,014.66', 58))
    run['research_questions'] = [{'question': '合同负债如何？', 'archived': True}, {'question': '分析行业竞争格局'}]
    assert report_evidence.metric_passages(run, deep.reference_catalog(run)) == []


def test_active_bound_quote_after_prefix_survives_with_metadata_and_literal_ranges():
    run = crowded_run()
    quote = '合同负债 29 8,006,739,780.94 9,592,453,014.66'
    text = '单位：元 币种：人民币\n2025年末 2024年末\n' + '背景介绍\n' * 1600 + quote + '\n其他附注' * 600
    run['research_sources'].append(source('long-balance', text, 58))
    run['research_questions'] = [{'bindings': [{'source_id': 'long-balance', 'quote': quote}]}]
    sources, _ = deep.generation_evidence(run)
    selected = next(s for s in sources if s['id'] == 'long-balance')
    assert quote in selected['text'] and '单位：元' in selected['text'] and '2025年末 2024年末' in selected['text']
    assert len(selected['text']) <= 3000 and selected['text_partial']
    assert selected['original_text_chars'] == len(text)
    assert all(text[r['start']:r['end']] in selected['text'] for r in selected['excerpt_ranges'])
    assert '不可跨段拼接引用' in selected['text']


def test_priority_cannot_reintroduce_future_sources_or_unobserved_quotes():
    run = crowded_run()
    run['research_sources'].append(source('future', '未来材料', 58, published_at='2026-10-16'))
    run['research_questions'] = [{'bindings': [{'source_id': 'noisy-1', 'quote': '伪造数字'}]}]
    sources, extra = deep.generation_evidence(run, drafts=(citations('future', 'invented'),))
    assert {'future', 'invented'}.isdisjoint(s['id'] for s in sources + extra)
    assert all('伪造数字' not in s['text'] for s in sources + extra)


def test_explicit_refinement_source_survives_old_citations_without_exceeding_budget():
    run=crowded_run()
    target=source('public-web-04a4345dec12','2026年半年度报告全文\n单位：千元\n'
        +'动力电池系统 192,124,890 152,492,111 20.63% 46.02%\n'
        +'储能电池系统 53,260,967 40,500,853 23.96% 87.54%\n'
        +'其他已查阅上下文\n'*130+'境外 87,129,188 61,018,360 29.97% 42.35%',16,
        title='公司 · 第16页')
    run['research_sources'].append(target)
    run['memo']=citations(*(s['id'] for s in run['research_sources'][:-1]))
    before=copy.deepcopy(run)
    sources,extra=deep.generation_evidence(run,focus='复用已读的第16页（public-web-04a4345dec12），补充动力/储能和境外数据。')
    assert sources[0]['id']==target['id'] and sources[0]['text']==target['text']
    assert sum(len(s.get('text','')) for s in sources+extra)<=65000
    assert any(s['id']=='noisy-1' for s in sources)
    assert run==before


def test_requested_source_id_must_be_exact_and_eligible():
    run=crowded_run()
    run['research_sources'].extend([
        source('half-page','已读半年度原文',58),
        source('future','未来材料',59,published_at='2026-10-16')])
    sources,extra=deep.generation_evidence(run,focus='future invented prefix-half-page-suffix')
    assert {'future','invented','half-page'}.isdisjoint(s['id'] for s in sources+extra)
    sources,extra=deep.generation_evidence(run,focus='来源half-page的已读原文')
    assert sources[0]['id']=='half-page'


def test_refine_passes_literal_requested_source_to_initial_and_repair_context(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    run=store.create_run({'ticker':'300750.SZ','year':2025,'as_of':'2026-10-15','question':'核对原文'})
    run.update(company='宁德时代',state='completed_with_gaps',research_sources=crowded_run()['research_sources'])
    run['research_sources'].append(source('half-page','2026年半年度报告\n储能电池系统 53,260,967',58))
    store.save_run(run)
    chat=store.new_conversation(run['id'])
    calls=[]
    def model(system,payload,*a,**kw):
        calls.append(copy.deepcopy(payload))
        assert payload['sources'][0]['id']=='half-page'
        assert '53,260,967' in payload['sources'][0]['text']
        return {'title':'修订稿'},{}
    def validate(*a,**kw):
        if len(calls)==1: raise ValueError('测试修复阶段仍应保留指定证据')
        return {'title':'修订稿'}
    monkeypatch.setattr(deep,'model_json',model)
    monkeypatch.setattr(deep,'validate_memo',validate)
    monkeypatch.setattr(deep,'save_memo',lambda *a,**kw:{'revision':2})
    assert deep.refine(run['id'],'复用来源half-page，修订报告',chat)['revision']==2
    assert len(calls)==2
    attempt=store.get_run(run['id'])['deep_research_attempts'][-1]
    assert 'half-page' in attempt['input_source_ids'] and 'half-page' in attempt['repair_input_source_ids']


def test_noncontiguous_quote_error_identifies_binding_and_bounded_preview():
    from app import questions
    run={'id':'quote-check','company':'宁德时代','request':{'year':2025,'as_of':'2026-10-16'},
         'facts':[],'calculations':[],'evidence':[],'research_sources':[
             source('p202','单位：元\n原文中间说明。\n经营现金流 100,000.00',202)]}
    q=questions.create('现金流量表的原文如何？');run['research_questions']=[q]
    item={'title':'事实与待查','body':'保留真实缺口','counter_evidence':'口径仍需复核','invalidate_if':'后续原文变化',
          'status':'inference','evidence_ids':['p202']}
    bad='单位：元 经营现金流 100,000.00'+'不应出现在预览尾部'*80
    memo={'title':'修订报告','summary':'等待核对','stance':'谨慎观察','theses':[item,copy.deepcopy(item)],
          'sections':[{'key':key,'body':'保留实际缺口','status':'inference','evidence_ids':['p202']} for key in deep.SECTION_TITLES],
          'question_answers':[{'question_id':q['id'],'answer':'待进一步核对','status':'partial',
            'bindings':[{'source_id':'p202','quote':'经营现金流 100,000.00','reason':'原始行值'},
                        {'source_id':'p202','quote':bad,'reason':'错误拼接'}]}]}
    with pytest.raises(ValueError,match=r'question_answers\[0\]\.bindings\[1\]\.quote') as error:
        deep.validate_memo(memo,run)
    message=str(error.value)
    assert '来源 p202' in message and '单位：元 经营现金流 100,000.00' in message
    assert bad not in message and len(message)<500
    memo['question_answers'][0]['bindings']=memo['question_answers'][0]['bindings'][:1]
    assert deep.validate_memo(memo,run)['question_answers'][0]['bindings'][0]['quote']=='经营现金流 100,000.00'


def test_many_cited_pages_share_budget_instead_of_losing_last_page():
    refs = {f'page-{i}': source(f'page-{i}', f'第{i}页原文' * 1000, i) for i in range(1, 61)}
    sources = deep.bounded_sources(refs, 55000, list(refs))
    assert {s['id'] for s in sources} == set(refs)
    assert all(s['text'] for s in sources)
    assert sum(len(s['text']) for s in sources) <= 55000


def test_many_prior_citations_leave_room_for_new_counter_evidence():
    refs = {f'page-{i}': source(f'page-{i}', f'第{i}页原文' * 1000, i) for i in range(1, 61)}
    prior = list(refs)
    refs['new-counter'] = source('new-counter', '更正前 更正后\n新披露反方证据', 1,
                                  url='https://example.com/correction.pdf#page=1')
    sources = deep.bounded_sources(refs, 55000, prior)
    assert {s['id'] for s in sources} == set(refs)
    assert next(s for s in sources if s['id'] == 'new-counter')['text'] == refs['new-counter']['text']


@pytest.mark.parametrize('draft', [
    {'sections': {'financial_quality': {'evidence_ids': ['balance']}}},
    {'theses': None, 'sections': None, 'question_answers': 1},
    {'theses': [{'evidence_ids': [{}, None]}], 'sections': ['bad']},
])
def test_invalid_draft_shapes_do_not_prevent_repair(draft):
    report_evidence.priorities({}, {'balance': source('balance', '已读原文')}, (draft,))


def test_build_repair_passes_current_draft_citations_after_new_sources_arrive(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(deep.competition, 'POLICY', {'enabled': False})
    run = store.create_run({'ticker': '600519.SH', 'year': 2025, 'as_of': '2026-10-15',
                            'mode': 'live', 'model_profile': 'stub', 'question': '核对原文'})
    run.update(company='贵州茅台', state='completed_with_gaps', research_sources=[source('balance', '已经读取的合同负债原文', 58)])
    store.save_run(run)
    monkeypatch.setattr(deep.documents, 'attached_documents', lambda _: [
        {'ticker': '600519.SH', 'year': 2025, 'title': '2025年年度报告', 'page_count': 143}])
    monkeypatch.setattr(deep.documents, 'ensure_index', lambda _: [])
    monkeypatch.setattr(deep.documents, 'search_attached', lambda *a: {'snippets': []})
    monkeypatch.setattr(deep.web_research, 'search', lambda qs, **kw: {'queries': qs, 'results': [], 'limitations': []})
    draft = {'title': '测试报告', 'summary': '仍需核对口径', 'stance': '继续核实',
             'theses': [{'title': label, 'body': '观察已读原文。', 'evidence_ids': ['balance'],
                         'counter_evidence': '仍需比较口径', 'invalidate_if': '原始披露出现变化', 'status': 'inference'}
                        for label in ('余额', '回款')],
             'sections': [{'key': key, 'body': '已读原文与推断分开。', 'evidence_ids': ['balance'], 'status': 'inference'}
                          for key in deep.SECTION_TITLES], 'questions': [], 'limitations': [],
             'tracking_metrics': [{'id': 'balance-tracking', 'name': '合同负债余额', 'definition': '合同负债同口径期末余额',
                 'thesis': '余额', 'baseline': {'status': 'missing', 'value': '尚缺同口径完整数值', 'period': ''},
                 'frequency': '定期披露后', 'next_check': '下一次定期报告披露后',
                 'support_condition': '余额变动与实际交付一致', 'invalidate_condition': '余额与退货退款出现矛盾',
                 'threshold_basis': 'qualitative', 'next_action': '核对明细', 'status': 'needs_baseline'}]}
    draft['investment_recommendation'] = watch_recommendation()
    calls = []

    def model(system, payload, *args, **kwargs):
        calls.append(copy.deepcopy(payload))
        if len(calls) == 1: return {'queries': [], 'pdf_queries': [], 'questions': []}, {}
        if len(calls) == 2: return {'queries': [], 'gaps': []}, {}
        if len(calls) == 3: return copy.deepcopy(draft), {}
        if len(calls) == 4:
            # Additional observations must not erase the draft's actual citation.
            store.update_run(run['id'], lambda r: r['research_sources'].extend(crowded_run()['research_sources']))
            return {'requires_revision': True, 'issues': ['核对原文']}, {}
        selected = {s['id']: s for s in payload['context']['sources']}
        assert selected['balance']['text'] == '已经读取的合同负债原文'
        return copy.deepcopy(draft), {}

    monkeypatch.setattr(deep, 'model_json', model)
    result = deep.build(run['id'], '修订报告')
    assert result['revision'] == 2 and len(calls) == 5
    attempt = store.get_run(run['id'])['deep_research_attempts'][-1]
    assert 'balance' in attempt['input_source_ids'] and 'balance' in attempt['repair_input_source_ids']
