from copy import deepcopy
from decimal import Decimal
import pytest
from app import deep_research, finance, store, valuation, questions
from app.report_state import report_valuation_status, valuation_snapshot


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    run = store.create_run({"ticker": "TEST", "year": 2025, "as_of": "2026-10-02"})
    run.update(state="completed", facts=[{"id": "revenue-2025", "metric": "revenue", "year": 2025,
        "value": "1619404301.59", "status": "verified", "label": "营业收入"}])
    store.save_run(run)
    return run


def ps(run, multiple="5", growth="1"):
    return valuation.save_result(run["id"], "ps", {"growth_pct": growth, "multiple": multiple})


def memo(scenario=None):
    return {"summary": "保留原稿 81.78 亿元", "sections": [{"key": "valuation", "body": "原始判断",
        "evidence_ids": ["valuation-" + scenario["id"]] if scenario else []}], "theses": [], "sources": []}


def status(run):
    return report_valuation_status(store.get_run(run["id"]))


def test_changed_ps_keeps_original_report_and_valuation_versions(run):
    old = ps(run)
    deep_research.save_memo(run["id"], memo(old), "初稿")
    before = store.get_run(run["id"])
    new = ps(run, "1.3", "0")
    saved = store.get_run(run["id"])
    assert Decimal(old["output"]["equity_value_100m"]) == Decimal("81.779917230295")
    assert Decimal(new["output"]["equity_value_100m"]) == Decimal("21.05225592067")
    assert saved["memo"] == before["memo"]
    assert saved["memo_revisions"] == before["memo_revisions"]
    assert saved["report_revision"] == before["report_revision"]
    assert [v["id"] for v in saved["valuation_history"]] == [old["id"], new["id"]]
    result = status(run)
    assert result["state"] == "changed" and result["basis"] == "references"
    assert result["items"][0]["reported_ids"] == [old["id"]]
    assert result["items"][0]["latest_id"] == new["id"]
    assert "尚未纳入" in result["message"] and "错误" not in result["message"]


def test_same_parameters_normalized_and_interleaved_methods_do_not_expire(run):
    old = ps(run)
    pb = valuation.save_result(run["id"], "pb", {"book_equity_100m": 10, "multiple": 2})
    deep_research.save_memo(run["id"], memo(old), "初稿")
    repeated = ps(run, "5.00", "1.0")
    assert repeated["id"] == old["id"]
    assert len(store.get_run(run["id"])["valuation_history"]) == 2
    result = status(run)
    assert result["state"] == "current"
    assert {i["latest_id"] for i in result["items"]} == {old["id"], pb["id"]}


def test_new_method_after_snapshot_requires_comparison(run):
    old = ps(run)
    deep_research.save_memo(run["id"], memo(old), "初稿")
    valuation.save_result(run["id"], "pb", {"book_equity_100m": 10, "multiple": 2})
    result = status(run)
    assert {i["method"]: i["state"] for i in result["items"]} == {"ps": "current", "pb": "changed"}


def test_old_report_without_link_is_unknown_and_not_rewritten(run):
    ps(run)
    store.update_run(run["id"], lambda r: r.update(memo=memo()))
    before = store.get_run(run["id"])
    result = report_valuation_status(before)
    assert result["state"] == "unlinked" and "无法确认" in result["message"]
    assert before == store.get_run(run["id"])


def test_legacy_actual_reference_can_be_compared_without_snapshot(run):
    old = ps(run)
    store.update_run(run["id"], lambda r: r.update(memo=memo(old)))
    assert status(run)["state"] == "current"
    ps(run, "1.3")
    assert status(run)["state"] == "changed"


def test_legacy_unreferenced_other_method_stays_unknown(run):
    old = ps(run)
    store.update_run(run["id"], lambda r: r.update(memo=memo(old)))
    valuation.save_result(run["id"], "pb", {"book_equity_100m": 10, "multiple": 2})
    result = status(run)
    assert result["state"] == "unlinked"
    assert {i["method"]: i["state"] for i in result["items"]} == {"ps": "current", "pb": "unlinked"}


def test_snapshot_before_generation_detects_change_during_model_call(run):
    ps(run)
    frozen = valuation_snapshot(store.get_run(run["id"]))
    ps(run, "1.3")
    deep_research.save_memo(run["id"], memo(), "模型返回", valuation_context=frozen)
    assert status(run)["state"] == "changed"
    assert status(run)["basis"] == "snapshot"
    assert len(store.get_run(run["id"])["memo"]["valuation_context"]["scenarios"]) == 1
    assert questions.report_sync(store.get_run(run['id']))['needs_update']


def test_empty_generation_snapshot_detects_first_scenario(run):
    frozen = valuation_snapshot(run)
    deep_research.save_memo(run["id"], memo(), "无估值初稿", valuation_context=frozen)
    assert status(run)["state"] == "no_valuations"
    ps(run)
    assert status(run)["state"] == "changed"


def test_revision_archives_context_and_old_report_unchanged(run):
    old = ps(run)
    deep_research.save_memo(run["id"], memo(old), "初稿")
    saved_memo = store.get_run(run["id"])["memo"]
    new = ps(run, "1.3")
    deep_research.save_memo(run["id"], memo(new), "显式修订")
    saved = store.get_run(run["id"])
    assert saved["memo_revisions"][-1]["memo"] == saved_memo
    assert len(saved["valuation_history"]) == 2
    assert status(run)["state"] == "current"


def test_equivalent_duplicate_historical_ids_do_not_expire(run):
    old = ps(run)
    store.update_run(run["id"], lambda r: r.update(memo=memo(old)))
    equivalent = deepcopy(old)
    equivalent.update(id="legacy-duplicate", assumptions={"growth_pct": "1.0", "multiple": "5.00"})
    store.update_run(run["id"], lambda r: r["valuation_history"].append(equivalent))
    assert status(run)["state"] == "current"


def test_same_assumptions_but_changed_base_is_new_scenario(run):
    old = ps(run)
    deep_research.save_memo(run["id"], memo(old), "初稿")
    store.update_run(run["id"], lambda r: r["facts"][0].update(value="2000000000"))
    assert ps(run)["id"] != old["id"]
    assert status(run)["state"] == "changed"


def test_source_reference_without_section_link_is_supported(run):
    old = ps(run)
    report = memo()
    report["sources"] = [{"kind": "valuation", "id": "valuation-" + old["id"]}]
    store.update_run(run["id"], lambda r: r.update(memo=report))
    assert status(run)["basis"] == "references"


def test_audit_deduplicates_aliases_but_keeps_distinct_occurrences():
    facts = [{"metric": "n_cashflow_act", "year": 2025, "value": "-37000000"},
             {"metric": "n_income_attr_p", "year": 2025, "value": "-195000000"}]
    text = "2025年归母净利润为1.95亿元，经营现金流净额为0.37亿元。经营现金流净额为0.37亿元。"
    issues = finance.audit_text(text, facts, [], 2025)
    assert len(issues) == 3
    cash = [i for i in issues if i["fact_id"] == "n_cashflow_act-2025"]
    assert len(cash) == 2 and cash[0]["span"] != cash[1]["span"]
    assert all(i["claim"] == "经营现金流净额为0.37亿元" for i in cash)


@pytest.mark.parametrize("current,previous,value,change", [
    ("-195000000", "-515000000", None, "320000000"),
    ("-37000000", "-123000000", None, "86000000"),
    ("100", "0", None, "100"), ("120", "100", "20.0", "20"),
    (None, "100", None, None)])
def test_growth_retains_amount_change_for_negative_zero_positive_missing(current, previous, value, change):
    result = finance.growth(current, previous)
    assert result["value"] == value and result.get("change") == change
    assert bool(result["reason"]) == (value is None)
