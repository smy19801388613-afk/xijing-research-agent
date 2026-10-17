"""Human intentions survive frozen contracts, dispatch, calculation and saving."""
from copy import deepcopy
from decimal import Decimal

import pytest

from app import agent, research, store, task_contract, turn_constraints


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    run = store.create_run({'ticker':'600887.SH', 'year':2025, 'as_of':'2026-10-17', 'mode':'live'})
    run.update(state='completed', memo={'title':'原报告'}, report_revision=1)
    for year in (2024, 2025):
        common = {'ts_code':'600887.SH', 'end_date':f'{year}1231', 'ann_date':f'{year+1}0420', 'report_type':'1'}
        research.add_api_facts(run, 'income', year, common | {'revenue':'200000000', 'n_income_attr_p':'100000000'}, 'test')
        research.add_api_facts(run, 'cashflow', year, common | {'n_cashflow_act':'50000000'}, 'test')
    store.save_run(run)
    chat = store.new_conversation(run['id'])
    monkeypatch.setattr(agent, 'model_json', lambda *a, **kw:pytest.fail('Unexpected model call'))
    return run, chat


def begin(chat, text, prior=None):
    if prior:
        agent.finish_error(chat['id'], prior, '隔离测试：上一轮尚未执行', 'failed')
    else:
        store.update_conversation(chat['id'], lambda c:c.update(state='idle'))
    return store.begin_turn(chat['id'], text, 'stub', 'documents', resume_from_turn_id=prior)


def propose(chat):
    current = begin(chat, '本轮只生成估值方案卡片。')
    result = agent.tool(current, 'propose_valuation', {'method':'pb', 'assumptions':{
        'book_equity_100m':'100', 'multiple':'1', 'shares_100m':'2'}})
    return current, result['proposal']


@pytest.mark.parametrize('text', [
    '请确认方案中的股本单位是否正确，先把结论告诉我。',
    '确认这个方案的股本数量是否准确。',
    '请确认这个方案是否合适。',
    '如果采用这个方案会怎样？',
    '是否采用这个方案？',
    '采用这个方案可以吗？',
    '我尚未采用这个方案。',
    '等我采用这个方案后再计算。',
    '采用这个方案，但现在暂不执行。',
    '采用这个方案，但不要执行这个方案。',
])
def test_checking_or_discussing_a_plan_never_adopts_it(case, text):
    run, chat = case
    _, proposal = propose(chat)
    current = begin(chat, text)
    before = deepcopy(store.get_run(run['id']))
    assert not turn_constraints.proposal_adoption(text)
    assert 'no_calculation' not in turn_constraints.explicit_permissions(text)
    with pytest.raises(ValueError):
        agent.tool(current, 'calculate_proposal', {'proposal_id':proposal['id']})
    assert store.get_run(run['id']) == before
    assert store.get_conversation(chat['id'])['proposals'][0]['state'] == 'proposed'


@pytest.mark.parametrize('text', [
    '采用方案 {id}，计算并保存。', '确认采用这个方案。', '确认这个估值方案。',
    '我同意这个方案。', '按这个方案执行。', '请计算已确认方案。',
    '确认采用，不调整倍数。',
])
def test_shared_adoption_releases_stage_and_saves_actual_calculation(case, text):
    run, chat = case
    old, proposal = propose(chat)
    current = begin(chat, text.format(id=proposal['id']), old['turn_id'])
    rules = task_contract.current_contract(current)['rules']
    assert not rules['no_calculation'] and rules['no_report']
    result = agent.tool(current, 'calculate_proposal', {'proposal_id':proposal['id']})
    assert result['result']['origin'] == 'accepted_proposal'
    assert Decimal(result['result']['output']['price']) == 50
    assert len(store.get_run(run['id'])['valuation_history']) == 1
    assert store.get_run(run['id'])['report_revision'] == 1


@pytest.mark.parametrize('continued', [False, True])
def test_a_later_check_cannot_reuse_older_adoption_in_aggregate_task(case, continued):
    run, chat = case
    _, proposal = propose(chat)
    accepted = begin(chat, '采用这个方案。')
    checking = begin(chat, '请确认方案中的股本单位是否正确。', accepted['turn_id'])
    current = begin(chat, '继续', checking['turn_id']) if continued else checking
    assert '采用这个方案' in task_contract.current_contract(current)['active_request']
    with pytest.raises(ValueError, match='明确要求'):
        agent.tool(current, 'calculate_proposal', {'proposal_id':proposal['id']})
    assert store.get_run(run['id'])['valuation_history'] == []


def test_adoption_is_bound_to_named_card(case):
    run, chat = case
    _, proposal = propose(chat)
    current = begin(chat, '采用方案 deadbeef，原先方案 '+proposal['id']+' 仅供比较。')
    with pytest.raises(ValueError, match='明确要求'):
        agent.tool(current, 'calculate_proposal', {'proposal_id':proposal['id']})
    assert store.get_run(run['id'])['valuation_history'] == []


@pytest.mark.parametrize('instruction', ['不要用PB1倍，采用PB0.8倍。', 'PB使用0.8倍。', '不要用PB1倍。'])
def test_adoption_cannot_bypass_latest_parameter_intent(case, instruction):
    run, chat = case
    _, proposal = propose(chat)
    current = begin(chat, '采用方案，'+instruction)
    with pytest.raises(ValueError, match='参数 multiple'):
        agent.tool(current, 'calculate_proposal', {'proposal_id':proposal['id']})
    assert store.get_run(run['id'])['valuation_history'] == []


def pe_args(multiple=20, quote='PE20倍'):
    return {'method':'pe', 'assumptions':{'growth_pct':5, 'multiple':multiple},
            'assumption_quotes':{'growth_pct':'增长5%', 'multiple':quote}}


@pytest.mark.parametrize('choice', ['采用PE15倍', '使用PE15倍', '用PE15倍', 'PE改为15倍'])
def test_forbidden_parameter_fragment_cannot_override_current_choice(case, choice):
    run, chat = case
    current = begin(chat, '请计算估值，增长5%，不要用PE20倍，'+choice+'。')
    before = deepcopy(store.get_run(run['id']))
    with pytest.raises(ValueError, match='修改后的新值'):
        agent.tool(current, 'calculate_valuation', pe_args())
    assert store.get_run(run['id']) == before
    # The same user request can execute with the affirmative target; the full
    # real calculation and storage path must remain usable, not merely blocked.
    result = agent.tool(current, 'calculate_valuation', pe_args(15, choice))
    assert Decimal(result['result']['output']['equity_value_100m']) == Decimal('15.75')
    assert len(store.get_run(run['id'])['valuation_history']) == 1
    assert result['result']['assumption_quotes']['multiple'] == choice


@pytest.mark.parametrize('text', [
    '请计算估值，增长5%，不要用PE20倍。',
    '请计算估值，增长5%，不用PE20倍。',
    '请计算估值，增长5%，PE不要用20倍。',
])
def test_negated_quote_is_rejected_even_when_previously_saved(case, text):
    run, chat = case
    current = begin(chat, '请计算估值，增长5%，采用PE20倍。')
    agent.tool(current, 'calculate_valuation', pe_args())
    current = begin(chat, text)
    before = deepcopy(store.get_run(run['id']))
    with pytest.raises(ValueError, match='否定修改'):
        agent.tool(current, 'calculate_valuation', pe_args())
    assert store.get_run(run['id']) == before


@pytest.mark.parametrize('quote', ['PE20倍', '20'])
def test_historical_negation_cannot_supply_new_parameter_consent(case, quote):
    run, chat = case
    begin(chat, '不要用PE20倍。')
    current = begin(chat, '请计算估值，增长5%。')
    with pytest.raises(ValueError, match='用户原话引用不匹配'):
        agent.tool(current, 'calculate_valuation', pe_args(20, quote))
    assert store.get_run(run['id'])['valuation_history'] == []


@pytest.mark.parametrize('text', [
    '请计算估值，增长5%，如果采用PE20倍会怎样？',
    '请计算估值，增长5%，PE20倍是否合适？',
])
def test_tentative_number_is_not_a_saved_assumption(case, text):
    run, chat = case
    current = begin(chat, text)
    with pytest.raises(ValueError, match='尚未明确授权'):
        agent.tool(current, 'calculate_valuation', pe_args())
    assert store.get_run(run['id'])['valuation_history'] == []


def test_opposing_parameter_instructions_require_resolution(case):
    run, chat = case
    current = begin(chat, '请计算估值，增长5%，采用PE20倍，但不要使用PE20倍。')
    with pytest.raises(ValueError, match='冲突'):
        agent.tool(current, 'calculate_valuation', pe_args())
    assert store.get_run(run['id'])['valuation_history'] == []


@pytest.mark.parametrize('text,expected', [
    ('请登记并保存财务期间底稿，然后分析财务期间同比。', {'financial_period_records','financial_period_analysis'}),
    ('请保存财务期间底稿并生成完整研究报告。', {'financial_period_records','report'}),
    ('请生成并保存研究底稿。', {'report'}),
    ('不要保存财务期间底稿。', set()),
])
def test_typed_period_ledger_and_report_are_separate_obligations(text, expected):
    assert task_contract.requested_artifacts(text) == expected
    if '财务期间底稿' in text and '研究报告' not in text:
        assert not turn_constraints.constraints(text)['no_report']


def test_period_ledger_and_analysis_can_finish_without_unrequested_memo(case, monkeypatch):
    run, chat = case
    current = begin(chat, '请登记并保存财务期间底稿，然后分析财务期间同比。')
    decisions = iter([
        {'action':'tool', 'tool':'record_financial_periods', 'arguments':{'fact_ids':[f['id'] for f in run['facts']]}},
        {'action':'tool', 'tool':'analyze_financial_periods', 'arguments':{}},
        {'action':'respond', 'message':'财务期间底稿已保存，年度同比已分析。'},
    ])
    monkeypatch.setattr(agent, 'model_json', lambda *a, **kw:(next(decisions), {'model':'stub'}))
    for name in ('build', 'refine'):
        monkeypatch.setattr(agent.deep_research, name, lambda *a, **kw:pytest.fail('Unrequested memo'))
    agent.execute(current['id'])
    saved = store.get_conversation(chat['id'])
    assert saved['state'] == 'idle'
    assert [s['tool'] for s in saved['steps']] == ['record_financial_periods','analyze_financial_periods']
    assert all(s['state']=='completed' for s in saved['steps'])
    assert saved['messages'][-1]['delivery']['status'] == 'completed'
    assert saved['messages'][-1]['delivery']['pending'] == []
    assert store.get_run(run['id'])['memo'] == run['memo']
    assert store.get_run(run['id'])['report_revision'] == 1
