"""Regression checks for bounded evidence, failed generations and PDF indexing."""
import copy
import json
import sqlite3

import pytest

from app import deep_research as deep, documents, questions, store
from app.providers import ProviderError


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(deep.competition, "POLICY", {"enabled": False})
    value = store.create_run({"ticker": "601899.SH", "year": 2025, "as_of": "2026-10-14",
                              "mode": "live", "model_profile": "stub", "question": "盈利与现金回收"})
    value.update(company="紫金矿业", state="completed_with_gaps",
                 memo={"title": "已有报告", "summary": "需要保留的研究结论", "theses": [], "limitations": []},
                 research_sources=[{"id": "web-business", "kind": "web", "title": "经营披露",
                                    "url": "https://example.com/business", "text": "已取得的经营披露原文",
                                    "published_at": "2026-09-01", "read_status": "body_read"}])
    store.save_run(value)
    return value


def failure_diagnostics():
    return {"model": "stub", "status": "length_exhausted", "requested_output_tokens": 7800,
            "actual_limit": 49152, "attempt_count": 2,
            "usage_total": {"completion_tokens": 81920},
            "attempts": [{"attempt": 1, "actual_limit": 32768, "finish_reason": "length",
                          "usage": {"completion_tokens": 32768}},
                         {"attempt": 2, "actual_limit": 49152, "finish_reason": "length",
                          "usage": {"completion_tokens": 49152}}]}


def isolated_build_inputs(monkeypatch):
    monkeypatch.setattr(documents, "attached_documents", lambda _: [
        {"id": "annual", "ticker": "601899.SH", "year": 2025, "title": "2025年年度报告", "page_count": 100}])
    monkeypatch.setattr(documents, "ensure_index", lambda _: [])
    monkeypatch.setattr(documents, "search_attached", lambda *args, **kwargs: {"snippets": []})
    monkeypatch.setattr(deep.web_research, "search", lambda queries, **kwargs: {"queries": queries, "results": [], "limitations": []})


def test_generation_evidence_has_disjoint_ids_shared_excerpt_budget_and_no_mutation(run):
    run["research_sources"] = [{"id": f"web-{i}", "kind": "web", "title": f"材料{i}",
                                "url": f"https://example.com/{i}", "text": f"正文{i}" * 3000,
                                "published_at": "2026-09-01", "read_status": "body_read"}
                               for i in range(30)]
    run["evidence"] = [{"id": f"raw-{i}", "kind": "api", "quote": "原始记录" * 1500,
                        "announced_date": "2026-04-30", "source_url": "https://example.com/filing"}
                       for i in range(8)]
    run["facts"] = [{"id": "revenue-2025", "label": "营业收入", "year": 2025, "metric": "revenue",
                     "value": "100", "status": "api_only", "evidence_ids": ["raw-0"]}]
    original = copy.deepcopy(run)
    sources, extra = deep.generation_evidence(run)
    source_ids, extra_ids = [s["id"] for s in sources], [s["id"] for s in extra]
    assert len(source_ids) == len(set(source_ids))
    assert len(extra_ids) == len(set(extra_ids))
    assert set(source_ids).isdisjoint(extra_ids) and extra
    assert sum(len(s["text"]) for s in sources + extra) == 65000
    assert next(s for s in sources if s["id"] == "revenue-2025")["text"]
    assert all("quote" not in s and "history" not in s for s in extra)
    assert all("eligibility" in s for s in sources + extra)
    assert run == original


def test_generation_evidence_excludes_future_sources_from_both_catalogs(run):
    run["research_sources"].append({"id": "future-web", "kind": "web", "title": "未来公告",
                                    "url": "https://example.com/future", "text": "未来信息",
                                    "published_at": "2026-10-15", "read_status": "body_read"})
    run["evidence"] = [{"id": "future-raw", "kind": "api", "quote": "未来原始记录",
                        "announced_date": "2026-10-15"}]
    sources, extra = deep.generation_evidence(run)
    assert {s["id"] for s in sources + extra}.isdisjoint({"future-web", "future-raw"})


def test_question_context_preserves_decisions_but_excludes_history_and_is_detached(run):
    active = questions.create("矿产铜产量增长是否改善现金回收？", "查阅分项现金数据", "high")
    active.update(answer="仍缺乏足够资料", status="partial", next_action="继续查阅附注",
                  bindings=[{"source_id": "web-business", "quote": "已取得的经营披露原文", "relation": "support"}],
                  counter_evidence="价格波动可能抵消产量增长", gaps=["缺分矿山数据"], needs_review=True,
                  history=[{"answer": "不应发送的旧分析" * 1000}], edited_by="human", revision=9)
    archived = questions.create("归档问题", priority="low")
    archived["archived"] = True
    run["research_questions"] = [active, archived]
    original = copy.deepcopy(run)
    context = deep.question_context(run)
    assert len(context) == 2 and context[1]["archived"] is True
    for field in ("id", "question", "completion_criteria", "priority", "status", "answer", "bindings",
                  "counter_evidence", "gaps", "next_action", "needs_review"):
        assert context[0][field] == active[field]
    assert all("history" not in q and "revision" not in q and "edited_by" not in q for q in context)
    context[0]["bindings"][0]["quote"] = "只改输出副本"
    assert run == original


@pytest.mark.parametrize("operation,prior_calls", [("build", 0), ("build", 2), ("refine", 0), ("refine", 1)])
def test_failed_model_usage_is_retained_without_replacing_report(run, monkeypatch, operation, prior_calls):
    isolated_build_inputs(monkeypatch)
    chat = store.new_conversation(run["id"])
    original = store.get_run(run["id"])
    calls = []
    diagnostics = failure_diagnostics()

    def model(system, payload, *args, **kwargs):
        assert callable(kwargs["cancel_check"]) and isinstance(kwargs["deadline"], (int, float))
        kwargs["cancel_check"]()
        calls.append(payload)
        if len(calls) > prior_calls: raise ProviderError("测试截断未生成报告", copy.deepcopy(diagnostics))
        # A first invalid refine draft triggers validation repair; build planning
        # and follow-up responses use the same deliberately empty schema.
        return {"questions": [], "queries": [], "gaps": []}, {"status": "completed", "usage": {"completion_tokens": 17}}

    monkeypatch.setattr(deep, "model_json", model)
    with pytest.raises(ProviderError, match="测试截断"):
        getattr(deep, operation)(run["id"], "更新报告", chat)
    saved = store.get_run(run["id"])
    attempt = saved["deep_research_attempts"][-1]
    assert len(attempt["calls"]) == prior_calls + 1
    assert attempt["calls"][-1] == diagnostics
    assert "测试截断" in attempt["failure"]["message"]
    for field in ("memo", "memo_revisions", "report_revision", "valuation_history", "research_questions"):
        assert saved[field] == original[field]
    if operation == "refine":
        assert attempt["mode"] == "source_refinement"
        if prior_calls:
            assert attempt["draft"] == {"questions": [], "queries": [], "gaps": []}
            assert "repaired_draft" not in attempt


def test_failure_without_provider_diagnostics_remains_visible(run, monkeypatch):
    chat = store.new_conversation(run["id"])
    def model(*args, **kwargs): raise ProviderError("未配置模型")
    monkeypatch.setattr(deep, "model_json", model)
    with pytest.raises(ProviderError): deep.refine(run["id"], "修订", chat)
    call = store.get_run(run["id"])["deep_research_attempts"][-1]["calls"][-1]
    assert call == {"status": "failed", "error": "未配置模型"}


def test_complete_but_invalid_refine_drafts_are_retained_without_becoming_report(run, monkeypatch):
    chat = store.new_conversation(run["id"])
    drafts = [{"title": "结构不完整的初稿"}, {"title": "结构仍不完整的修订稿"}]
    iterator = iter(drafts)
    monkeypatch.setattr(deep, "model_json", lambda *args, **kwargs: (next(iterator), {"status": "completed"}))
    with pytest.raises(ValueError): deep.refine(run["id"], "修订报告", chat)
    saved = store.get_run(run["id"])
    attempt = saved["deep_research_attempts"][-1]
    assert attempt["draft"] == drafts[0] and attempt["repaired_draft"] == drafts[1]
    assert len(attempt["calls"]) == 2 and "completed_at" not in attempt
    assert saved["memo"] == run["memo"] and saved["report_revision"] == run["report_revision"]


def test_no_chat_annual_index_metadata_writes_before_run_transaction(run, monkeypatch, tmp_path):
    """Exercise the real cached-page index and its real document write."""
    doc = {"id": "downloaded-annual", "ticker": "601899.SH", "year": 2025,
           "title": "2025年年度报告", "announced_date": "2026-04-30", "page_count": 0,
           "source_url": "https://example.com/annual.pdf"}
    store.put_document(doc)
    folder = tmp_path / "documents"
    folder.mkdir()
    (folder / (doc["id"] + ".pages.json")).write_text(json.dumps(["紫金矿业2025年年度报告\n经营情况"]), encoding="utf-8")
    monkeypatch.setattr(deep.disclosures, "fetch_annual", lambda *args: copy.deepcopy(doc))
    monkeypatch.setattr(documents, "search_attached", lambda *args, **kwargs: {"snippets": []})
    monkeypatch.setattr(deep.web_research, "search", lambda queries, **kwargs: {"queries": queries, "results": [], "limitations": []})
    index_writes = []
    real_put = documents.put_document

    def save_index_metadata(value):
        # A separate writer must be able to acquire the SQLite lock now. With the
        # old nested call this fails quickly rather than waiting the normal 15 s.
        with sqlite3.connect(tmp_path / "research.sqlite3", timeout=0.1) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        index_writes.append(copy.deepcopy(value))
        real_put(value)

    monkeypatch.setattr(documents, "put_document", save_index_metadata)
    calls = []
    def model(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1: return {"questions": [], "queries": [], "pdf_queries": []}, {"status": "completed"}
        raise ProviderError("结束无网络的索引验证")
    monkeypatch.setattr(deep, "model_json", model)
    with pytest.raises(ProviderError, match="索引验证"): deep.build(run["id"], "分析自动取得的年报")
    saved = store.get_run(run["id"])
    indexed = store.get_document(doc["id"])
    assert index_writes and indexed["page_count"] == 1 and indexed["index_status"] == "ready"
    assert doc["id"] in saved["document_ids"]
    assert saved["attached_documents"][0]["page_count"] == 1
