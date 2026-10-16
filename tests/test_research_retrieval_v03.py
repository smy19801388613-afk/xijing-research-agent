import copy
import json
import pytest
from app import documents,store,valuation


@pytest.fixture
def run(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    value=store.create_run({'ticker':'300271.SZ','year':2025,'as_of':'2026-10-02'})
    value.update(state='completed_with_gaps',company='华宇软件',facts=[{
        'id':'revenue-2025','metric':'revenue','year':2025,'value':'1619404301.59','status':'api_only'}])
    value['gaps']=[{'code':'no_document','message':'尚未提供匹配的年度报告 PDF'},
                   {'code':'no_supplementary_evidence','message':'经营原因仍需补充材料'}]
    value['memo']={'summary':'保留原有判断','limitations':[
        '仅基于结构化财务数据，未获取年度报告全文，无法进行深入归因分析。',
        '未提供季度数据，无法分析年内趋势。','未完整核验全部附注。']}
    value['memo_revisions']=[{'memo':copy.deepcopy(value['memo'])}]
    store.save_run(value)
    return value


def attach_pages(run,pages,title='2025年年度报告.pdf',ocr_pages=None):
    folder=store.DATA/'documents';folder.mkdir(exist_ok=True)
    identity='qa-report'
    doc={'id':identity,'ticker':'300271.SZ','year':2025,'title':title,
         'announced_date':'2026-04-28','path':str(folder/'qa-report.pdf'),
         'source_url':'https://example.org/annual.pdf','created_at':store.now()}
    (folder/(identity+'.pages.json')).write_text(json.dumps(pages,ensure_ascii=False),encoding='utf-8')
    if ocr_pages:
        (folder/(identity+'.ocr.json')).write_text(json.dumps({str(k):{'text':v} for k,v in ocr_pages.items()},ensure_ascii=False),encoding='utf-8')
    store.put_document(doc)
    chat=store.new_conversation(run['id']);documents.attach(chat['id'],identity)
    return store.get_conversation(chat['id'])


def test_multiword_search_finds_products_with_separated_words(run):
    chat=attach_pages(run,['2025 年年度报告\n目录\n法律科技 产品 10',
        '分产品收入\n应用软件 696,855,146.81\n运维服务 599,989,913.18',
        '核心行业产品及解决方案介绍\n华宇万象大模型服务于法律科技领域。\n万象法官数字助理等产品。',
        '教育信息化产品及培训。'])
    result=documents.search_attached(chat,['法律科技 产品'])
    assert result['snippets'][0]['page']==3
    assert result['snippets'][0]['match_type']=='all_terms'
    assert '华宇万象' in result['snippets'][0]['text']
    assert result['snippets'][0]['url'].endswith('#page=3')


def test_search_window_tracks_whitespace_normalized_hit(run):
    chat=attach_pages(run,['2025 年年度报告','公司背景。'*900+'\n华宇万象大\n模型应用说明。'])
    result=documents.search_attached(chat,['华宇万象大模型'])
    assert result['snippets'][0]['page']==2
    assert '华宇万象大\n模型' in result['snippets'][0]['text']
    assert len(result['snippets'][0]['text'])<=2300


def test_multiword_search_preserves_ocr_markers_and_missing_pages(run):
    chat=attach_pages(run,['2025 年年度报告','',''],ocr_pages={2:'法律科技领域。具体产品包括万象知识库。'})
    result=documents.search_attached(chat,['法律科技 产品'])
    assert result['snippets'][0]['page']==2
    assert result['snippets'][0]['extraction']=='windows_ocr'
    assert result['pages_without_text'][0]['pages']==[3]
    assert 'OCR' in result['next_action']


def test_zero_hits_gives_retrieval_recovery_hint(run):
    chat=attach_pages(run,['2025 年年度报告','业务介绍。'])
    result=documents.search_attached(chat,['不存在的产品名称'])
    assert not result['snippets']
    assert '更短' in result['next_action']


def test_attaching_full_annual_refreshes_availability_without_rewriting_history(run):
    attach_pages(run,['2025 年年度报告全文','业务介绍'])
    saved=store.get_run(run['id'])
    assert [g['code'] for g in saved['gaps']]==['no_supplementary_evidence']
    assert saved['memo']==run['memo']
    assert saved['memo_revisions']==run['memo_revisions']
    assert saved['facts']==run['facts'] and saved['report_revision']==1


def test_summary_does_not_remove_full_report_caveat(run):
    attach_pages(run,['2025 年年度报告摘要'],'年报摘要.pdf')
    saved=store.get_run(run['id'])
    assert saved['gaps']==run['gaps']
    assert saved['memo']==run['memo']


def test_unrelated_material_does_not_resolve_annual_report_gap(run):
    attach_pages(run,['2025 年华宇万象产品手册'],'产品手册.pdf')
    saved=store.get_run(run['id'])
    assert saved['gaps']==run['gaps'] and saved['memo']==run['memo']


def test_repeated_numeric_valuation_submit_reuses_last_id(run):
    first=valuation.save_result(run['id'],'ps',{'growth_pct':1,'multiple':5})
    second=valuation.save_result(run['id'],'ps',{'growth_pct':'1.00','multiple':'5.0'},origin='conversation')
    assert first==second
    assert len(store.get_run(run['id'])['valuation_history'])==1


def test_revised_base_or_inputs_still_create_valuation_versions(run):
    inputs={'growth_pct':1,'multiple':5}
    first=valuation.save_result(run['id'],'ps',inputs)
    store.update_run(run['id'],lambda r:r['facts'][0].update(value='1700000000'))
    revised=valuation.save_result(run['id'],'ps',inputs)
    changed=valuation.save_result(run['id'],'ps',inputs|{'multiple':4})
    assert len({first['id'],revised['id'],changed['id']})==3
    assert revised['comparison']['output_changes']['equity_value_100m']['delta']!='0'


def test_existing_duplicate_history_is_preserved_and_same_method_saves_reused(run):
    inputs={'growth_pct':1,'multiple':5}
    old=[valuation.calculate(run,'ps',inputs) for _ in range(3)]
    store.update_run(run['id'],lambda r:r.update(valuation_history=old))
    reused=valuation.save_result(run['id'],'ps',inputs)
    assert reused['id']==old[-1]['id']
    assert store.get_run(run['id'])['valuation_history']==old
    pb=valuation.save_result(run['id'],'pb',{'book_equity_100m':20,'multiple':2})
    same_ps=valuation.save_result(run['id'],'ps',inputs)
    assert same_ps==reused==old[-1]
    history=store.get_run(run['id'])['valuation_history']
    assert history==old+[pb]
    assert len(history)==4
