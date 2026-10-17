"""Human sensitivity axes survive harmless formatting, never invented values."""
from copy import deepcopy

import pytest

from app import agent, store, valuation
from app.schemas import RunRequest


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    run=store.create_run(RunRequest(ticker='688981.SH',year=2025,as_of='2026-09-28',mode='live',data_source='documents').model_dump(mode='json'))
    run.update(state='completed_with_gaps',facts=[{'id':'profit','metric':'n_income_attr_p','label':'归母净利润',
        'year':2025,'value':'100000000','status':'api_only','evidence_ids':['income'],'announced_date':'2026-03-31'}],
        evidence=[{'id':'income','kind':'api','title':'合成接口','locator':'测试记录','quote':'归母净利润100000000','announced_date':'2026-03-31'}])
    store.save_run(run)
    base=valuation.save_result(run['id'],'pe',{'growth_pct':'16.5','multiple':'40'})
    args={'base_scenario_id':base['id'],'axis_x':[0,16.5,30],'axis_y':[30,40,50],
        'axis_quotes':{'x':'增长轴为0、16.5、30','y':'PE倍数轴为30、40、50'}}
    return run,base,args


def chat(run,text):
    return {'run_id':run['id'],'messages':[{'role':'user','content':text}]}


@pytest.mark.parametrize('axes',[
    '增长轴0、16.5、30，PE倍数轴30、40、50',
    '增长轴为0、16.5、30，PE倍数轴为30、40、50',
    '增长轴：0, 16.5, 30，PE倍数轴：30, 40, 50',
    '增长轴取值为0、16.5、30，PE倍数轴分别为30、40、50',
])
def test_recovery_axes_allow_only_presentation_variation(setup,axes):
    run,base,args=setup
    human='继续未完成部分，不重新联网，也不重算已采用估值。再完成原定敏感性：以所保存的修正股数估值为基准，'+axes+'；其他参数保持不变。'
    result=agent.tool(chat(run,human),'calculate_sensitivity',deepcopy(args))
    assert result['valid_cells']==9
    assert result['axes']['x']['values']==['0','16.5','30']
    assert result['axes']['y']['values']==['30','40','50']
    assert store.get_run(run['id'])['valuation_history']==[base]


@pytest.mark.parametrize('human,quotes',[
    ('请计算敏感性，不要用增长轴0、16.5、30，PE倍数轴30、40、50。',
        {'x':'增长轴为0、16.5、30','y':'PE倍数轴为30、40、50'}),
    ('请计算敏感性，增长轴0、16.5、30，禁止采用PE倍数轴30、40、50。',
        {'x':'增长轴为0、16.5、30','y':'PE倍数轴为30、40、50'}),
])
def test_recovered_human_clause_retains_negation(setup,human,quotes):
    run,_,args=setup;args['axis_quotes']=quotes
    with pytest.raises(ValueError,match='否定'):
        agent.tool(chat(run,human),'calculate_sensitivity',args)
    assert not store.get_run(run['id']).get('valuation_sensitivity_history')


@pytest.mark.parametrize('human',[
    '请计算敏感性，范围你自己决定。',
    '请计算敏感性，增长轴0、16.5、30。',
    '请计算敏感性，增长轴0、16.5、30，PE倍数轴30、40、500。',
    '请计算敏感性，增长轴0、16.5、30，PE倍数轴30、40、50.5。',
    '请计算敏感性，增长轴0、16.5、20，PE倍数轴30、40、50。',
])
def test_axes_still_require_current_human_values(setup,human):
    run,_,args=setup
    with pytest.raises(ValueError,match='原话'):
        agent.tool(chat(run,human),'calculate_sensitivity',args)
    assert not store.get_run(run['id']).get('valuation_sensitivity_history')


def test_no_quote_or_model_invented_extra_value_cannot_authorize_axes(setup):
    run,_,args=setup;human=chat(run,'请计算敏感性，增长轴0、16.5、30，PE倍数轴30、40、50。')
    with pytest.raises(ValueError,match='原话'):
        agent.tool(human,'calculate_sensitivity',{k:v for k,v in args.items() if k!='axis_quotes'})
    args['axis_x']=[0,16.5,30,40]
    with pytest.raises(ValueError,match='未确认'):
        agent.tool(human,'calculate_sensitivity',args)
    assert not store.get_run(run['id']).get('valuation_sensitivity_history')


def test_assistant_suggested_axes_are_not_user_authorization(setup):
    run,_,args=setup;human=chat(run,'请计算敏感性，范围你自己决定。')
    human['messages'].append({'role':'assistant','content':'建议增长轴0、16.5、30，PE倍数轴30、40、50。'})
    with pytest.raises(ValueError,match='原话'):
        agent.tool(human,'calculate_sensitivity',args)


def test_normalized_match_returns_exact_human_offsets():
    human='增长轴：0, 16.5, 30，PE倍数轴：30, 40, 50。'
    spans=agent.sensitivity_quote_spans(human,'增长轴为0、16.5、30')
    assert [human[start:stop] for start,stop in spans]==['增长轴：0, 16.5, 30']
    assert not agent.sensitivity_quote_spans('轴-10、20','10、20')
