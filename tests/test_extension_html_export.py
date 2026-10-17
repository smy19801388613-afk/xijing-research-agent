"""Printable analysis must contain real structures and escaped evidence text."""
from copy import deepcopy
from html.parser import HTMLParser

from app import exporting, financial_periods, store, valuation, valuation_analysis
from app.schemas import RunRequest


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags=[]; self.attributes=[]; self.text=[]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag); self.attributes.extend(attrs)

    def handle_data(self, data):
        self.text.append(data)


def test_generated_headings_blank_line_tables_quotes_and_lists_are_html():
    source='''## 财务期间工作底稿

| 指标 | 本期 | 基期 |

|---|---:|---:|

| 营业收入 | 0.0100 | 0.0080 |

### 持续跟踪指标

> 原文第一行：净利润 < 经营现金流。
> 原文第二行：100 万元 & 80 万元。

- 对照公告核验口径
- 记录本次观察

1. 核验来源
2. 修订判断
'''
    html=exporting.extension_html(source); parsed=Elements(html)
    assert '<h2>财务期间工作底稿</h2>' in html
    assert '<h3>持续跟踪指标</h3>' in html
    assert parsed.tags.count('table')==1 and parsed.tags.count('thead')==1 and parsed.tags.count('tbody')==1
    assert parsed.tags.count('td')==3 and parsed.tags.count('th')==3
    assert '<p>|' not in html and '|---' not in html
    assert '<blockquote>原文第一行：净利润 &lt; 经营现金流。<br>原文第二行：100 万元 &amp; 80 万元。</blockquote>' in html
    assert parsed.tags.count('ul')==1 and parsed.tags.count('ol')==1 and parsed.tags.count('li')==4


def test_markup_and_urls_never_supply_executable_tags_or_attributes():
    source='''## <img src=x onerror=alert(1)>

| 来源 | 内容 |
|---|---|
| <script>alert(1)</script> | [执行](javascript:alert(1)) |

> </blockquote><svg onload=alert(1)> & 原文

- <iframe srcdoc="bad"></iframe>

普通文本 <a href="javascript:alert(1)">链接</a>

```json
{"quote":"</code><script>danger</script>"}
```
'''
    html=exporting.extension_html(source); parsed=Elements(html)
    assert not {'img','script','svg','iframe','a'}.intersection(parsed.tags)
    assert parsed.attributes==[('class','extension-table')]
    assert '<script>' not in html and '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '<pre><code>' in html and '&lt;/code&gt;&lt;script&gt;danger&lt;/script&gt;' in html
    assert '[执行](javascript:alert(1))' in ''.join(parsed.text)
    assert '</blockquote><svg onload=alert(1)> & 原文' in ''.join(parsed.text)


def test_non_table_pipe_text_and_unclosed_code_preserve_original_text():
    html=exporting.extension_html('金额 | 口径\n下一行\n\n| 只有一行 | 不应误判 |\n\n```\n|---|\n<script>x</script>')
    assert '<table' not in html
    assert '<p>金额 | 口径<br>下一行</p>' in html
    assert '<pre><code>|---|\n&lt;script&gt;x&lt;/script&gt;</code></pre>' in html


def test_real_printable_export_renders_periods_sensitivity_and_tracking(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    run=store.create_run(RunRequest(mode='live',ticker='600519.SH',year=2025,as_of='2026-09-28',use_model=False,data_source='documents').model_dump(mode='json'))
    run.update(company='合成导出验收',state='completed_with_gaps',facts=[
        {'id':'profit','metric':'n_income_attr_p','label':'归母净利润','year':2025,'value':'100000000',
         'status':'api_only','evidence_ids':['income'],'announced_date':'2026-03-31'}],
        evidence=[{'id':'income','kind':'api','title':'合成来源','locator':'合成记录','quote':'归母净利润100000000','announced_date':'2026-03-31'}])
    store.save_run(run)
    financial_periods.save_records(run['id'],[
        {'metric':'revenue','year':year,'period':'FY','period_type':'cumulative','value':value,'unit':'万元','currency':'CNY','scope':'consolidated',
         'source_type':'user_input','source_id':'synthetic','quote':f'合成验收营业收入{value}万元','announced_date':'2026-03-31'}
        for year,value in [(2024,'80'),(2025,'100')]])
    base=valuation.save_result(run['id'],'pe',{'growth_pct':'0','multiple':'10'})
    valuation_analysis.save_sensitivity(run['id'],{'base_scenario_id':base['id'],'axis_x':[0,10],'axis_y':[10,20]})
    quote='原文保持：收入100万元，<script>不是可执行内容</script>。\n第二行：须复核回款。'
    def memo(value):
        value['memo']={'title':'合成报告','summary':'演示导出结构。','tracking_metrics':[
            {'name':'收入与回款','thesis':'现金回收质量','definition':'营业收入与经营现金流的比较',
             'baseline':{'status':'observed','value':'100万元','period':'2025年度','source_id':'synthetic','quote':quote},
             'frequency':'每次定期披露后','next_check':'下一份公告','support_condition':'现金回收改善','invalidate_condition':'应收持续逾期',
             'next_action':'核对附注','threshold_basis':'qualitative'}]}
    saved=store.update_run(run['id'],memo);before=deepcopy(saved)
    html=exporting.printable(saved);parsed=Elements(html)
    assert '<h2>财务期间工作底稿</h2>' in html
    assert '<h3>同比与环比</h3>' in html and '<td>25.00%</td>' in html
    assert '<h2>估值敏感性与情景包络</h2>' in html
    assert '预测增长率' in ''.join(parsed.text) or '增速' in ''.join(parsed.text)
    assert '<h2>持续跟踪指标</h2>' in html and '<h3>收入与回款</h3>' in html
    assert parsed.tags.count('table')>=5 and parsed.tags.count('thead')>=4
    assert '<p>|' not in html and '<p>## ' not in html
    assert '<script>' not in html and '&lt;script&gt;不是可执行内容&lt;/script&gt;' in html
    assert '<blockquote>原文保持：收入100万元，' in html and '<br>第二行：须复核回款。</blockquote>' in html
    assert saved==before and store.get_run(run['id'])==before
