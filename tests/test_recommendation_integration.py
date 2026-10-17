from copy import deepcopy
import pytest

from app import agent_context, report_state, research_extensions
from app import questions
from app.tool_capabilities import TOOLS
from app.turn_constraints import prohibited_reason


def quote():
    return {'artifact':'market_quote','status':'available','ticker':'600887.SH',
        'price':'28.15','currency':'CNY','trade_date':'2026-10-16','as_of':'2026-10-17',
        'query_as_of':'2026-10-17','price_age_days':1,'source_id':'market-abc',
        'text':'600887.SH 2026-10-16 未复权收盘价28.15元',
        'provider':'tushare','retrieved_at':'2026-10-17T01:00:00Z','gaps':[]}


def test_market_tool_preserves_dated_price_through_repeated_context_compaction():
    rows=[{'tool':'fetch_market_quote','result':quote()}]
    rows += [{'tool':'read_document','result':{'artifact':'document_search','snippets':[
        {'document_id':'annual','page':i,'text':'已披露经营材料'*150}]}} for i in range(1,6)]
    compact=agent_context.compact_tool_results(agent_context.compact_tool_results(rows))
    found=next(r['result'] for r in compact if r.get('tool')=='fetch_market_quote')
    for key in ('price','ticker','trade_date','source_id','price_age_days'):
        assert found[key]==quote()[key]


def test_quote_refresh_affects_dependencies_only_for_semantic_changes():
    run={'market_snapshot':quote(),'memo':{'summary':'已保存原结论'}}
    original=deepcopy(run)
    before=research_extensions.dependencies(run)
    run['market_snapshot']['retrieved_at']='2026-10-17T02:00:00Z'
    assert research_extensions.dependencies(run)==before
    run['market_snapshot']['price']='29'
    assert research_extensions.dependencies(run)!=before
    assert run['memo']==original['memo']
    assert research_extensions.context(run)['market_snapshot']==run['market_snapshot']


def test_recommendation_only_valuation_links_are_preserved():
    memo={'investment_recommendation':{'valuation_scenario_ids':['pe1','valuation-ddm2'],
        'evidence_ids':['body','valuation-pe1']}}
    assert report_state.referenced_valuation_ids(memo)==['pe1','ddm2']


def test_quote_is_network_and_material_write_not_report_write():
    assert TOOLS['fetch_market_quote']=={'network','materials_write'}
    assert prohibited_reason({'no_network':True},'fetch_market_quote')
    assert prohibited_reason({'no_material_writes':True},'fetch_market_quote')
    assert not prohibited_reason({'no_report':True},'fetch_market_quote')


def test_recommendation_only_source_participates_in_save_conflict_detection():
    run={'request':{'as_of':'2026-10-17','year':2025,'ticker':'600887.SH'},
        'facts':[],'calculations':[],'research_sources':[{'id':'risk','kind':'web','read_status':'body_read',
        'published_at':'2026-03-20','text':'原始披露经营风险','source_type':'company_disclosure'}]}
    memo={'investment_recommendation':{'evidence_ids':['risk']}}
    memo['_question_input_snapshot']=questions._question_snapshot(run,memo)
    assert 'risk' in memo['_question_input_snapshot']['report_sources']
    questions.assert_report_input(run,memo)
    run['research_sources'][0]['text']='披露已更正，原结论失效'
    with pytest.raises(ValueError,match='引用来源已变化'):
        questions.assert_report_input(run,memo)
