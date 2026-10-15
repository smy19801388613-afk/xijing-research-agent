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
