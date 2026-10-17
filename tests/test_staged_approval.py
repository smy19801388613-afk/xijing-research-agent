"""A promised later phase never authorizes writes in the proposal-only turn."""
import pytest

from app import task_contract
from app.turn_constraints import constraints, explicit_permissions, prohibited_reason, resolve_turn_policy


PHASE_ONE = '''我们要做这家公司完整的买方研究，并最终保存有明确投资建议的备忘录。
本轮先准备证据和估值方案：查找并接入2025年年报，读取业务与主要财务数据、股本数量相关原文，取得研究截止日前最近一个交易日的真实市场收盘价。
根据现有历史财务数据与已读原文，讨论适合的估值方法，提出一个完整的估值方案卡片，明确假设、单位、出处与尚未核验项。
本轮结束在方案待确认阶段，等我采用方案后再计算和生成完整备忘录。说明目前资料是否足够与还缺什么。'''


def test_real_phase_one_preserves_evidence_and_proposal_but_not_later_writes():
    rules = constraints(PHASE_ONE)
    assert rules['no_calculation'] and rules['no_report']
    assert not rules['no_network'] and not rules['no_valuation'] and not rules['no_pdf_read']
    for tool in ('propose_valuation', 'fetch_annual_report', 'fetch_market_quote', 'search_documents', 'read_document', 'web_search'):
        assert prohibited_reason(rules, tool) is None
    for tool in ('calculate_proposal', 'calculate_valuation', 'calculate_sensitivity', 'build_investment_memo', 'refine_investment_memo'):
        assert prohibited_reason(rules, tool)
    contract, _ = task_contract.resolve_contract({}, PHASE_ONE, turn_id='first')
    assert 'proposal' in contract['completion']['required_artifacts']
    assert 'report' not in contract['completion']['required_artifacts']
    assert 'valuation' not in contract['completion']['required_artifacts']


@pytest.mark.parametrize('text', [
    '等我采用方案后再计算和生成完整备忘录。',
    '等我确认该方案后，再计算并保存报告。',
    '等待我明确采用这个估值方案之后，你再开始计算和生成备忘录。',
    '待用户批准估值方案以后才执行估值，并保存报告。',
    '只有我确认方案后才可计算和写报告。',
    '本轮先仅方案。',
    '本轮只生成估值方案卡片。',
    '这一轮仅准备一个参数方案。',
    '本次结束在方案待确认阶段。',
    '本轮停留在估值方案等待确认阶段。',
    '下一轮再计算估值并生成报告。',
    '估值计算放到下一轮，报告留到下次。',
])
def test_explicit_later_phase_blocks_calculation_and_report_only(text):
    rules = constraints(text)
    assert rules['no_calculation'] and rules['no_report']
    assert not rules['no_valuation'] and not rules['no_pdf_read'] and not rules['no_network']
    assert not {'no_calculation', 'no_report'} & explicit_permissions(text)


@pytest.mark.parametrize('text', [
    '本轮准备证据和方案，下一轮再报告。',
    '完整备忘录放在下一轮。',
    '等我采用该方案后再保存完整报告。',
])
def test_only_report_deferred_does_not_invent_a_calculation_ban(text):
    rules = constraints(text)
    assert rules['no_report'] and not rules['no_calculation']


def test_only_calculation_wait_does_not_block_current_authorized_report():
    rules = constraints('等我确认方案后再计算，本轮仍先保存研究报告。')
    assert rules['no_calculation'] and not rules['no_report']


@pytest.mark.parametrize('text', [
    '确认收入数据的单位后计算同比，并保存完整报告。',
    '先读取年报并核对股数，下一步计算估值并生成报告。',
    '我已经确认采用这个方案，现在计算并生成报告。',
    '不用等我确认方案，现在直接计算并生成报告。',
    '本轮只修改估值方案中的折现率，并重新计算和更新报告。',
    '下一轮再核对报告中的数字，本轮生成完整备忘录。',
])
def test_fact_checking_current_adoption_and_ordinary_work_order_are_not_new_bans(text):
    rules = constraints(text)
    assert not rules['no_calculation'] and not rules['no_report']


def test_pure_continue_cannot_release_a_pending_phase():
    previous = resolve_turn_policy({}, PHASE_ONE)['rules']
    rules = resolve_turn_policy({}, '继续未完成部分', previous)['rules']
    assert rules['no_calculation'] and rules['no_report']


def test_future_adoption_does_not_release_inherited_limits_when_other_rule_cancelled():
    previous = resolve_turn_policy({}, '本轮只生成估值方案卡片')['rules']
    rules = resolve_turn_policy({}, '解除联网限制。等我采用该方案后再计算并保存完整报告。', previous)['rules']
    assert rules['no_calculation'] and rules['no_report']


def test_later_affirmative_adoption_and_report_request_release_the_phase():
    first, _ = task_contract.resolve_contract({}, PHASE_ONE, turn_id='first')
    prior = {'task_contract': first, 'task_turn_ids': ['first']}
    second, _ = task_contract.resolve_contract({}, '确认采用这个方案，请计算估值并生成完整备忘录。', prior, turn_id='second')
    assert not second['rules']['no_calculation'] and not second['rules']['no_report']
    assert 'report' in second['completion']['required_artifacts']


def test_plan_adoption_alone_releases_calculation_without_granting_report_writes():
    previous = resolve_turn_policy({}, '本轮只生成估值方案卡片')['rules']
    rules = resolve_turn_policy({}, '我采用这个方案。', previous)['rules']
    assert not rules['no_calculation'] and rules['no_report']


@pytest.mark.parametrize('text', ['我尚未采用这个方案。', '是否采用这个方案？', '如果采用这个方案会怎样？'])
def test_nonaffirmative_adoption_does_not_release_an_inherited_calculation_gate(text):
    previous = resolve_turn_policy({}, '本轮只生成估值方案卡片')['rules']
    assert resolve_turn_policy({}, text, previous)['rules']['no_calculation']


RECOVERY_YILI = '''继续完成这次全流程验收。已有第2版报告和估值、敏感性结果保留，不重新联网或重算估值。
先把系统已保存的2025、2024年度收入、归母净利润、经营现金流六项事实直接导入财务期间底稿，保留原值、原来源和核验状态，不要再重抄PDF金额；然后分析年度同比，缺少季度就明确缺失。
接着仅修改并保存报告中的投资建议栏：适用期限明确为未来6个月；给未持仓者和若已持有者各自清楚的当前动作及改变动作的条件；区分真正阻碍当前判断的证据缺口与后续补充研究项，不把EV桥接所需的少数股东权益/净债务机械地当作PE计算必备项。
评级仍须依据现有证据，不强行买入或制造目标价。其余报告正文、标题、跟踪指标、问题答案、引用来源和已采用估值参数保持不变。请使用已有报告进行局部修订，完成后明确保存版本与实际进展。'''
RECOVERY_SMIC = '''继续未完成部分，复用已保存的年报、网络正文、估值和未发布的最新草稿，不重新联网，不重做整套深研，也不重算已采用估值。
先把系统已有2025、2024年度收入、归母净利润、经营现金流六项事实直接导入财务期间底稿，保留原值、来源与核验状态，然后分析年度同比。
再完成原定敏感性：以方案98573c0a所保存的修正股数估值为基准，增长轴0、16.5、30，PE倍数轴30、40、50；其他参数保持不变。
最后依据已读证据修复未发布草稿并保存完整买方备忘录，必须包含明确投资建议，分别写未持仓者和若已持有者的当前动作、期限、风险、反证与改变判断的条件；不足以决定时写清具体缺口。
每项跟踪基线只填一个原始量和包含该量的连续摘录，不能把不同来源的资本开支和经营现金流合成一个未经计算的覆盖率。请保留已完成的反方审阅与来源，先解决阻止保存的具体错误再交付，不再重新扩展研究范围。'''


@pytest.mark.parametrize('text', [RECOVERY_YILI, RECOVERY_SMIC])
def test_real_recovery_prompts_keep_saved_valuation_without_banning_requested_period_work(text):
    rules = constraints(text)
    assert rules['no_valuation_recalculation'] and not rules['no_calculation']
    assert rules['no_network'] and not rules['no_valuation'] and not rules['no_report']
    for name in ('record_financial_periods', 'analyze_financial_periods', 'calculate_sensitivity', 'refine_investment_memo'):
        assert prohibited_reason(rules, name) is None
    for name in ('calculate_valuation', 'calculate_proposal'):
        assert '不重新计算' in prohibited_reason(rules, name)


@pytest.mark.parametrize('text', ['不要重算已有估值。', '不重算已采用估值。', '不再计算已保存的估值情景。',
    '现有估值不要重新计算。', '不要重新计算之前的PE情景。', '不重新联网或重算估值。', '不要重算 PS 估值。'])
def test_targeted_recomputation_bans_only_base_valuation_tools(text):
    rules = constraints(text)
    assert rules['no_valuation_recalculation'] and not rules['no_calculation']
    assert prohibited_reason(rules, 'calculate_valuation') and prohibited_reason(rules, 'calculate_proposal')
    for name in ('propose_valuation', 'record_financial_periods', 'analyze_financial_periods', 'calculate_sensitivity', 'save_comparables'):
        assert prohibited_reason(rules, name) is None


@pytest.mark.parametrize('ban', ['禁止任何计算。', '不要计算。', '不要估值。', '只读，不保存任何结果。'])
def test_general_calculation_or_valuation_ban_still_has_priority(ban):
    rules = constraints(ban + '不要重算已有估值，计算敏感性，并导入财务期间和分析同比。')
    assert rules['no_calculation']
    for name in ('calculate_valuation', 'calculate_proposal', 'record_financial_periods', 'analyze_financial_periods', 'calculate_sensitivity'):
        assert prohibited_reason(rules, name)


def test_targeted_limit_persists_and_sensitivity_does_not_release_it():
    first = resolve_turn_policy({}, '以后不要重算已有估值。')
    assert first['persistent_rules'] == {'no_valuation_recalculation': True}
    chat = {'persistent_constraints': first['persistent_rules']}
    next_turn = resolve_turn_policy(chat, '现在计算敏感性并分析财务期间。')
    assert next_turn['rules']['no_valuation_recalculation'] and not next_turn['rules']['no_calculation']
    assert '不重算已有估值' in next_turn['labels']
    assert 'no_valuation_recalculation' not in explicit_permissions('现在计算敏感性。')


@pytest.mark.parametrize('permission', ['解除估值重算限制。', '解除重算限制。', '解除所有限制。',
    '现在重新计算已有估值。', '确认采用这个估值方案。'])
def test_targeted_limit_can_be_released_by_matching_affirmative_permission(permission):
    first = resolve_turn_policy({}, '以后不要重算已有估值。')
    chat = {'persistent_constraints': first['persistent_rules']}
    released = resolve_turn_policy(chat, permission)
    assert not released['rules']['no_valuation_recalculation']


def test_future_calculation_does_not_release_targeted_standing_limit():
    rules = resolve_turn_policy({'persistent_constraints': {'no_valuation_recalculation': True}},
        '等我采用这个估值方案后再计算估值。')['rules']
    assert rules['no_calculation'] and rules['no_valuation_recalculation']


def test_completion_keeps_periods_and_sensitivity_under_targeted_valuation_ban():
    artifacts = {'valuation', 'financial_period_records', 'financial_period_analysis', 'sensitivity', 'report'}
    allowed = task_contract.permitted_artifacts(artifacts, {'no_valuation_recalculation': True})
    assert allowed == artifacts - {'valuation'}


FINISH_YILI = '''现在允许计算财务期间同比并保存报告修订。请调用 analyze_financial_periods，重新核对2025和2024合并营业收入、归母净利润、经营现金流，最终答复纠正之前把主营收入1145.45/1143.02亿元说成营业收入的错误；金额必须来自本次工具结果，说明两种收入口径不同，历史消息保留。
然后仅修改估值章节正文。该章节末尾仍有“企业价值字段为null，不能直接比较”的错误旧句，应改成：PE每股股权价值与同币种股价在单位上可比较，但未经独立验证的PE和增长假设不能支持确定低估或目标价；企业价值桥接缺失不影响PE每股计算。保留该章节其余有依据分析并保存新版本。报告其他字段保持原样。本轮不联网，不重算估值情景，不新增研究问题。完成后报告真实保存版本及三项财务同比。'''


@pytest.mark.parametrize('inherited', [None, {'no_calculation': True, 'no_valuation_recalculation': True, 'no_report': True}])
def test_real_finish_prompt_releases_old_false_gate_without_recalculating_valuation(inherited):
    rules = resolve_turn_policy({}, FINISH_YILI, inherited)['rules']
    assert not rules['no_calculation'] and rules['no_valuation_recalculation']
    assert rules['no_network'] and not rules['no_report']
    for tool in ('record_financial_periods', 'analyze_financial_periods', 'calculate_sensitivity', 'refine_investment_memo'):
        assert prohibited_reason(rules, tool) is None
    for tool in ('calculate_valuation', 'calculate_proposal'):
        assert prohibited_reason(rules, tool)


@pytest.mark.parametrize('ban', ['不重算估值。', '不重算估值情景。', '不要重算PE。', '不要重新计算DCF。',
    '不要重复计算 PB。', '不要再算PS。', '估值情景不要重算。', 'PE不要重算。', '不要重算EV/EBITDA。'])
def test_recalculation_target_does_not_require_existing_modifier(ban):
    rules = resolve_turn_policy({}, '现在允许计算财务同比和敏感性。' + ban, {'no_calculation': True})['rules']
    assert rules['no_valuation_recalculation'] and not rules['no_calculation']
    assert prohibited_reason(rules, 'analyze_financial_periods') is None
    assert prohibited_reason(rules, 'calculate_sensitivity') is None
    assert prohibited_reason(rules, 'calculate_valuation')


@pytest.mark.parametrize('phrase', ['企业价值桥接缺失不影响PE每股计算。', '该缺口不会影响计算。',
    '缺少市场股价不妨碍估值。', '材料缺口不应阻碍已授权计算。'])
def test_negative_causal_description_is_not_a_calculation_ban(phrase):
    assert not constraints(phrase)['no_calculation']


@pytest.mark.parametrize('ban', ['禁止任何计算。', '不要计算。', '不允许计算。', '不要估值。'])
def test_real_finish_explicit_global_ban_still_wins(ban):
    rules = resolve_turn_policy({}, FINISH_YILI + ban, {'no_calculation': True})['rules']
    assert rules['no_calculation']
    assert prohibited_reason(rules, 'analyze_financial_periods')
    assert prohibited_reason(rules, 'calculate_sensitivity')


def test_causal_prefix_exemption_does_not_hide_second_negation_in_same_clause():
    assert constraints('该缺口不影响但不要计算。')['no_calculation']


def test_bare_method_recomputation_limit_persists_and_releases_explicitly():
    first = resolve_turn_policy({}, '以后不要重算PE。')
    assert first['persistent_rules'] == {'no_valuation_recalculation': True}
    chat = {'persistent_constraints': first['persistent_rules']}
    assert resolve_turn_policy(chat, '现在计算财务同比。')['rules']['no_valuation_recalculation']
    assert not resolve_turn_policy(chat, '现在重新计算PE。')['rules']['no_valuation_recalculation']
