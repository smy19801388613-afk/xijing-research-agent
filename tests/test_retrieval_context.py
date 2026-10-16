"""The real retrieval pools must inherit cancellation fences and one token ledger."""
import threading

import pytest

from app import competition,deep_research as deep,execution_budget as budgets,research,store,web_research


@pytest.fixture
def task(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    monkeypatch.setattr(competition,'POLICY',{'enabled':False,'model_origins':[],'retrieval_origins':[]})
    run=store.create_run({'ticker':'600036.SH','year':2025,'as_of':'2026-10-16','question':'核验业务与风险'})
    store.update_run(run['id'],lambda r:r.update(state='completed_with_gaps',company='招商银行',memo={'summary':'保留旧报告'}))
    chat=store.begin_turn(store.new_conversation(run['id'])['id'],'请联网研究招商银行的业务与风险','controlled','documents')
    return run,chat


def test_search_pool_shares_one_budget_across_simultaneous_queries(task,monkeypatch):
    run,chat=task
    limit={'total_tokens':50000,'time_minutes':1}
    barrier=threading.Barrier(2)
    seen=[];lock=threading.Lock();parent_thread=threading.get_ident()
    def query(query,cutoff):
        assert budgets.current()==limit
        reservation=budgets.reserve(15000,10000)
        assert reservation is not None
        barrier.wait(timeout=5)
        # Both requests have reserved the entire shared budget. Distinct copied
        # Context objects must point to the same synchronized ledger.
        before=budgets.snapshot()
        with lock: seen.append((threading.get_ident(),before['request_count']))
        barrier.wait(timeout=5)
        budgets.settle(reservation,{'total_tokens':14000})
        store.update_run(run['id'],lambda r:r['events'].append({'label':query}))
        return [],[],['controlled']
    monkeypatch.setattr(web_research,'_query_search',query)
    with store.execution_scope('conversation',chat['id'],chat['turn_id']):
        with budgets.scope(limit,on_usage=lambda usage:store.record_budget_usage(chat['id'],chat['turn_id'],usage)):
            response=web_research.search(['招商银行经营','招商银行风险'],as_of='2026-10-16')
            assert response['status']=='empty'
            assert budgets.snapshot()['reported_tokens']==28000
            assert budgets.snapshot()['remaining_tokens']==22000
            with pytest.raises(budgets.BudgetExhausted): budgets.reserve(22001,1)
    assert len({thread for thread,count in seen})==2
    assert all(thread!=parent_thread and count==2 for thread,count in seen)
    assert len(store.get_run(run['id'])['events'])==2
    assert store.get_conversation(chat['id'])['budget_usage']['request_count']==2


def test_deep_read_pool_cannot_write_after_chat_is_cancelled(task,monkeypatch):
    run,chat=task;tid=chat['turn_id']
    store.update_conversation(chat['id'],lambda c:c['steps'].append({
        'id':'deep-test','tool':'build_investment_memo','turn_id':tid,'state':'running'}))
    monkeypatch.setattr(deep,'model_json',lambda *a,**kw:({'questions':[],
        'queries':['招商银行业务','招商银行风险','招商银行经营'],'pdf_queries':[]}, {'status':'controlled'}))
    monkeypatch.setattr(deep.documents,'attached_documents',lambda c:[])
    def no_annual(*a,**kw): raise ValueError('annual download outside this controlled pool test')
    monkeypatch.setattr(deep.disclosures,'fetch_annual',no_annual)
    urls=['https://one.example/report','https://two.example/report']
    monkeypatch.setattr(web_research,'search',lambda *a,**kw:{'results':[
        {'title':'招商银行业务资料','url':url,'snippet':'已取得的搜索片段','published_at':'2026-09-01'} for url in urls]})
    barrier=threading.Barrier(2);cancelled=threading.Event();blocked=[];threads=set();lock=threading.Lock()
    def read_source(current,url,*a,**kw):
        reservation=budgets.reserve(500,500)
        assert reservation is not None
        budgets.settle(reservation,{'total_tokens':250})
        with lock: threads.add(threading.get_ident())
        barrier.wait(timeout=5)
        if url==urls[0]:
            store.cancel_conversation(chat['id'],tid)
            cancelled.set()
        assert cancelled.wait(5)
        try:
            # This is an actual storage write in the executor thread, not a
            # main-thread precheck. Missing execution context would allow it.
            store.put_document({'id':'late-'+str(urls.index(url)),'title':'must not be saved'})
        except store.ExecutionStopped:
            with lock: blocked.append(url)
            raise
        raise AssertionError('cancelled retrieval thread wrote to storage')
    monkeypatch.setattr(deep,'read_research_source',read_source)
    with store.execution_scope('conversation',chat['id'],tid):
        with budgets.scope({'total_tokens':50000,'time_minutes':1},
                on_usage=lambda usage:store.record_budget_usage(chat['id'],tid,usage)):
            with pytest.raises((store.ExecutionStopped,research.Cancelled)):
                deep.build(run['id'],'研究招商银行业务与风险',chat=chat)
            assert budgets.snapshot()['reported_tokens']==500
    assert len(threads)==2 and threading.get_ident() not in threads
    assert sorted(blocked)==sorted(urls)
    assert not store.list_documents()
    saved=store.get_conversation(chat['id'])
    assert saved['state']=='cancelled' and saved['budget_usage']['reported_tokens']==500
    assert store.get_run(run['id'])['memo']=={'summary':'保留旧报告'}
