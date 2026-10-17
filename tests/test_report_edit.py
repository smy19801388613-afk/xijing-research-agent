from copy import deepcopy

import pytest

from app import deep_research as deep, report_edit as edits, review_state, store
from recommendation_fixture import watch_recommendation


def report():
    return {'title':'原标题','summary':'原摘要','stance':'原立场',
            'theses':[{'title':title,'body':'已读资料中的推断','counter_evidence':'另一种解释',
                       'invalidate_if':'取得反向证据','evidence_ids':['web-source'],'status':'inference'}
                      for title in ('现金回收','持续盈利')],
            'sections':[{'key':key,'body':'保留此节原文。','evidence_ids':['web-source'],'status':'inference'}
                        for key in deep.SECTION_TITLES],
            'questions':['取得进一步资料'],'limitations':['原有研究边界']}


def chat_for(*texts):
    contexts={str(i):{'user_text':text,'task_turn_ids':[str(j) for j in range(i+1)],
                      **({'resume_from_turn_id':str(i-1)} if i else {})} for i,text in enumerate(texts)}
    return {'turn_id':str(len(texts)-1),'turn_contexts':contexts,
            'messages':[{'turn_id':str(i),'role':'user','content':text} for i,text in enumerate(texts)]}


@pytest.mark.parametrize('text,paths',[
    ('仅修正 catalysts 中一处时态，其他已核对的正文和估值保持不变。',{'/sections/catalysts/body'}),
    ('修改摘要和风险章节，估值保持不变。',{'/summary','/sections/risks/body','/sections/risks/status','/sections/risks/evidence_ids'}),
    ('请修改报告标题。',{'/title'}),
    ('只改研究立场。',{'/stance'}),
    ('仅修改第2个论点标题。',{'/theses/1/title'}),
    ('修改第二条核心论点的反证。',{'/theses/1/counter_evidence'}),
    ('请更新“现金回收”论点的失效条件。',{'/theses/0/invalidate_if'}),
    ('请修订财务质量章节。',{'/sections/financial_quality/body','/sections/financial_quality/status','/sections/financial_quality/evidence_ids'}),
    ('请修订估值、预期与安全边际章节。',{'/sections/valuation/body','/sections/valuation/status','/sections/valuation/evidence_ids'}),
    ('更新核实清单。',{'/questions'}),
    ('修改核心论点标题。',{'/theses/0/title','/theses/1/title'}),
])
def test_scope_is_derived_from_explicit_user_targets(text,paths):
    assert set(edits.resolve_scope(chat_for(text),report()).paths)==paths


@pytest.mark.parametrize('text',['只修改这一处，其他保持不变。','定点修正不准确的内容。'])
def test_ambiguous_local_scope_refuses_full_regeneration(text):
    with pytest.raises(ValueError,match='范围尚不明确'):
        edits.resolve_scope(chat_for(text),report())


def test_full_rewrite_and_continuation_scope():
    assert not edits.resolve_scope(chat_for('完整重写报告。'),report()).limited
    current=chat_for('只修改摘要。','继续未完成的保存。')
    assert edits.resolve_scope(current,report()).paths==('/summary',)
    current=chat_for('只修改摘要。','现在完整重写报告。')
    assert not edits.resolve_scope(current,report()).limited
    current=chat_for('完整重写报告。','现在只修改标题。')
    assert edits.resolve_scope(current,report()).paths==('/title',)


SMIC_RECOVERY = ('继续未完成部分，复用已保存的年报、网络正文、估值和未发布的最新草稿，不重新联网，不重做整套深研，也不重算已采用估值。'
    '先把系统已有2025、2024年度收入、归母净利润、经营现金流六项事实直接导入财务期间底稿，保留原值、来源与核验状态，然后分析年度同比。'
    '再完成原定敏感性：以方案98573c0a所保存的修正股数估值为基准，增长轴0、16.5、30，PE倍数轴30、40、50；其他参数保持不变。'
    '最后依据已读证据修复未发布草稿并保存完整买方备忘录，必须包含明确投资建议，分别写未持仓者和若已持有者的当前动作、期限、风险、反证与改变判断的条件；不足以决定时写清具体缺口。'
    '每项跟踪基线只填一个原始量和包含该量的连续摘录，不能把不同来源的资本开支和经营现金流合成一个未经计算的覆盖率。'
    '请保留已完成的反方审阅与来源，先解决阻止保存的具体错误再交付，不再重新扩展研究范围。')


@pytest.mark.parametrize('published',[report(), {'title':'旧财务简报','summary':'三项指标摘要','theses':[]}])
def test_actual_full_draft_recovery_does_not_patch_old_published_valuation_section(published):
    assert not edits.resolve_scope(chat_for(SMIC_RECOVERY),published).limited


@pytest.mark.parametrize('text',[
    '完整重写报告，估值参数保持不变。',
    '复用未发布草稿并保存完整买方备忘录，其他参数保持不变。',
    '以修正股数估值为基准，最后修复并保存完整买方备忘录。',
    '沿用既有估值情景，修复完整研究备忘录，增长假设和PE倍数保持不变。',
    '沿用既有估值情景并生成完整报告。',
    '保存完整买方备忘录且其他参数保持不变。',
])
def test_preserving_calculation_parameters_does_not_limit_full_report(text):
    assert not edits.resolve_scope(chat_for(text),report()).limited
    assert not edits.resolve_scope(chat_for('只修改摘要。',text),report()).limited


@pytest.mark.parametrize('text',[
    '只改估值章节，其他参数不变。',
    '请修改估值章节中的股本引用。',
    '只改估值章节并保存完整买方备忘录，其他内容不变。',
])
def test_real_valuation_section_edits_remain_limited(text):
    scope=edits.resolve_scope(chat_for(text),report())
    assert scope.limited and all(p.startswith('/sections/valuation/') for p in scope.paths)


YILI_COHERENCE = ('第3版投资建议已保存。本轮仅修改研究立场、估值章节正文和第3条估值论点正文，使旧叙述与已保存投资建议一致，然后保存新版本。'
    '具体问题：旧研究立场说不能形成持有结论，而建议栏已区分未持仓者不新建仓、若已持有则可继续持有但暂不加仓；请保留这种条件性区分。'
    '旧估值正文和对应论点说“企业价值字段为null，不能直接比较”，应改为PE每股股权价值与同币种股价在单位上可比较，但假设未经独立可比和预测验证，不能因此确认低估或给出目标价；企业价值桥接缺失不影响PE每股计算。'
    '\n其余正文、标题、摘要、引用、投资建议栏、跟踪指标和已保存估值保持不变。不联网；不新增计算或扩大研究。请直接用 refine_investment_memo 完成局部保存，告知新版本和具体修改。')
SMIC_COHERENCE = ('现在允许计算财务期间同比和敏感性，保留已有估值不重新计算。先调用 analyze_financial_periods，按实际底稿报告2025/2024营业收入、归母净利润、经营现金流年度同比，不能把主营或分部数字替换营业收入。'
    '\n再计算并保存敏感性，以已保存修正股数情景00337cfd4156为基准：增长轴0、16.5、30，PE倍数轴30、40、50。轴值使用我的这句原话，其他估值参数保持原样。'
    '\n然后仅修改投资建议栏、估值章节正文和第4条估值论点正文中的市值口径错误并保存：你已读年报第111页明确人民币普通股1,999,562,549股、香港上市普通股6,000,845,486股，总数8,000,408,035股。'
    '因此102.66元A股收盘价乘全部股份不能称实际A/H合计总市值，应删除这项总市值推断，保留每股情景与A股价的同币种比较及探索假设限制，不新增未取得的H股价格或汇率。'
    '投资建议、期限、动作与其他内容依据原稿保留，必要时只修正该口径叙述。其余标题、摘要、研究立场、正文、跟踪指标、引用和已保存估值保持不变。'
    '不联网，不扩展研究。完成后告知保存版本与实际完成结果。')


def four_thesis_report():
    value = report()
    value['theses'] += [{**deepcopy(value['theses'][0]), 'title': '估值假设'},
                        {**deepcopy(value['theses'][0]), 'title': '估值与风险'}]
    value['investment_recommendation'] = {'summary': '原建议', 'action': 'watch'}
    value['valuation_context'] = {'scenarios': [{'id': 'retained', 'output': {'price': '12'}}]}
    return value


@pytest.mark.parametrize('text,paths', [
    (YILI_COHERENCE, {'/stance', '/sections/valuation/body', '/theses/2/body'}),
    (SMIC_COHERENCE, {'/investment_recommendation', '/sections/valuation/body', '/theses/3/body'}),
])
def test_actual_coherence_requests_cover_exactly_three_requested_fields(text, paths):
    base = four_thesis_report()
    scope = edits.resolve_scope(chat_for(text), base)
    assert set(scope.paths) == paths
    values = {p: {'summary': '更正市值口径', 'action': 'watch'} if p == '/investment_recommendation'
              else '已修正：PE每股计算不依赖EV桥，A股价乘全部股份不是A/H实际总市值。' for p in paths}
    candidate = edits.apply_patch(base, {'edits': [{'path': p, 'value': v} for p, v in values.items()]}, scope)
    assert edits.editable_values(candidate, scope) == values
    for path in edits._all_paths(base) - paths:
        assert edits._get(candidate, path) == edits._get(base, path)
    assert candidate['valuation_context'] == base['valuation_context']
    assert base == four_thesis_report()


@pytest.mark.parametrize('object_name', ['已保存估值', '已保存的估值', '已有估值', '已采用估值',
                                        '估值结果', '估值计算结果', '估值情景', '估值参数'])
def test_protecting_calculations_in_mixed_list_does_not_freeze_authorized_prose(object_name):
    text = f'只修改估值章节正文。其余正文、标题、摘要、引用和{object_name}保持不变。'
    assert edits.resolve_scope(chat_for(text), report()).paths == ('/sections/valuation/body',)


@pytest.mark.parametrize('object_name', ['估值', '已保存估值章节', '已保存的估值正文', '估值结果的正文'])
def test_bare_or_explicit_valuation_prose_still_remains_protected(object_name):
    scope = edits.resolve_scope(chat_for(f'完整重写报告。{object_name}保持不变。'), report())
    assert scope.limited and '/sections/valuation/body' not in scope.paths


def test_named_valuation_thesis_and_reference_do_not_authorize_other_theses_or_section():
    scope = edits.resolve_scope(chat_for('仅修改第3条估值论点正文。对应论点应改为新的条件性说明。'), four_thesis_report())
    assert scope.paths == ('/theses/2/body',)


def test_replacement_content_does_not_expand_scope_or_turn_theme_into_section_target():
    scope=edits.resolve_scope(chat_for('只把摘要改为“风险仍待核实，估值仅作测试”。'),report())
    assert scope.paths==('/summary',)
    scope=edits.resolve_scope(chat_for('请把当前研究报告更新为现金流风险视角，请实际保存新版本。'),report())
    assert not scope.limited
    scope=edits.resolve_scope(chat_for('只把摘要改为“请完整重写报告”。'),report())
    assert scope.paths==('/summary',)


@pytest.mark.parametrize('text',['修改第七个论点标题。','修改第0个论点。','修改theses[2]正文。'])
def test_nonexistent_thesis_is_not_misread_as_report_title(text):
    with pytest.raises(ValueError,match='不存在'):
        edits.resolve_scope(chat_for(text),report())


@pytest.mark.parametrize('text',['完整重写报告，估值保持不变。','修订报告，不要修改估值。'])
def test_full_rewrite_with_preserved_section_is_bounded(text):
    scope=edits.resolve_scope(chat_for(text),report())
    assert scope.limited and '/summary' in scope.paths
    assert not any(path.startswith('/sections/valuation/') for path in scope.paths)


def test_conflicting_or_only_negative_scope_cannot_authorize_full_rewrite():
    for text in ('修改摘要，摘要保持不变。','不要修改风险。'):
        with pytest.raises(ValueError): edits.resolve_scope(chat_for(text),report())


def test_preserved_citations_are_excluded_from_authorized_section_edit():
    scope=edits.resolve_scope(chat_for('修改风险章节，风险章节引用保持不变。'),report())
    assert set(scope.paths)=={'/sections/risks/body','/sections/risks/status'}
    scope=edits.resolve_scope(chat_for('完整重写报告，引用保持不变。'),report())
    assert scope.limited and not any(path.endswith('/evidence_ids') for path in scope.paths)


def test_assistant_text_cannot_broaden_scope():
    chat=chat_for('只修改摘要。')
    chat['messages'].append({'turn_id':'0','role':'assistant','content':'完整重写报告和估值'})
    assert edits.resolve_scope(chat,report()).paths==('/summary',)


@pytest.mark.parametrize('response',[
    {'edits':[{'path':'/stance','value':'越权'}]},
    {'edits':[{'path':'/summary','value':'修改'},{'path':'/summary','value':'重复'}]},
    {'edits':[{'path':'/summary','value':'修改'}],'question_answers':[]},
    {'edits':[{'path':'/summary','value':'修改','operation':'replace'}]},
    {'title':'整份报告','summary':'错误输出'},
    {'edits':[]},
])
def test_patch_cannot_expand_write_paths(response):
    with pytest.raises(ValueError):
        edits.apply_patch(report(),response,edits.EditScope(('/summary',)))


def test_program_merge_preserves_all_unauthorized_editorial_fields_and_provenance():
    base=report();original=deepcopy(base);scope=edits.EditScope(('/summary',))
    candidate=edits.apply_patch(base,{'edits':[{'path':'/summary','value':'新摘要'}]},scope)
    normalized=deepcopy(candidate)
    normalized.update(title='规范化不应改标题',limitations=['验证器默认附注'],
                      _report_input_identity={'revision':4},_report_dependency_snapshot={'version':1})
    merged=edits.preserve_unauthorized(base,candidate,normalized,scope)
    assert base==original and merged['summary']=='新摘要'
    for key in edits.EDITORIAL_FIELDS:
        if key!='summary':
            assert merged.get(key)==base.get(key)
            assert (key in merged)==(key in base)
    assert merged['_report_input_identity']=={'revision':4}
    assert merged['_report_dependency_snapshot']=={'version':1}
    candidate['stance']='额外改动'
    with pytest.raises(ValueError,match='范围以外'):
        edits.preserve_unauthorized(base,candidate,normalized,scope)


def test_failed_legacy_full_draft_is_projected_onto_authorized_paths_only():
    base=report();draft=deepcopy(base)
    draft.update(summary='待续接摘要',stance='越权旧草稿立场',question_answers=[{'bad':'ledger write'}])
    projected=edits.project_working_draft(base,draft,edits.EditScope(('/summary',)))
    assert projected['summary']=='待续接摘要'
    assert projected['stance']==base['stance'] and projected['question_answers']==[]


def test_changed_thesis_citations_keep_validator_derived_fact_ids():
    base=report();base['theses'][0]['fact_ids']=['old']
    scope=edits.EditScope(('/theses/0/evidence_ids',))
    candidate=edits.apply_patch(base,{'edits':[{'path':'/theses/0/evidence_ids','value':['new']}]},scope)
    validated=deepcopy(candidate);validated['theses'][0]['fact_ids']=['new']
    result=edits.preserve_unauthorized(base,candidate,validated,scope)
    assert result['theses'][0]['fact_ids']==['new']
    assert result['theses'][1]==base['theses'][1]


@pytest.fixture
def saved_research(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    run=store.create_run({'ticker':'600588.SH','year':2025,'as_of':'2026-10-16',
                          'mode':'live','model_profile':'stub','question':'公司研究'})
    run.update(company='测试公司',research_sources=[{'id':'web-source','kind':'web','title':'已读披露',
               'url':'https://example.com/source','text':'已取得的经营事实资料',
               'published_at':'2026-09-01','read_status':'body_read'}])
    memo=deep.validate_memo(report(),run)
    memo['review']={'issues':['现金回收仍缺乏独立证明'],'strengths':['保留反证'],'revision_performed':True}
    run.update(memo=memo)
    store.save_run(run)
    chat=store.new_conversation(run['id'])
    return run,chat


def set_request(chat,text):
    request=chat_for(text)
    store.update_conversation(chat['id'],lambda current:current.update(**request))
    return store.get_conversation(chat['id'])


def test_full_recovery_uses_unpublished_seven_section_draft_over_legacy_brief(saved_research,monkeypatch):
    run,chat=saved_research
    brief={'title':'旧财务简报','summary':'三项财务数据','theses':[]}
    store.update_run(run['id'],lambda value:value.update(memo=deepcopy(brief)))
    chat=set_request(chat,SMIC_RECOVERY)
    unpublished=report();unpublished['investment_recommendation']=watch_recommendation()
    working={'working_draft':deepcopy(unpublished),'review':{'issues':[]},'attempt_id':'synthetic-attempt',
             'stage':'repaired_draft','unpublished':True}
    monkeypatch.setattr(deep.research_draft,'select_working_draft',lambda *args:deepcopy(working))
    payloads=[]
    def model(system,payload,*args,**kwargs):
        payloads.append(deepcopy(payload))
        assert 'allowed_edit_paths' not in payload and len(payload['working_draft']['sections'])==7
        return deepcopy(unpublished),{'model':'stub'}
    monkeypatch.setattr(deep,'model_json',model)
    result=deep.refine(run['id'],SMIC_RECOVERY,chat)
    saved=store.get_run(run['id'])
    assert result['artifact']=='report' and len(payloads)==1
    assert len(saved['memo']['sections'])==7 and saved['memo']['investment_recommendation']['action']=='watch'
    assert saved['memo_revisions'][-1]['memo']==brief


def test_refine_uses_patch_only_and_does_not_rewrite_summary_stance_or_ledger(saved_research,monkeypatch):
    run,chat=saved_research
    chat=set_request(chat,'仅修正 catalysts 中的一处时态，其他已核对的正文和估值保持不变。')
    seen=[]
    def model(system,payload,*args,**kwargs):
        seen.append(payload)
        assert payload['allowed_edit_paths']==['/sections/catalysts/body']
        assert '只返回JSON' in system and 'question_answers数组' not in system
        return {'edits':[{'path':'/sections/catalysts/body','value':'此前公告仅披露活动安排，实际举行情况尚未核实。'}]},{}
    monkeypatch.setattr(deep,'model_json',model)
    original=store.get_run(run['id'])
    result=deep.refine(run['id'],'工具试图扩大为完整重写摘要和立场',chat)
    updated=store.get_run(run['id'])
    assert result['revision']==original['report_revision']+1 and len(seen)==1
    for key in edits.EDITORIAL_FIELDS:
        if key!='sections':
            assert updated['memo'].get(key)==original['memo'].get(key)
            assert (key in updated['memo'])==(key in original['memo'])
    for before,after in zip(original['memo']['sections'],updated['memo']['sections']):
        if before['key']!='catalysts': assert before==after
    assert updated['research_questions']==original['research_questions']
    review=updated['memo']['review']
    assert review['issues'][0]['text']=='现金回收仍缺乏独立证明'
    assert review['issues'][0]['status']=='needs_review' and not review['independent_review_performed']


def test_title_only_edit_explicitly_restores_real_archived_review_without_old_prose(saved_research,monkeypatch):
    run, chat = saved_research
    chat = set_request(chat, '只修改报告标题，并恢复此前实际反方审阅意见到审阅区；其余正文和投资建议保持不变。')
    attempt = {'id': '2026-10-16T10:01:00+00:00', 'started_at': '2026-10-16T10:01:00+00:00',
               'run_id': run['id'], 'chat_id': chat['id'], 'turn_id': 'archived-turn', 'task_turn_id': 'archived-turn',
               'step_id': 'archived-step', 'base_report_revision': 1,
               'draft': {'title': '旧草稿标题', 'summary': '绝不能回滚的新旧差异'},
               'review': {'issues': ['历史A/H股份口径疑问', '历史资本开支疑问']}}
    store.update_run(run['id'], lambda r: r.update(report_revision=4,
        memo_updated_at='2026-10-16T11:00:00+00:00', deep_research_attempts=[deepcopy(attempt)]))
    def add_audit(c):
        c['turn_contexts']['archived-turn'] = {'run_id': run['id'], 'task_turn_id': 'archived-turn'}
        c['steps'].append({'id': 'archived-step', 'turn_id': 'archived-turn', 'tool': 'build_investment_memo',
                          'state': 'failed', 'started_at': '2026-10-16T10:00:00+00:00',
                          'finished_at': '2026-10-16T10:03:00+00:00'})
    store.update_conversation(chat['id'], add_audit)
    before = store.get_run(run['id'])
    def model(system, payload, *args, **kwargs):
        assert payload['allowed_edit_paths'] == ['/title']
        assert 'working_draft' not in payload
        assert len(payload['prior_review_issues']) == 3
        assert payload['historical_review_origins'][0]['attempt_id'] == attempt['id']
        return {'edits': [{'path': '/title', 'value': '带历史审阅的当前报告'}]}, {'model': 'stub'}
    monkeypatch.setattr(deep, 'model_json', model)
    result = deep.refine(run['id'], '模型工具参数不能代替用户授权', chat)
    after = store.get_run(run['id'])
    assert result['revision'] == 5
    for field in edits.EDITORIAL_FIELDS:
        if field != 'title':
            assert after['memo'].get(field) == before['memo'].get(field)
    review = after['memo']['review']
    assert len(review['issues']) == 3 and all(i['status'] == 'needs_review' for i in review['issues'])
    assert review['independent_review_performed'] is False
    assert review['recovered_from_unpublished_attempts'][0]['attempt_id'] == attempt['id']
    assert '未恢复旧稿正文' in review['semantic_status']
    assert after['deep_research_attempts'][0] == attempt
    assert after['deep_research_attempts'][-1]['recovered_review_issue_count'] == 2
    assert after['deep_research_attempts'][-1].get('resumed_from_attempt_id') is None


def test_refine_rejects_two_out_of_scope_model_outputs_without_save(saved_research,monkeypatch):
    run,chat=saved_research;chat=set_request(chat,'只修改摘要。')
    calls=[]
    def model(*args,**kwargs):
        calls.append(True)
        return {'edits':[{'path':'/stance','value':'越权立场'}]},{}
    monkeypatch.setattr(deep,'model_json',model)
    with pytest.raises(ValueError,match='未授权'):
        deep.refine(run['id'],'修改摘要',chat)
    assert len(calls)==2
    saved=store.get_run(run['id'])
    assert saved['memo']==run['memo'] and saved['report_revision']==run['report_revision']
    assert 'validation_error' in saved['deep_research_attempts'][-1]


def test_ambiguous_edit_fails_before_any_model_call(saved_research,monkeypatch):
    run,chat=saved_research;chat=set_request(chat,'只修改这一处。')
    monkeypatch.setattr(deep,'model_json',lambda *args,**kwargs:pytest.fail('must not call model'))
    with pytest.raises(ValueError,match='范围尚不明确'):
        deep.refine(run['id'],'模型擅自理解为修改风险章节',chat)


@pytest.mark.parametrize('name',['build_investment_memo','revise_report'])
def test_report_tool_router_uses_patch_for_scoped_user_request(saved_research,name):
    from app import agent
    _,chat=saved_research;chat=set_request(chat,'只修改报告摘要，其他内容保持不变。')
    assert agent.memo_tool_for_request(name,chat,[])=='refine_investment_memo'


def test_direct_build_tool_cannot_bypass_patch_router(saved_research,monkeypatch):
    from app import agent
    _,chat=saved_research;chat=set_request(chat,'只修改报告摘要，其他内容保持不变。')
    monkeypatch.setattr(deep,'build',lambda *a,**k:pytest.fail('scoped request reached full build'))
    with pytest.raises(ValueError,match='不可用完整深研'):
        agent.tool(chat,'build_investment_memo',{'question':'工具试图扩大为整篇深研'})


def test_full_rewrite_tool_route_remains_available(saved_research,monkeypatch):
    from app import agent
    _,chat=saved_research;chat=set_request(chat,'完整重写报告并重新联网研究。')
    monkeypatch.setattr(agent.resume_plan,'plan',lambda *a:{'pending_obligations':['search']})
    assert agent.memo_tool_for_request('build_investment_memo',chat,[])=='build_investment_memo'


def test_router_leaves_permission_and_scope_errors_to_execution_guard(saved_research):
    from app import agent
    _,chat=saved_research;chat=set_request(chat,'不要修改风险，也不要更新报告。')
    assert agent.memo_tool_for_request('build_investment_memo',chat,[])=='build_investment_memo'
    chat=set_request(chat,'只修改这一处。')
    assert agent.memo_tool_for_request('build_investment_memo',chat,[])=='build_investment_memo'
    with pytest.raises(ValueError,match='范围尚不明确'):
        agent.tool(chat,'build_investment_memo',{})


def test_review_issue_ids_are_stable_and_refinement_does_not_claim_resolution():
    previous={'issues':['待核对资本化',{'id':'known','text':'确认分红日期','status':'resolved','resolution':'已读实施公告'}],
              'strengths':['现金口径清楚']}
    normalized=review_state.normalize(previous,3)
    again=review_state.normalize(normalized,4)
    assert normalized==again
    after=review_state.after_refinement(normalized,3,('/summary',))
    assert [i['id'] for i in after['issues']]==[i['id'] for i in normalized['issues']]
    assert all(i['status']=='needs_review' for i in after['issues'])
    assert after['issues'][1]['previous_status']=='resolved'
    assert after['issues'][1]['resolution']=='已读实施公告'
    assert after['strengths']==previous['strengths']
    assert previous['issues'][1]['status']=='resolved'
    combined=review_state.combined(normalized,{'issues':['新的草稿意见']})
    assert len(combined['issues'])==3
    assert combined['issues'][0]['id']==normalized['issues'][0]['id']


def test_refocus_scope_uses_only_current_contract_obligations():
    from app.task_contract import resolve_contract
    chat=chat_for('只修改摘要。','现在仅修改报告标题。')
    prior,_=resolve_contract(chat,'只修改摘要。',turn_id='0')
    chat['turn_contexts']['0']['task_contract']=prior
    current,_=resolve_contract(chat,'现在仅修改报告标题。',chat['turn_contexts']['0'],'1')
    chat['turn_contexts']['1']['task_contract']=current
    assert current['obligation_turn_ids']==['1']
    assert edits.user_requests(chat)==['现在仅修改报告标题。']
    assert edits.resolve_scope(chat,report()).paths==('/title',)


def test_exports_show_structured_review_status_and_legacy_strings(saved_research):
    from app.exporting import markdown,printable
    run,_=saved_research
    run['memo']['review']={'semantic_status':'反方意见待复核','issues':[
        {'id':'stable','text':'缺少独立验证 <script>','status':'needs_review'},
        {'id':'done','text':'已读分红公告','status':'resolved'},'旧意见']}
    md=markdown(run);html=printable(run)
    for output in (md,html):
        assert '待复核：缺少独立验证' in output
        assert '已复核解决：已读分红公告' in output
        assert '历史意见，待复核：旧意见' in output
        assert "'status':" not in output
    assert '<script>' not in html and '&lt;script&gt;' in html


def test_direct_full_revision_retains_unresolved_review_without_new_reviewer(saved_research):
    from app import agent
    run,chat=saved_research;chat=set_request(chat,'完整重写报告。')
    store.update_run(run['id'],lambda r:r.update(state='completed'))
    before=store.get_run(run['id']);candidate=deepcopy(before['memo'])
    candidate['summary']='修订后仍需进一步核实。'
    # A model-supplied empty review is not authority to clear the old issues.
    candidate['review']={'issues':[],'semantic_status':'没有问题'}
    agent.tool(chat,'revise_report',{'memo':candidate},model_input_run=before)
    after=store.get_run(run['id'])['memo']['review']
    assert after['issues'][0]['text']=='现金回收仍缺乏独立证明'
    assert after['issues'][0]['status']=='needs_review'
    assert not after['independent_review_performed']


def test_new_independent_review_preserves_old_issues_by_stable_id():
    old=review_state.normalize({'issues':['此前未解决的口径问题']},2)
    fresh={'issues':['本次新的反方意见'],'strengths':['披露日期清楚'],'revision_performed':True}
    result=review_state.after_independent_review(old,fresh,3)
    assert result['issues'][0]['id']==old['issues'][0]['id']
    assert result['issues'][0]['status']=='needs_review'
    assert result['issues'][1]['status']=='open'
    assert result['independent_review_performed']
    omitted=review_state.after_independent_review(result,{'issues':[]},4)
    assert [i['id'] for i in omitted['issues']]==[i['id'] for i in result['issues']]


def test_full_build_does_not_drop_unmentioned_prior_review_issues(saved_research,monkeypatch):
    run,_=saved_research
    monkeypatch.setattr(deep.documents,'attached_documents',lambda _: [{'ticker':'600588.SH','year':2025,'title':'2025年年度报告','page_count':100}])
    monkeypatch.setattr(deep.documents,'ensure_index',lambda _: [])
    monkeypatch.setattr(deep.documents,'search_attached',lambda *a: {'snippets':[]})
    monkeypatch.setattr(deep.web_research,'search',lambda qs,**kw: {'queries':qs,'results':[]})
    generated=report()
    generated['investment_recommendation']=watch_recommendation()
    generated['tracking_metrics']=[{'id':'cash','name':'现金回收','definition':'同口径经营现金流与客户回款',
        'thesis':'现金回收','baseline':{'status':'missing','value':'尚缺回款明细','period':''},
        'frequency':'定期披露后','next_check':'下一次定期报告披露后',
        'support_condition':'现金回收改善','invalidate_condition':'回款持续逾期',
        'threshold_basis':'qualitative','next_action':'核查附注','status':'needs_baseline'}]
    replies=iter([{'queries':['测试公司 产品'], 'pdf_queries':['产品']}, {'queries':[]},
                  generated,{'issues':['新的持续盈利疑问'],'requires_revision':False}])
    monkeypatch.setattr(deep,'model_json',lambda *a,**kw:(next(replies),{}))
    deep.build(run['id'],'完整研究并重写报告')
    review=store.get_run(run['id'])['memo']['review']
    assert [(i['text'],i['status']) for i in review['issues']]==[
        ('现金回收仍缺乏独立证明','needs_review'),('新的持续盈利疑问','open')]
