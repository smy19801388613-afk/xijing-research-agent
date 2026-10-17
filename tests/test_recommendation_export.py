"""Investment actions are visible, frozen to their report, and safe to deliver."""
from copy import deepcopy
from html.parser import HTMLParser
import hashlib
import io
import json
import zipfile

import pytest

from app import competition, exporting, store
from app.schemas import RunRequest


class Parsed(HTMLParser):
    def __init__(self, content):
        super().__init__(); self.tags=[]; self.attrs=[]; self.text=[]; self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag); self.attrs.extend(attrs)

    def handle_data(self, data): self.text.append(data)


@pytest.fixture
def run(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    monkeypatch.setattr(competition,'POLICY',{'enabled':False,'model_origins':[],'retrieval_origins':[]})
    value=store.create_run(RunRequest(mode='live',ticker='600519.SH',year=2025,as_of='2026-10-16',use_model=False,data_source='documents').model_dump(mode='json'))
    value.update(company='合成建议验收',state='completed_with_gaps',report_revision=2)
    value['memo']={'title':'合成研究报告','summary':'证据和假设分别保存。','stance':'原研究立场保留，仍须核验经营持续性。',
                   'theses':[],'sections':[],'questions':[],'limitations':[],
                   'sources':[{'id':'source-a','kind':'pdf','title':'合成公告','url':'https://example.org/report.pdf','text':'合成材料，仅用于验收。'}]}
    store.save_run(value)
    return value


def recommendation(**overrides):
    return {'schema_version':1,'action':'buy','status':'conditional','basis':'valuation',
            'summary':'先补齐价格与股本口径，再核验估值是否有足够空间。','horizon':'未来十二个月','confidence':'medium',
            'as_of':'2026-10-16','new_position':'目前等待，条件满足后再考虑建仓。','existing_position':'核验原持仓理由，暂不加仓。',
            'buy_conditions':['补齐同一交易日价格和股本。'],'exit_conditions':['现金回收出现持续恶化。'],
            'key_risks':['利润增长不能转化为现金。'],'revisit_triggers':['下一期定期报告披露。'],
            'blocking_gaps':['缺少可核验收盘价。'],'evidence_ids':['source-a'],
            'valuation_scenario_ids':['scenario-old'],'valuation_basis':[{'scenario_id':'scenario-old','method':'pe','method_name':'PE · 市盈率',
                'assumptions':{'growth_pct':'0','multiple':'10'},'output':{'equity_value_100m':'100'},'formula':'利润 × 倍数'}],
            'market_snapshot':{'status':'missing','price':'999999','trade_date':'2099-01-01'},'price_comparisons':[],**overrides}


def test_legacy_exports_explain_missing_recommendation_and_preserve_stance(run):
    before=deepcopy(run)
    for output in (exporting.markdown(run),exporting.printable(run)):
        assert '此版本尚未形成明确建议' in output
        assert run['memo']['stance'] in output
    assert run==before and store.get_run(run['id'])==before


def test_conditional_advice_is_front_loaded_without_imagined_quote(run):
    run['memo']['investment_recommendation']=recommendation()
    before=deepcopy(run)
    for output in (exporting.markdown(run),exporting.printable(run)):
        assert output.index('投资建议：条件满足后买入')<output.index('核心财务事实')
        for text in ('未持仓','已有持仓','未来十二个月','置信度','建仓或加仓条件','减仓或退出条件','主要风险','何时调整建议','当前决策缺口','合成公告'):
            assert text in output
        assert '999999' not in output and '2099-01-01' not in output
        assert '参考收盘价' not in output and run['memo']['stance'] in output
    assert run==before


@pytest.mark.parametrize('action',['buy','hold','reduce','avoid','watch'])
def test_incomplete_evidence_cannot_render_as_unconditional_action(action):
    memo={'investment_recommendation':recommendation(action=action,status='insufficient_evidence')}
    assert exporting.recommendation_markdown(memo).startswith('\n## 投资建议：暂不决策')
    assert '<h2>投资建议：暂不决策</h2>' in exporting.recommendation_html(memo)


def test_actual_quote_and_comparison_are_frozen_strings_not_recomputed_from_run(run):
    run['memo']['investment_recommendation']=recommendation(status='actionable',
        market_snapshot={'status':'available','price':'20.00','currency':'CNY','trade_date':'2026-10-15','source_url':'https://example.org/quote','source_id':'source-a','decision_eligible':True},
        price_comparisons=[{'scenario_id':'scenario-old','scenario_price':'24.00','current_price':'20.00','upside_pct':'20.00','currency':'CNY','trade_date':'2026-10-15','assumption_status':'user_scenario'}])
    run['memo']['investment_recommendation']['valuation_basis'][0]['output']['price']='24.00'
    run['market_snapshot']={'status':'available','price':'999.00','trade_date':'2026-10-16'}
    run['valuation_history']=[{'id':'scenario-new','method':'pe','method_name':'PE · 市盈率','assumptions':{},'output':{'price':'888.00'}}]
    before=deepcopy(run)
    for output in (exporting.recommendation_markdown(run['memo']),exporting.recommendation_html(run['memo'])):
        assert '20' in output and '24' in output and '2026' in output
        assert '假设空间' in output and '不代表收益预测' in output
        assert '999' not in output and '888' not in output and '2099' not in output
    assert run==before


def test_source_and_scenario_refs_only_use_this_report_snapshot(run):
    run['memo']['investment_recommendation']=recommendation(evidence_ids=['source-a','not-in-report'],valuation_scenario_ids=['scenario-old','missing-scenario'])
    run['research_sources']=[{'id':'not-in-report','title':'不可拼入旧版的新来源'}]
    for output in (exporting.recommendation_markdown(run['memo']),exporting.recommendation_html(run['memo'])):
        assert 'https://example.org/report.pdf' in output
        assert '此版本未附来源' in output and '此版本未附情景' in output
        assert '不可拼入旧版的新来源' not in output
        assert 'PE' in output and '100' in output and '用户假设' in output


def test_model_markup_never_becomes_executable_in_recommendation_or_references(run):
    payload='<img src=x onerror=alert(1)><script>alert(2)</script>'
    run['memo']['sources']=[{'id':'source-a','kind':'pdf','title':payload,'url':'javascript:alert(3)','text':payload}]
    run['memo']['investment_recommendation']=recommendation(summary=payload,new_position=payload,
        key_risks=[payload],buy_conditions=['[bad](javascript:alert(4))'],
        market_snapshot={'status':'available','price':'20','currency':'CNY','trade_date':'2026-10-15','source_url':'javascript:alert(5)'})
    html=exporting.printable(run); parsed=Parsed(html)
    assert not {'img','script','svg','iframe'}.intersection(parsed.tags)
    assert not any(key.startswith('on') or (key=='href' and value.startswith('javascript:')) for key,value in parsed.attrs)
    assert '&lt;script&gt;' in html and payload in ''.join(parsed.text)
    md=exporting.recommendation_markdown(run['memo'])
    assert '<script>' not in md and '[bad](javascript:' not in md and '](<javascript:' not in md


def test_packaged_recommendation_retains_portable_source_links_and_raw_audit(run,tmp_path):
    run['memo']['sources'][0]['url']='/api/documents/report-a/file#page=3'
    run['memo']['investment_recommendation']=recommendation()
    run['market_snapshot']={'status':'missing','gaps':['合成验收无真实行情']}
    run['market_snapshot_history']=[deepcopy(run['market_snapshot'])]
    run['document_ids']=['report-a']
    pdf=tmp_path/'synthetic.pdf'; pdf.write_bytes(b'%PDF-1.7\nsynthetic fixture')
    snapshots=tmp_path/'snapshots';snapshots.mkdir()
    before=deepcopy(run)
    content=exporting.evidence_bundle(run,lambda doc_id:{'id':doc_id,'path':str(pdf),'title':'合成文档'},snapshots)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        paths=set(archive.namelist())
        assert {'analysis/investment-recommendation.json','analysis/market_snapshot.json','analysis/market_snapshot_history.json'}.issubset(paths)
        assert json.loads(archive.read('analysis/investment-recommendation.json'))==run['memo']['investment_recommendation']
        assert json.loads(archive.read('run.json'))==before
        for path in ('report.md','report.html'):
            text=archive.read(path).decode()
            assert '投资建议' in text and 'documents/report-a.pdf#page=3' in text
        manifest=json.loads(archive.read('manifest.json'))
        for entry in manifest['files']:
            assert hashlib.sha256(archive.read(entry['path'])).hexdigest()==entry['sha256']
    assert run==before


def test_historical_report_can_export_its_old_recommendation_without_latest_values(run):
    old=deepcopy(run['memo']);old['investment_recommendation']=recommendation(action='watch',status='insufficient_evidence',summary='历史版本仍在等证据。')
    run['memo']['investment_recommendation']=recommendation(status='actionable',summary='新版建议。')
    historical={**deepcopy(run),'memo':old,'report_revision':1}
    text=exporting.printable(historical)
    assert '投资建议：暂不决策' in text and '历史版本仍在等证据。' in text and '新版建议。' not in text
    assert run['memo']['investment_recommendation']['summary']=='新版建议。'


def test_watch_is_an_immediate_observation_state_not_a_future_trade():
    memo={'investment_recommendation':recommendation(action='watch',status='conditional')}
    assert '投资建议：观察，暂不行动' in exporting.recommendation_markdown(memo)
    assert '投资建议：观察，暂不行动' in exporting.recommendation_html(memo)
    assert '条件满足后观察' not in exporting.recommendation_html(memo)


def test_unsourced_available_quote_and_unbound_price_comparison_are_hidden(run):
    rec=recommendation(market_snapshot={'status':'available','price':'987654.32','trade_date':'2026-10-15','source_id':'not-in-report'},
                       price_comparisons=[{'scenario_id':'invented','scenario_price':'999999','upside_pct':'98765'}])
    run['memo']['investment_recommendation']=rec
    for output in (exporting.recommendation_markdown(run['memo']),exporting.recommendation_html(run['memo'])):
        assert '987654' not in output and '999999' not in output and '98765' not in output
        assert '参考收盘价' not in output and '假设空间' not in output
    rec['market_snapshot'].update(source_id='source-a',currency='CNY',decision_eligible=True)
    for output in (exporting.recommendation_markdown(run['memo']),exporting.recommendation_html(run['memo'])):
        assert '参考收盘价' in output and '假设空间' not in output


def test_stale_reference_quote_can_be_read_but_does_not_show_return_comparison(run):
    run['memo']['investment_recommendation']=recommendation(
        market_snapshot={'status':'available','price':'20','currency':'CNY','trade_date':'2026-10-01','source_id':'source-a',
                         'decision_eligible':False,'decision_gaps':['距截止日超过7天，需要核实停牌或数据迟延。']},
        price_comparisons=[{'scenario_id':'scenario-old','scenario_price':'24','upside_pct':'20'}])
    for output in (exporting.recommendation_markdown(run['memo']),exporting.recommendation_html(run['memo'])):
        assert '参考收盘价（适用性待核实）' in output and '行情适用限制' in output
        assert '假设空间' not in output


def test_title_edit_does_not_hide_changed_market_basis_or_refresh_old_judgment(run):
    run['memo']['investment_recommendation']=recommendation()
    frozen=deepcopy(run['memo']['investment_recommendation'])
    run['market_snapshot']={'status':'available','price':'88888','trade_date':'2026-10-16','ticker':'600519.SH','currency':'CNY','as_of':'2026-10-16'}
    run['memo']['title']='仅修改过的标题'
    for output in (exporting.markdown(run),exporting.printable(run)):
        assert '本建议依据需要重新核验' in output and '局部文字修改不会更新这些依据' in output
    assert run['memo']['investment_recommendation']==frozen
    run['market_snapshot']=deepcopy(frozen['market_snapshot'])
    assert '依据指纹' in exporting.recommendation_freshness(run)
    run['request']['as_of']='2026-10-17'
    assert '需明确重评' in exporting.recommendation_freshness(run)


def test_known_internal_prose_tokens_are_chinese_only_in_display_exports(run,tmp_path):
    run['memo']['investment_recommendation']=recommendation(
        summary='按revisit_triggers重新评估。',new_position='满足buy_conditions后再建仓。',
        existing_position='按exit_conditions减持。',key_risks=['结合revisit_triggers检查风险。'])
    run['memo']['sources'][0]['url']='https://example.org/exit_conditions.pdf'
    before=deepcopy(run)
    for output in (exporting.markdown(run),exporting.printable(run)):
        assert '按重新评估条件重新评估' in output
        assert '满足建仓或加仓条件后再建仓' in output and '按减仓或退出条件减持' in output
        assert '按exit_conditions减持' not in output and '满足buy_conditions后再建仓' not in output
        assert 'https://example.org/exit_conditions.pdf' in output
    snapshots=tmp_path/'snapshots';snapshots.mkdir()
    bundle=exporting.evidence_bundle(run,lambda identity:None,snapshots)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert json.loads(archive.read('run.json'))==before
        assert json.loads(archive.read('analysis/investment-recommendation.json'))==before['memo']['investment_recommendation']
    assert run==before
    assert exporting._recommendation_text('my_exit_conditions exit_conditions_old exit_conditions')=='my_exit_conditions exit_conditions_old 减仓或退出条件'


def test_frozen_advice_catalog_is_independent_of_revised_body_sources(run):
    old=deepcopy(run['memo']['sources'][0]);old.update(title='建议保留旧披露',text='原建议采用的证据金额。')
    run['memo']['recommendation_sources']=[old]
    run['memo']['sources'][0].update(title='正文采用新披露',url='https://example.org/revised.pdf',text='后续新增正文证据。')
    run['memo']['investment_recommendation']=recommendation(market_snapshot={
        'status':'available','price':'20','currency':'CNY','trade_date':'2026-10-15','source_id':'source-a'})
    before=deepcopy(run)
    for render in (exporting.recommendation_markdown,exporting.recommendation_html):
        output=render(run['memo'])
        assert '建议保留旧披露' in output and '正文采用新披露' not in output
        assert 'report.pdf' in output and 'revised.pdf' not in output and '参考收盘价' in output
    for render in (exporting.markdown,exporting.printable):
        output=render(run)
        assert '建议保留旧披露' in output and '正文采用新披露' in output
        assert '投资建议冻结来源' in output
    assert run==before


def test_explicit_empty_frozen_catalog_never_falls_back_to_body_or_embedded_quote(run):
    run['memo']['recommendation_sources']=[]
    run['memo']['investment_recommendation']=recommendation(market_snapshot={
        'status':'available','price':'87654','currency':'CNY','trade_date':'2026-10-15','source_id':'source-a',
        'text':'同一ID已改为新正文记录。','source_url':'https://example.org/quote'})
    for render in (exporting.recommendation_markdown,exporting.recommendation_html):
        output=render(run['memo'])
        assert '此版本未附来源' in output and '合成公告' not in output
        assert '参考收盘价' not in output and '87654' not in output


def test_quote_source_can_live_only_in_frozen_advice_catalog(run):
    run['memo']['recommendation_sources']=deepcopy(run['memo']['sources'])
    run['memo']['sources']=[]
    run['memo']['investment_recommendation']=recommendation(market_snapshot={
        'status':'available','price':'20','currency':'CNY','trade_date':'2026-10-15','source_id':'source-a'})
    for render in (exporting.recommendation_markdown,exporting.recommendation_html):
        assert '参考收盘价' in render(run['memo'])


def test_reading_export_has_cover_metadata_structured_body_and_printable_evidence(run):
    run['memo']['sections']=[{'title':'经营与现金流','body':'| 指标 | 2025 |\n|---|---:|\n| 合成营业收入 | 12.50 |\n\n<script>不可执行</script>', 'evidence_ids':['source-a']}]
    output=exporting.printable(run);parsed=Parsed(output)
    header=output.split('<header class="report-header">',1)[1].split('</header>',1)[0]
    for value in (run['company'],'600519.SH','2026-10-16','报告第 2 版'):
        assert value in header
    assert '模型状态' not in output and run['state'] not in output
    assert '模型状态' in exporting.printable(run,audit=True)
    assert 'size:A4' in output and 'table-header-group' in output and '<details open>' in output
    assert parsed.tags.count('table')>=2 and '<td>12.50</td>' in output
    assert 'script' not in parsed.tags and '&lt;script&gt;' in output
    assert output.index('核心判断')<output.index('证据附录')<output.index('交付说明')


def test_raw_evidence_and_question_urls_cannot_execute_in_export(run):
    run['evidence']=[{'id':'unsafe','title':'保留原始文字','locator':'合成位置',
                     'quote':'<script>来源原文</script>', 'source_url':'javascript:alert(1)'}]
    parsed=Parsed(exporting.printable(run))
    assert not any(key=='href' and value.startswith('javascript:') for key,value in parsed.attrs)
    assert '<script>来源原文</script>' in ''.join(parsed.text)


def test_source_less_thesis_exports_with_explicit_evidence_gap(run):
    run['memo']['theses']=[{'title':'待证实的论点','body':'尚需经营证据。','evidence_ids':[],
                          'counter_evidence':'缺少反证检验。','invalidate_if':'取得新披露。'}]
    for render in (exporting.markdown,exporting.printable):
        assert '证据待补充' in render(run)
        assert '核心投资论点' in render(run)


def test_long_advice_reason_does_not_displace_position_actions_or_quote(run):
    summary='长期盈利质量仍需要补充证据。'*120
    run['memo']['investment_recommendation']=recommendation(summary=summary,market_snapshot={
        'status':'available','price':'20','currency':'CNY','trade_date':'2026-10-15','source_id':'source-a'})
    html=exporting.recommendation_html(run['memo'])
    assert html.index('未持仓')<html.index('已有持仓')<html.index('参考收盘价')<html.index('建议理由')<html.index(summary)


def test_advice_notice_uses_shared_source_freshness_and_frozen_source_gaps(run):
    from app import questions
    rec=recommendation(source_fingerprints={'source-a':'prior-fingerprint'},valuation_scenario_ids=[],valuation_basis=[])
    run['memo'].update(investment_recommendation=rec,recommendation_sources=[],recommendation_source_gaps=['lost-source'])
    reasons=questions.report_sync(run)['recommendation_freshness']['reasons']
    assert any('来源已变化或失效' in reason for reason in reasons)
    assert any('当时冻结的来源版本' in reason for reason in reasons)
    for render in (exporting.markdown,exporting.printable):
        output=render(run)
        for reason in reasons:
            assert (exporting._markdown_text(reason) if render==exporting.markdown else reason) in output
        assert output.index('当时冻结的来源版本')<output.index('核心财务事实')


def test_reading_price_rounding_retains_exact_frozen_value_and_raw_details(run):
    precise='34.55639264160381512676832788'
    rec=recommendation(market_snapshot={'status':'available','price':'28.2','currency':'CNY',
        'trade_date':'2026-10-15','source_id':'source-a','decision_eligible':True},
        price_comparisons=[{'scenario_id':'scenario-old','scenario_price':precise,'current_price':'28.2',
            'upside_pct':'22.5404','currency':'CNY','trade_date':'2026-10-15','assumption_status':'user_scenario'}])
    rec['valuation_basis'][0]['output']['price']=precise
    run['memo']['investment_recommendation']=rec
    original=deepcopy(run['memo'])
    html=exporting.recommendation_html(run['memo'])
    pricing=html.split('<div class="recommendation-pricing">')[1].split('<h3>建议理由')[0]
    visible=''.join(Parsed(pricing).text)
    assert '34.56' in visible and '28.20' in visible and '22.54' in visible
    assert precise not in visible and '22.5404' not in visible
    assert precise in html and run['memo']==original
