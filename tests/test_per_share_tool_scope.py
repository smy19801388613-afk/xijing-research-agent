"""A per-share ban must not block total-value PB or mutate accepted inputs."""
import copy
import pytest

from app import agent, store
from app.schemas import RunRequest


@pytest.fixture
def chat(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    c=store.new_conversation()
    c=store.begin_turn(c['id'],'确认计算PB总权益；不计算每股价值。','stub','documents')
    return store.get_conversation(c['id'])


@pytest.mark.parametrize('name',['propose_valuation','calculate_valuation'])
def test_pb_total_value_scope_allows_no_shares_and_rejects_shares(chat,name):
    good={'method':'pb','assumptions':{'book_equity_100m':100,'multiple':0.8}}
    agent.enforce_valuation_scope(chat,name,good)
    bad={**good,'assumptions':{**good['assumptions'],'shares_100m':10}}
    saved=copy.deepcopy(bad)
    with pytest.raises(ValueError,match='省略股本参数'):
        agent.tool(chat,name,bad)
    assert bad==saved


@pytest.mark.parametrize('name',['propose_valuation','calculate_valuation'])
def test_ddm_without_shares_still_produces_per_share_and_is_rejected(chat,name):
    with pytest.raises(ValueError,match='每股股利法'):
        agent.tool(chat,name,{'method':'ddm','assumptions':{'dividend_per_share':1,'cost_of_equity_pct':8,'terminal_growth_pct':2}})


def test_confirmed_proposal_is_not_silently_stripped_or_cached(chat,monkeypatch):
    proposal={'id':'p1','run_id':None,'method':'pb','assumptions':{'book_equity_100m':100,'multiple':0.8,'shares_100m':10},'valuation_id':'saved'}
    store.update_conversation(chat['id'],lambda c:c.update(proposals=[copy.deepcopy(proposal)]))
    with pytest.raises(ValueError,match='既有方案不能原样执行'):
        agent.tool(chat,'calculate_proposal',{'proposal_id':'p1'})
    decisions=iter([{'action':'tool','tool':'calculate_proposal','arguments':{'proposal_id':'p1'}},
                    {'action':'respond','message':'既有方案包含股本，本轮未执行；需要符合总权益范围的方案。'}])
    monkeypatch.setattr(agent,'model_json',lambda *a,**k:(next(decisions),{'model':'stub'}))
    monkeypatch.setattr(agent,'reusable_step',lambda *a,**k:pytest.fail('scope must be checked before cached output'))
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='idle' and saved['steps'][0]['state']=='failed'
    assert saved['proposals']==[proposal]


def test_no_per_share_false_preserves_normal_scope(chat):
    agent.finish_error(chat['id'],chat['turn_id'],'上一轮结束','failed')
    current=store.begin_turn(chat['id'],'现在允许计算每股价值。','stub','documents')
    agent.enforce_valuation_scope(current,'calculate_valuation',
                                 {'method':'pe','assumptions':{'shares_100m':10}})


@pytest.mark.parametrize('suffix',['不计算每股价值。','不计算每股价值，不调整估值参数，不要修改研究报告。'])
def test_actual_pb_proposal_and_acceptance_save_total_equity_only(chat,suffix):
    run=store.create_run(RunRequest(use_model=False).model_dump(mode='json'))
    store.update_run(run['id'],lambda r:r.update(state='completed'))
    store.update_conversation(chat['id'],lambda c:c.update(run_id=run['id']))
    result=agent.tool(chat,'propose_valuation',{'method':'pb','assumptions':{'book_equity_100m':100,'multiple':0.8}})
    assert store.get_run(run['id'])['valuation_history']==[]
    store.update_conversation(chat['id'],lambda c:c.update(state='idle'))
    current=store.begin_turn(chat['id'],'确认采用方案'+result['proposal']['id']+'，计算总权益，'+suffix,'stub','documents')
    assert agent.effective_rules(current)['no_per_share']
    calculated=agent.tool(current,'calculate_proposal',{'proposal_id':result['proposal']['id']})
    assert calculated['result']['output']['equity_value_100m']=='80.0'
    assert calculated['result']['output']['price'] is None
    assert len(store.get_run(run['id'])['valuation_history'])==1


@pytest.mark.parametrize('text',[
    '确认采用方案，不要修改研究报告。','确认采用，不调整倍数。',
    '确认采用，先不修改估值参数。','采用方案，把报告改为关于PE的测试说明。',
    '采用方案，修改研究问题的说明。'])
def test_non_parameter_or_negated_edits_do_not_block_adoption(text):
    assert not agent.valuation_change_requested(text)


@pytest.mark.parametrize('text',[
    '采用方案，把PE改为22倍。','确认，但PB倍数调整为0.9。',
    '采用方案，增长率设为5%。','确认采用，不调整倍数，但股本改成20亿股。',
    '请修改估值参数，增长率不再使用0%。'])
def test_real_new_parameters_still_require_updated_proposal(text):
    assert agent.valuation_change_requested(text)
