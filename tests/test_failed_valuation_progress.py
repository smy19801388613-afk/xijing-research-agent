"""Regression for changing PB rationale while repeatedly submitting zero equity."""
import pytest

from app import agent, store, valuation


@pytest.fixture
def chat(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    c=store.new_conversation()
    return store.begin_turn(c['id'],'请提出PB测试方案，缺少归母净资产时先查资料。','stub','documents')


def proposal(equity=0,rationale='待核对'):
    return {'action':'tool','tool':'propose_valuation','arguments':{
        'method':'pb','assumptions':{'book_equity_100m':equity,'multiple':0.8},'rationale':rationale}}


def test_rationale_and_number_spelling_cannot_hide_same_failed_pb(chat,monkeypatch):
    calls=[]
    spellings=[0,'0.00',0.0,'0e0']
    def model(*a,**k):
        n=len(calls);calls.append(n)
        return proposal(spellings[n],f'不同措辞说明缺少净资产，第{n}次'),{'model':'stub'}
    def tool(c,name,args,*rest):
        # Use actual financial validation: the failure is the real zero-base bug.
        return valuation.calculate({'state':'completed'},args['method'],args['assumptions'])
    monkeypatch.setattr(agent,'model_json',model)
    monkeypatch.setattr(agent,'tool',tool)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert len(calls)==4 and saved['state']=='failed'
    assert saved['stop_reason']=='no_progress'
    assert all('归母净资产低于允许范围' in s['result']['error'] for s in saved['steps'])
    assert saved['messages'][-1]['delivery']['can_resume']


def test_reading_missing_base_then_correcting_it_can_save_proposal(chat,monkeypatch):
    decisions=iter([proposal(),proposal(rationale='再换个说法'),
        {'action':'tool','tool':'read_document','arguments':{'document_id':'annual','page':10,'count':1}},
        proposal(1234),{'action':'respond','message':'已从原文取得净资产，方案待确认，尚未执行估值。'}])
    monkeypatch.setattr(agent,'model_json',lambda *a,**k:(next(decisions),{'model':'stub'}))
    def tool(c,name,args,*rest):
        if name=='read_document':return {'snippets':[{'document_id':'annual','page':10,'text':'归母净资产1234亿元'}]}
        valuation.calculate({'state':'completed'},args['method'],args['assumptions'])
        return {'artifact':'proposal','proposal':{'id':'valid','state':'proposed'}}
    monkeypatch.setattr(agent,'tool',tool)
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='idle' and not saved.get('stop_reason')
    assert [s['state'] for s in saved['steps']]==['failed','failed','completed','completed']


def test_honest_missing_base_response_does_not_force_an_invalid_proposal(chat,monkeypatch):
    decisions=iter([proposal(),{'action':'respond','message':'当前缺少归母净资产原文，不能以零代替；未保存方案，缺口保留。'}])
    monkeypatch.setattr(agent,'model_json',lambda *a,**k:(next(decisions),{'model':'stub'}))
    monkeypatch.setattr(agent,'tool',lambda *a,**k:{'error':'归母净资产低于允许范围'})
    agent.execute(chat['id'])
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='idle' and len(saved['steps'])==1


@pytest.mark.parametrize('name',['propose_valuation','calculate_valuation'])
def test_economic_change_remains_distinct_but_rationale_and_quotes_do_not(name):
    a=proposal()['arguments']
    b={**a,'rationale':'changed','assumption_quotes':{'book_equity_100m':'0.00'},
       'assumptions':{'book_equity_100m':'0.00','multiple':'0.80','shares_100m':None}}
    assert agent.failed_action_fingerprint(name,a)==agent.failed_action_fingerprint(name,b)
    assert agent.failed_action_fingerprint(name,a)!=agent.failed_action_fingerprint(name,proposal(1234)['arguments'])


def test_proposal_id_and_real_read_locations_remain_distinct():
    key=agent.failed_action_fingerprint
    assert key('calculate_proposal',{'proposal_id':'a'})==key('calculate_proposal',{'proposal_id':'a','rationale':'change'})
    assert key('calculate_proposal',{'proposal_id':'a'})!=key('calculate_proposal',{'proposal_id':'b'})
    assert key('read_document',{'page':1})!=key('read_document',{'page':2})
