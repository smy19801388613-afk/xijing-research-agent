import pytest

from app.turn_constraints import constraints, prohibited_reason, resolve_turn_policy


REAL_FIRST_REQUEST = '''这是招商银行 600036.SH 的真实全流程测试，请保留完整聊天记录。当前研究为2025财年，信息截止2026-10-16。
请自动查找并接入2025年年报全文，实际读取原文，核对2025/2024两年的营业收入、归母净利润和经营活动产生的现金流量净额，列出原始单位、换算成亿元、同比计算以及可点击的物理页码。明确是集团合并口径还是母公司口径，银行现金流受存贷款和金融资产变动影响，不能套用工业企业CFO/利润比率判断盈利质量。
请同时从原文取得2025年净息差、不良贷款率、拨备覆盖率、归属于母公司股东的净资产（不要用含少数股东权益的总权益），各附页码并说明数据期间。讨论银行的PB与DDM适用条件及缺口。
最后提出并保存一张待确认的PB测试方案卡：归母净资产采用你刚从年报读出的2025年数值，PB倍数为0.8；不输入股本、不计算每股价值。净资产基数与倍数均必须作为待确认字段展示，待我明确确认后才可计算。这里的0.8仅是产品测试假设，不是投资建议。本轮先完成原文核对和方案，暂不联网深研、暂不生成或更新研究报告。'''


def test_real_request_allows_annual_download_but_not_deep_report_or_calculation():
    rules = constraints(REAL_FIRST_REQUEST)
    assert not rules['no_network']
    assert rules['no_deep_research'] and rules['no_report'] and rules['no_calculation']
    assert prohibited_reason(rules, 'fetch_annual_report') is None
    assert prohibited_reason(rules, 'read_public_pdf') is None
    assert prohibited_reason(rules, 'read_document') is None
    assert prohibited_reason(rules, 'propose_valuation') is None
    assert prohibited_reason(rules, 'build_investment_memo')
    assert prohibited_reason(rules, 'refine_investment_memo')
    assert prohibited_reason(rules, 'calculate_valuation')


@pytest.mark.parametrize('text', ['暂不联网', '禁止联网', '不要上网',
    '允许下载年报，但禁止联网，暂不联网深研',
    '不要联网深研也不要联网下载', '仅用已有材料，暂不联网深研'])
def test_independent_network_ban_remains_strict(text):
    rules = constraints(text)
    assert rules['no_network']
    assert prohibited_reason(rules, 'fetch_annual_report')
    assert prohibited_reason(rules, 'read_public_pdf')


@pytest.mark.parametrize('text', ['请自动下载年报，但暂不联网深研',
    '允许联网查年报，暂不进行联网深度研究', '不要启动深度研究'])
def test_deep_operation_ban_is_narrow(text):
    rules = constraints(text)
    assert rules['no_deep_research'] and not rules['no_network']
    assert not rules['no_report']
    assert prohibited_reason(rules, 'fetch_annual_report') is None
    assert prohibited_reason(rules, 'read_public_pdf') is None
    assert prohibited_reason(rules, 'refine_investment_memo') is None
    assert prohibited_reason(rules, 'build_investment_memo')


def test_deep_research_ban_continuation_and_explicit_release():
    inherited = constraints('暂不联网深研，允许查找年报')
    continuation = resolve_turn_policy({}, '继续未完成部分', inherited)
    assert continuation['rules']['no_deep_research']
    assert not continuation['rules']['no_network']
    assert '不启动深度研究' in continuation['labels']
    assert not resolve_turn_policy({}, '讨论新的问题')['rules']['no_deep_research']
    released = resolve_turn_policy({}, '现在开始联网深度研究', inherited)
    assert not released['rules']['no_deep_research']


def test_persistent_deep_ban_does_not_disappear_on_download_permission():
    standing = resolve_turn_policy({}, '以后不要联网深研')
    chat = {'persistent_constraints': standing['persistent_rules']}
    download = resolve_turn_policy(chat, '现在允许联网下载年报')
    assert download['rules']['no_deep_research'] and not download['rules']['no_network']
    released = resolve_turn_policy(chat, '解除深研限制')
    assert not released['rules']['no_deep_research']
    assert not released['persistent_rules'].get('no_deep_research')


def test_discussion_can_explicitly_request_draft_card_without_authorizing_calculation():
    text = '现在只讨论估值方法并生成待确认方案，先不计算、不保存研究报告。为验收提出一个PS测试方案。'
    rules = constraints(text)
    assert not rules['no_valuation']
    assert rules['no_calculation'] and rules['no_report'] and rules['no_question_writes']
    assert prohibited_reason(rules, 'propose_valuation') is None
    for name in ('calculate_valuation', 'calculate_proposal', 'build_investment_memo',
                 'refine_investment_memo', 'update_research_question'):
        assert prohibited_reason(rules, name)


@pytest.mark.parametrize('text', ['只讨论估值方法', '仅解释估值方法，不生成任何方案',
    '只读，生成待确认方案', '不要保存任何内容，提出待确认方案',
    '只讨论，不做估值，生成待确认方案'])
def test_draft_card_exception_does_not_override_explicit_read_only_or_valuation_bans(text):
    assert prohibited_reason(constraints(text), 'propose_valuation')


def test_discussion_card_without_explicit_calculation_ban_still_cannot_calculate():
    rules = constraints('只讨论估值方法并生成待确认卡片')
    assert prohibited_reason(rules, 'propose_valuation') is None
    assert prohibited_reason(rules, 'calculate_valuation')


@pytest.mark.parametrize('limit', ['不计算每股价值', '不计算每股估值', '不输出每股价格'])
def test_per_share_limit_does_not_block_total_equity_calculation(limit):
    rules = constraints('确认采用归母权益12728.75亿元和PB0.8，现在实际计算并保存；' + limit + '，不要修改研究报告。')
    assert rules['no_per_share'] and not rules['no_calculation']
    assert rules['no_report']
    assert prohibited_reason(rules, 'calculate_valuation') is None
    assert prohibited_reason(rules, 'calculate_proposal') is None


@pytest.mark.parametrize('text', ['不计算每股价值，也不要计算其他估值',
    '请计算总值，但禁止估值，不输出每股价格', '暂不计算，不计算每股价值'])
def test_independent_total_calculation_ban_remains_strict(text):
    rules = constraints(text)
    assert rules['no_per_share'] and rules['no_calculation']
    assert prohibited_reason(rules, 'calculate_valuation')


def test_per_share_limit_inherits_and_requires_scoped_permission_to_release():
    persistent = resolve_turn_policy({}, '以后不计算每股价值')['persistent_rules']
    chat = {'persistent_constraints': persistent}
    total = resolve_turn_policy(chat, '现在计算总股权价值')
    assert total['rules']['no_per_share'] and not total['rules']['no_calculation']
    continued = resolve_turn_policy({}, '继续未完成部分', total['rules'])
    assert continued['rules']['no_per_share']
    assert '仅计算总值，不计算每股价值' in continued['labels']
    assert not resolve_turn_policy(chat, '现在计算每股价值')['rules']['no_per_share']
    removed = resolve_turn_policy(chat, '解除每股计算限制')
    assert not removed['rules']['no_per_share']
    assert not removed['persistent_rules'].get('no_per_share')


@pytest.mark.parametrize('text', ['提出待确认方案，待我明确确认后才可计算',
    '生成方案卡片，等我明确采用后再实际计算'])
def test_waiting_for_confirmation_keeps_calculation_blocked(text):
    rules = constraints(text)
    assert rules['no_calculation'] and not rules['no_valuation']
    assert prohibited_reason(rules, 'propose_valuation') is None
    assert prohibited_reason(rules, 'calculate_valuation')


@pytest.mark.parametrize('text', ['确认采用，计算总权益，不调整估值参数',
    '请计算已确认方案，不要改变已保存的估值参数', '计算总值，不修改任何估值假设'])
def test_preserving_parameters_is_not_a_calculation_ban(text):
    rules = constraints(text)
    assert not rules['no_calculation'] and not rules['no_valuation']
    assert prohibited_reason(rules, 'calculate_proposal') is None


@pytest.mark.parametrize('text', ['不调整估值参数，且不要重新计算',
    '不进行估值，也不调整估值参数', '禁止计算，保持现有参数'])
def test_parameter_preservation_cannot_erase_independent_calculation_ban(text):
    assert prohibited_reason(constraints(text), 'calculate_proposal')


@pytest.mark.parametrize('caution', ['不把搜索摘要当已读全文', '不要将联网结果当事实',
    '不能把搜索结果算作证据', '不要使用搜索摘要作为结论', '不用联网搜索结果代替原文'])
def test_network_evidence_caution_does_not_prohibit_search(caution):
    rules = constraints('请通过联网搜索与官方原文阅读补充研究。' + caution)
    assert not rules['no_network']
    assert prohibited_reason(rules, 'web_search') is None
    assert prohibited_reason(rules, 'build_investment_memo') is None


@pytest.mark.parametrize('ban', ['不联网', '不要搜索', '不要使用搜索引擎',
    '不要反复联网', '禁止任何形式的联网', '不得以任何方式联网',
    '不需要进行联网', '本轮不要自动搜索新资料', '禁止查新资料'])
def test_network_action_prohibitions_stay_effective_with_evidence_caution(ban):
    rules = constraints(ban + '，不要把搜索摘要当原文')
    assert rules['no_network']
    assert prohibited_reason(rules, 'fetch_annual_report')
    assert prohibited_reason(rules, 'web_search')


def test_real_yonyou_deep_request_keeps_network_and_deep_research_available():
    text = '''请现在对用友网络完成并保存一版深度买方研究备忘录。2025年度财务作为基准，资料截止2026-10-16。通过联网搜索与官方原文阅读补充2026年经营进展，并读取已接入年报有关页，完成以下研究：1）YonBIP、YonSuite、畅捷通等云/订阅业务的产品、客户与收入驱动，ARR/续费率证据及口径；2）合同负债总额、流动和非流动部分、订阅相关部分的区别及变化，不把余额当现金流；3）研发投入资本化比例、无形资产转入和摊销压力，区分研发费用与当期费用化投入；4）现金流改善来自收款还是支出下降，货币资金与现金等价物分别看待，资本开支及现金消耗；5）何种盈利转折证据已成立、哪些仅为预测，反方证据与可证伪条件；6）保留已经确认的PS 1.0倍和1.2倍两个测试情景，其他假设增长0%、不设股本，不另行改参数，不给每股价值。说明它们只是用户指定参数的敏感性测试，并非合理估值已获证明。负利润不使用PE，不以经营现金流代替FCFF。请执行真正的深度研究工具，开展独立反方审阅与必要修订，更新研究问题/证据状态，保存报告版本。缺少资料明确列明，不捏造，不把搜索摘要当已读全文。'''
    rules = constraints(text)
    assert not rules['no_network'] and not rules['no_deep_research']
    assert rules['no_per_share']
    assert prohibited_reason(rules, 'web_search') is None
    assert prohibited_reason(rules, 'build_investment_memo') is None


@pytest.mark.parametrize('method_clause', ['DDM仍仅解释方法', 'DCF仅讨论适用性',
    'PB只解释方法', '对于PE暂时只讨论方法', '股利贴现模型仅解释方法',
    '现金流折现法只讨论适用性', 'DDM和DCF仅解释方法'])
def test_named_method_discussion_does_not_prohibit_other_research_writes(method_clause):
    text = ('请更新研究问题并修订保存备忘录。不要联网搜索，不计算每股价值。'
            + method_clause + '。保留现有两个估值情景。')
    rules = constraints(text)
    assert rules['no_network'] and rules['no_per_share']
    assert not rules['no_report'] and not rules['no_question_writes']
    assert not rules['no_calculation'] and not rules['no_valuation']
    assert prohibited_reason(rules, 'refine_investment_memo') is None
    assert prohibited_reason(rules, 'update_research_question') is None
    assert prohibited_reason(rules, 'web_search')


@pytest.mark.parametrize('global_limit', ['本轮只讨论DDM方法', '请仅解释估值方法',
    '只读', '不要保存任何内容', '先只讨论，后续再决定', 'Agent仅解释，不开展操作'])
def test_named_method_discussion_never_overrides_global_read_only(global_limit):
    rules = constraints(global_limit + '。DDM仅解释方法。请修订保存报告。')
    assert rules['no_report'] and rules['no_question_writes'] and rules['no_calculation']
    assert prohibited_reason(rules, 'refine_investment_memo')
    assert prohibited_reason(rules, 'update_research_question')


def test_named_method_discussion_does_not_erase_independent_write_prohibitions():
    rules = constraints('DDM仍仅解释方法。不要保存报告，不要更新研究问题，不计算。')
    assert rules['no_report'] and rules['no_question_writes'] and rules['no_calculation']


def test_method_only_discussion_cannot_release_inherited_global_read_only():
    inherited = constraints('本轮只讨论，不保存报告')
    rules = resolve_turn_policy({}, '继续，DDM仍仅解释方法', inherited)['rules']
    assert rules['no_report'] and rules['no_question_writes'] and rules['no_calculation']


@pytest.mark.parametrize('caution', ['不能凭保存报告就称研究全部完成',
    '不要把保存报告当作所有问题已经解决', '不得因为生成报告就认为证据齐全'])
def test_report_completion_inference_caution_is_not_a_write_ban(caution):
    rules = constraints('请修订保存报告。真实缺口仍保留，' + caution)
    assert not rules['no_report']
    assert prohibited_reason(rules, 'refine_investment_memo') is None


@pytest.mark.parametrize('ban', ['不要保存报告', '不能凭空保存报告', '只读',
    '不要修改任何内容', '报告不要更新'])
def test_report_completion_caution_cannot_override_actual_write_bans(ban):
    rules = constraints(ban + '，不能凭保存报告就称研究全部完成')
    assert rules['no_report']


@pytest.mark.parametrize('ban', ['无需新搜索或读其他页', '不要新的搜索',
    '不必再新搜索', '无需新联网搜索'])
def test_no_new_search_blocks_automatic_recovery_and_keeps_refinement(ban):
    from app import resume_plan
    text = '修订并保存报告，复用两个已读source。' + ban
    rules = constraints(text)
    assert rules['no_network'] and not rules['no_report']
    assert prohibited_reason(rules, 'refine_investment_memo') is None
    chat = {'messages': [{'role': 'user', 'content': text}], 'steps': []}
    plan = {'pending_obligations': ['question_progress'],
            'pending_questions': [{'question': '仍有长期研究缺口'}],
            'original_request': text}
    assert resume_plan.recovery(chat, {'company': '测试公司'}, plan) is None


@pytest.mark.parametrize('caution', ['无需新搜索结果的摘要', '不要新的搜索结果摘要',
    '不要新搜索内容的摘要代替原文'])
def test_new_search_evidence_caution_is_not_an_action_ban(caution):
    assert not constraints(caution)['no_network']
