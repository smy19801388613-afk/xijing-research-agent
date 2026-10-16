"""Completed evidence descriptions must not dispatch new reading tools."""
import pytest

from app import agent, resume_plan, store, turn_constraints


@pytest.mark.parametrize('text', [
    '其他已核对的正文和估值保持不变。',
    '保留已经读取的原文，仅修正该句时态。',
    '沿用已完整阅读的全文。',
    '已逐页查阅原文，修改报告标题。',
    '核对过的正文不必再次查阅。',
    '已经核对完的正文保留原样。',
    '不要读取正文，只修订报告。',
])
def test_completed_or_negated_read_does_not_require_new_read(text):
    assert not turn_constraints.constraints(text)['require_body']


@pytest.mark.parametrize('text', [
    '请重新核对已读正文。',
    '请阅读原文后保存报告。',
    '重新读取已核对的正文，检查单位。',
    '已核对的正文保持不变；请读取其他原文。',
    '请核对已经读取的原文。',
    '不要修改报告；请阅读原文。',
    '请继续查阅正文。',
])
def test_affirmative_new_read_still_requires_body(text):
    assert turn_constraints.constraints(text)['require_body']


REAL_EDIT = '''这是一个新的定点编辑任务，以当前已保存的第3版报告为基准，仅修正 catalysts 中业绩说明会的一处时态，不接续前一轮多余循环产生的未保存稿。

报告写“2026年10月9日半年度业绩说明会已举行”，但已读来源 web-47eed8eb8345（http://finance.eastmoney.com/a/202609233882593490.html）仅说：“证券日报网讯9月23日，用友网络发布公告称，公司计划于2026年10月9日16:00-17:00举行2026年半年度业绩说明会。” 日期过去不能证明实际举行。

请将报告该句改为：“9月23日公告原定于2026年10月9日举行半年度业绩说明会，这是历史安排；是否如期举行尚未另行核实，不作为未来催化剂。” 保留这一已读来源，其他已核对的正文和估值保持不变，保存下一版报告。

本轮允许修订保存报告；不更新问题台账，不联网、不补读网页或PDF，不重算估值，不扩大研究范围。完成后只根据实际保存结果简要说明版本与改动，不宣称已经核实该活动举行。'''


def test_full_yonyou_prompt_does_not_force_body_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    r = store.create_run({'ticker':'600588.SH','year':2025,'as_of':'2026-10-16','mode':'sample',
                          'question':'长期研究缺口'})
    r.update(research_sources=[{'id':'old','url':'/api/documents/annual/file#page=93','text':'已有年度原文'}])
    store.save_run(r)
    h = store.new_conversation(r['id'])
    current = store.begin_turn(h['id'], REAL_EDIT, 'stub', 'documents')
    assert not current['turn_contexts'][current['turn_id']]['rules']['require_body']
    assert current['turn_contexts'][current['turn_id']]['rules']['no_network']
    plan = resume_plan.plan(current, store.get_run(r['id']))
    assert plan['pending_obligations'] == []
    assert resume_plan.recovery(current, store.get_run(r['id']), plan) is None
    # The explicit ban on supplementing PDF/web reads is enforced at dispatch.
    assert turn_constraints.prohibited_reason(turn_constraints.constraints(REAL_EDIT), 'read_document')
