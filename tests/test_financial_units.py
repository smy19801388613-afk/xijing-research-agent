from copy import deepcopy

import pytest

from app.financial_units import assert_memo_amounts, check_text, memo_findings, source_amounts


CHANNELS = {
    "id": "channels", "title": "示例公司2025年年度报告 · 第17页",
    "text": """单位：万元 币种：人民币
渠道类型 本期销售收入 上期销售收入
本期销售量
（吨）
上期销售量
（吨）
直销 8,454,303.19 7,484,332.71 21,249.31 18,221.93
批发代理 8,423,155.33 9,576,851.10 63,854.83 65,110.83
单位：万元 币种：人民币
线上销售平台 线上销售产品档次 本期销售收入 上期销售收入 同比（%） 毛利率（%）
“i 茅台”数
字营销平台
中、高档酒 1,303,141.15 2,002,366.62 -34.92 94.47
其他线上平台 中、高档酒 40,619.50 209,561.85 -80.62 94.81""",
}
DIVIDENDS = {
    "id": "dividends", "title": "示例公司2025年年度报告 · 第33页",
    "text": """单位：元 币种：人民币
每 10 股派息数（元）（含税） 279.93
现金分红金额（含税） 35,032,568,759.73
合并报表中归属于上市公司普通股股东的净利
润
82,320,067,101.68
现金分红金额占合并报表中归属于上市公司普
通股股东的净利润的比率（%）
42.56
以现金方式回购股份计入现金分红的金额 6,120,088,960.04
合计分红金额（含税） 71,153,295,260.53
合计分红金额占合并报表中归属于上市公司普
通股股东的净利润的比率（%）
86.43""",
}


def check(text, source=CHANNELS):
    return check_text(text, source_amounts(source))


@pytest.mark.parametrize("text,expected", [
    ("2025年直销84.54亿元", "845.43亿元"),
    ("2025年直营84.54亿元", "845.43亿元"),
    ("2025年批发代理84.23亿元", "842.32亿元"),
    ("i茅台收入13.03亿元，同比下降", "130.31亿元"),
    ("其他线上平台0.41亿元", "4.06亿元"),
])
def test_real_channel_conversions_rejected(text, expected):
    findings = check(text)
    assert len(findings) == 1
    assert findings[0]["expected"] == expected
    assert findings[0]["factor"] == "10"
    assert findings[0]["source_id"] == "channels"


def test_dividend_proposal_per_share_and_currency_totals_separated():
    text = ("末期拟每股派27.993元，合计35.03亿元；中期已派300.01亿元；"
            "2025年回购6.12亿元。公司披露合计现金分红+回购71.15亿元，占归母净利86.43%。")
    findings = check(text, DIVIDENDS)
    assert [f["expected"] for f in findings] == ["350.33亿元", "61.20亿元", "711.53亿元"]
    assert not any("27.993" in f["claim"] or "300.01" in f["claim"] for f in findings)


def test_interim_plus_final_dividend_does_not_make_later_buyback_half_year():
    text = ('截至2026-10-15收盘价1312.97元；2025年EPS 65.66元。'
            '2025年宣告中期+末期现金分红合计650.34亿元（含末期预案），回购6.12亿元，'
            '对应当前市值约4%量级。')
    findings = check(text, DIVIDENDS)
    assert len(findings) == 1
    assert findings[0]['claim'] == '6.12亿元' and findings[0]['expected'] == '61.20亿元'


@pytest.mark.parametrize("text", [
    "2025年直销845.43亿元、批发代理842.32亿元；i茅台收入130.31亿元，其他线上平台4.06亿元。",
    "2025年直销845.4亿元，i茅台约130亿元。",
    "2025年直销8,454,303.19万元。",
    "2025年直销84,543,031,900元。",
    "2024年直销748.43亿元，2024年批发代理957.69亿元。",
    "直销同比增长12.96%，毛利率94.58%，销量21249.31吨。",
    "预计直销84.54亿元，假设i茅台收入13.03亿元。",
    "下一期直销84.54亿元。直销增量84.54亿元。",
    "2026年直销84.54亿元。",
    "2025年上半年直销84.54亿元。",
    "其他公司的直销渠道收入84.54亿元。",
])
def test_rounding_units_other_periods_and_explicit_estimates_not_flagged(text):
    assert not check(text)


def test_powers_of_ten_detection_not_hardcoded_to_company_or_amount():
    source = {"id": "other", "title": "另一企业2024年年度报告",
              "text": "单位：万元\n海运业务 2,468,034.71 1,358,642.91"}
    assert check("2024年海运业务24.68亿元", source)[0]["expected"] == "246.80亿元"
    assert not check("2024年海运业务246.80亿元", source)


def test_correct_corresponding_label_prevents_unrelated_amount_false_alarm():
    source = {"id": "rows", "title": "2025年年度报告",
              "text": "单位：万元\n海运业务 246,803.47 135,864.29\n航空业务 2,468,034.71 1,358,642.91"}
    assert not check("2025年海运业务24.68亿元", source)


@pytest.mark.parametrize("label,claim", [
    ("海运业务", "航空业务"), ("营业收入", "营业成本"),
    ("管理费用", "销售费用"), ("经营现金流", "投资现金流"),
])
def test_shared_generic_words_do_not_match_different_items(label, claim):
    source = {"id": "different", "title": "2025年年度报告",
              "text": f"单位：万元\n{label} 2,468,034.71"}
    assert not check(f"2025年{claim}24.68亿元", source)


def test_repurchase_accounting_label_is_not_cash_dividend():
    assert not check("2025年现金分红6.12亿元", DIVIDENDS)


def test_same_sentence_each_amount_matches_own_label():
    source = {"id": "different", "title": "2025年年度报告",
              "text": "单位：万元\n海运业务 2,468,034.71\n航空业务 246,803.47"}
    assert not check("2025年海运业务246.80亿元，航空业务24.68亿元。", source)


def test_non_currency_table_resets_previous_unit():
    source = {"id": "shares", "title": "2025年年度报告",
              "text": "单位：万元\n海运业务 2,468,034.71\n单位：股\n总股本 125,227.02"}
    assert not check("总股本0.13亿元", source)
    assert all("股本" not in v.label for v in source_amounts(source))


def test_half_year_context_not_lost_across_sentences():
    assert not check("2025年上半年直销收入84.54亿元。直销收入84.54亿元值得分析。")


def test_explicit_columns_override_inferred_year():
    source = {"id": "columns", "title": "2025年年度报告",
              "text": "单位：万元\n项目 2024年度 2023年度\n海运业务 2,468,034.71 1,358,642.91"}
    assert not check("2025年海运业务24.68亿元", source)
    assert check("2024年海运业务24.68亿元", source)[0]["expected"] == "246.80亿元"


def test_cost_column_is_not_mistaken_for_previous_year_revenue():
    source = {"id": "mixed-columns", "title": "2025年年度报告",
              "text": "单位：万元\n项目 营业收入 营业成本 营业收入比上年增减\n海运业务 2,468,034.71 1,358,642.91"}
    assert not check("2024年海运业务13.59亿元", source)
    assert not check("海运业务13.59亿元", source)
    assert check("2025年海运业务24.68亿元", source)[0]["expected"] == "246.80亿元"


def test_multiple_inline_amounts_do_not_inherit_the_first_label():
    source = {"id": "prose", "title": "2025年年度报告",
              "text": "营业收入2,468,000,000元，营业成本246,800,000元"}
    assert not check("营业成本2.468亿元", source)
    assert not source_amounts(source)


def test_unknown_currency_unit_or_year_not_guessed():
    source = {"id": "unknown", "title": "公司报表", "text": "海运业务 2,468,034.71 1,358,642.91"}
    assert not check("2024年海运业务24.68亿元", source)
    source["text"] = "单位：万元\n" + source["text"]
    assert not check("2024年海运业务24.68亿元", source)


def test_report_checks_only_cited_sources_and_preserves_input():
    run = {"research_sources": [CHANNELS, DIVIDENDS]}
    memo = {"summary": "核心变化需分析", "theses": [], "sections": [
        {"key": "business", "body": "2025年直销84.54亿元", "evidence_ids": ["channels"]}],
        "question_answers": [{"answer": "2025年回购6.12亿元", "bindings": [{"source_id": "dividends"}]}]}
    before = deepcopy((memo, run))
    findings = memo_findings(memo, run)
    assert {f["field"] for f in findings} == {"sections[0].body", "question_answers[0].answer"}
    with pytest.raises(ValueError, match="万元÷10000"):
        assert_memo_amounts(memo, run)
    assert (memo, run) == before
    memo["sections"][0]["evidence_ids"] = ["not-read"]
    assert len(memo_findings(memo, run)) == 1


def test_ambiguous_totals_not_arbitrarily_matched():
    source = {"id": "ambiguous", "title": "2025年年度报告",
              "text": "单位：万元\n海运业务 246,800.00\n航空业务 2,468,000.00"}
    assert not check("合计0.25亿元", source)


def test_validate_memo_rejects_conversion_before_report_can_be_saved(monkeypatch):
    from app import deep_research
    run = {"id": "unit-test", "request": {"year": 2025, "as_of": "2026-10-15"},
           "facts": [], "calculations": [], "evidence": [], "research_sources": [
               CHANNELS | {"kind": "pdf", "published_at": "2026-04-17", "read_status": "pdf_text", "url": "https://example.test/report.pdf"}]}
    item = {"title": "渠道", "body": "2025年直销84.54亿元", "counter_evidence": "还需渠道调研", "invalidate_if": "分项不一致", "status": "supported", "evidence_ids": ["channels"]}
    memo = {"title": "报告", "summary": "分渠道变化", "stance": "继续研究", "theses": [item, deepcopy(item)],
            "sections": [{"key": k, "body": "仍需研究", "status": "gap", "evidence_ids": []} for k in deep_research.SECTION_TITLES], "question_answers": []}
    with pytest.raises(ValueError, match="845.43亿元"):
        deep_research.validate_memo(memo, run)
    next(s for s in memo['sections'] if s['key'] == 'catalysts')['body'] = '2026年春节旺季动销。'
    with pytest.raises(ValueError) as error:
        deep_research.validate_memo(memo, run)
    assert '845.43亿元' in str(error.value) and '报告时点校验' in str(error.value)
