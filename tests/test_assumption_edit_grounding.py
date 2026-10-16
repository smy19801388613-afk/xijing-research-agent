import pytest
from app import agent,store
from app.schemas import RunRequest


@pytest.fixture
def make_chat(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    run=store.create_run(RunRequest(use_model=False).model_dump(mode='json'))
    store.update_run(run['id'],lambda r:r.update(state='completed',valuation_history=[{
        'id':'old','method':'pb','assumptions':{'book_equity_100m':'100','multiple':'0.8'}}]))
    def create(text):
        c=store.new_conversation(run['id'])
        return store.begin_turn(c['id'],text,'stub','documents')
    return create


def test_real_from_old_to_new_uses_exact_human_span(make_chat):
    text='把已经保存的PB测试情景倍数从0.8改为0.9，其余参数不变。'
    chat=make_chat(text);quotes={'multiple':'PB倍数0.9'}
    agent.grounded_assumptions(chat,'pb',{'book_equity_100m':100,'multiple':0.9},quotes)
    assert quotes['multiple']=='把已经保存的PB测试情景倍数从0.8改为0.9'
    assert quotes['multiple'] in text


def test_old_value_is_not_authorized_by_quote_containing_both_values(make_chat):
    text='PB倍数从0.8改为0.9，其余不变。';chat=make_chat(text)
    with pytest.raises(ValueError,match='修改后的新值'):
        agent.grounded_assumptions(chat,'pb',{'multiple':0.8},{'multiple':text})


@pytest.mark.parametrize('text',['不要把PB倍数从0.8改为0.9。','PB倍数不改为0.9。'])
def test_negated_target_is_not_authorized_even_with_literal_quote(make_chat,text):
    chat=make_chat(text)
    with pytest.raises(ValueError,match='否定修改'):
        agent.grounded_assumptions(chat,'pb',{'multiple':0.9},{'multiple':text})


def test_conflicting_new_targets_are_not_silently_resolved(make_chat):
    chat=make_chat('PB倍数从0.8改为0.9；PB倍数改为1.0。')
    with pytest.raises(ValueError,match='冲突'):
        agent.grounded_assumptions(chat,'pb',{'multiple':0.9},{})


def test_share_change_is_not_misread_as_multiple_change(make_chat):
    chat=make_chat('PB倍数0.8，其余不变，股本从10改为20。');quotes={}
    agent.grounded_assumptions(chat,'pb',{'multiple':0.8,'shares_100m':20},quotes)
    assert quotes['shares_100m']=='股本从10改为20'


def test_feedback_distinguishes_user_quote_from_financial_evidence(make_chat):
    chat=make_chat('将PB测试倍数设为0.9。')
    with pytest.raises(ValueError,match='修改后的新值'):
        agent.grounded_assumptions(chat,'pb',{'multiple':1.2},{'multiple':'PB倍数1.2'})
    other=make_chat('讨论PB倍数，先看0.9和1.0两种可能。')
    with pytest.raises(ValueError,match='用户原话引用不匹配') as error:
        agent.grounded_assumptions(other,'pb',{'multiple':1.2},{})
    assert '不要求年报' in str(error.value)
