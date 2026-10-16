import pytest
from app.report_dates import past_catalysts, assert_catalyst_dates


def memo(body, key='catalysts'):
    return {'sections':[{'key':key, 'body':body, 'status':'gap'}]}


def test_elapsed_spring_festival_is_not_a_future_catalyst():
    candidate = memo('可验证催化事件：2026年三季报（预计10月下旬，以公告为准）；2026年春节旺季动销、批价和渠道库存。')
    issues = past_catalysts(candidate, '2026-10-15')
    assert len(issues) == 1 and '2026年春节' in issues[0]
    with pytest.raises(ValueError, match='报告时点校验'):
        assert_catalyst_dates(candidate, {'request':{'as_of':'2026-10-15'}})


@pytest.mark.parametrize('text', [
    '未来关注2027年春节的实际动销，时间待核对。',
    '预计2026年10月下旬披露第三季度报告，以公告为准。',
    '2026年春节已经结束，后续需核验实际库存。',
    '回顾2026年春节旺季表现，关注下一期正式披露。',
    '2025年年度股东大会的日期尚未核实，不能推定具体召开日期。',
    '2026年6月26日已实施分红，未来分红政策仍待核实。',
])
def test_future_or_explicit_historical_observations_are_allowed(text):
    assert not past_catalysts(memo(text), '2026-10-15')


def test_explicit_past_dates_rejected_only_in_catalysts():
    assert past_catalysts(memo('关注2026-06-26现金分红到账。'), '2026-10-15')
    assert past_catalysts(memo('下一催化剂为2026年6月的现金分红。'), '2026-10-15')
    assert not past_catalysts(memo('2026-06-26现金分红到账。', 'financial_quality'), '2026-10-15')
    assert not past_catalysts(memo('关注2026-10-15当天的公告。'), '2026-10-15')


def test_real_dated_disclosure_quote_is_historical_even_when_label_is_distant():
    body = ('相对2026-10-16，可观察事件包括：2026年三季报（具体披露日未取得）及后续定期报告；'
            '观察2026年累计经营现金流是否收窄或转正、ARR和订阅合同负债变化；'
            'AI合同签约向收入转化；资本化项目YonBIP2025转为无形资产后是否产生收入并覆盖摊销；'
            '研发费用与费用化投入差异的调节披露。2026年10月9日半年度业绩说明会已举行，属历史复盘，不再作为未来催化。'
            '分红方面，2026-04-18年报摘要中“无分红预案尚待2025年年度股东会审议”只是历史时点信息；'
            '截至2026-10-16未查到最新决议，不能表述为目前仍待审议。')
    assert not past_catalysts(memo(body), '2026-10-16')


@pytest.mark.parametrize('body', [
    '2026-04-18股东会不能表述为目前仍待审议。',
    '2026-04-18年度报告中“本次利润分配方案仍需股东大会进行审议”仅为历史披露。',
])
def test_explicitly_denied_upcoming_status_is_not_a_future_catalyst(body):
    assert not past_catalysts(memo(body), '2026-10-16')


@pytest.mark.parametrize('body', [
    '预计2026-04-18年报摘要中所述分红落地，引用内容只是历史时点信息。',
    '2026-04-18年报摘要中提及的分红仍待落地，相关说明文字只是历史时点信息。',
    '下一催化是2026-04-18分红；2026-05-10年报摘要中所述方案只是历史时点信息。',
])
def test_historical_disclosure_cannot_hide_an_affirmative_elapsed_catalyst(body):
    assert any('2026-04-18' in issue for issue in past_catalysts(memo(body), '2026-10-16'))


def test_original_plan_with_unknown_occurrence_is_historical_not_upcoming():
    body = ('9月23日公告原定于2026年10月9日举行半年度业绩说明会，这是历史安排；'
            '是否如期举行尚未另行核实，不作为未来催化剂。')
    assert not past_catalysts(memo(body), '2026-10-16')
    assert past_catalysts(memo('业绩说明会定于2026年10月9日举行，这是历史安排。'), '2026-10-16')
