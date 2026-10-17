import json
from datetime import date
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from app import store, research, documents, main
from app.config import FIXTURES
from app.finance import number,growth,eligible_record,scenario,audit_text
from app.schemas import RunRequest

@pytest.fixture
def isolated(tmp_path,monkeypatch):
    for module in (store,research,main):
        monkeypatch.setattr(module,"DATA",tmp_path)
    documents.seed()
    return tmp_path

def sample():
    run=store.create_run(RunRequest(as_of=date(2026,9,28)).model_dump(mode="json"))
    research.execute(run["id"])
    return store.get_run(run["id"])

def test_sample_e2e(isolated):
    run=sample()
    assert run["state"]=="completed_with_gaps"
    assert run["model"]["status"]=="sample"
    assert len(run["facts"])==6
    expected=json.loads((FIXTURES/"midea-gold.json").read_text(encoding="utf-8"))
    for fact in expected["facts"]:
        for year in (2025,2024):
            actual=next(f for f in run["facts"] if f["metric"]==fact["metric"] and f["year"]==year)
            assert Decimal(actual["value"])==Decimal(fact[f"value_{year}"])*1000
    assert len([f for f in run["facts"] if f["status"]=="verified"])==3
    assert len(run["calculations"])==3
    assert all(c["value"] is not None for c in run["calculations"])
    assert all(e["page"]==5 for e in run["evidence"] if e["kind"]=="document")

def test_before_disclosure_blocks(isolated):
    run=store.create_run(RunRequest(as_of=date(2026,2,1)).model_dump(mode="json"))
    research.execute(run["id"]);result=store.get_run(run["id"])
    assert result["state"]=="blocked" and not result["facts"] and result["memo"] is None

@pytest.mark.parametrize("value",[None,False,True,"","NaN","Infinity","not a number"])
def test_missing_never_zero(value):
    assert number(value) is None

@pytest.mark.parametrize("previous",[0,-100,None])
def test_invalid_yoy_base(previous):
    assert growth(10,previous)["value"] is None

def test_negative_cashflow_sign():
    assert Decimal(growth(50,60)["value"])<0

def test_asof_and_report_scope():
    base={"end_date":"20251231","f_ann_date":"20260331","report_type":"1","revenue":10}
    assert eligible_record([base],2025,"2026-03-30") is None
    assert eligible_record([base|{"report_type":"6"}],2025,"2026-09-28") is None
    assert eligible_record([base],2025,"2026-03-31")==base
    with pytest.raises(ValueError):
        eligible_record([base,base|{"revenue":99}],2025,"2026-09-28")

def test_missing_shares_no_price():
    a=scenario(100000000,10,15)
    assert a["price"] is None and Decimal(a["equity_value_100m"])==Decimal("16.5")
    b=scenario(100000000,10,15,2)
    assert Decimal(b["price"])==Decimal("8.25")
    with pytest.raises(ValueError): scenario(-1,5,15)
    with pytest.raises(ValueError): scenario(100,5,15,0)

def test_cross_source_conflict(isolated):
    run=sample()
    row={"revenue":1,"n_income_attr_p":43945411000,"ann_date":"20260331"}
    research.add_api_facts(run,"income",2025,row,"today")
    assert next(f for f in run["facts"] if f["id"]=="revenue-2025")["status"]=="conflict"
    assert any(g["code"]=="conflict-revenue-2025" for g in run["gaps"])
    assert next(c for c in research.compute(run["facts"],2025) if c["metric"]=="revenue")["value"] is None

def test_error_injection_and_control(isolated):
    run=sample()
    bad="营业收入4585.02亿元，归母净利润412.67亿元，经营现金流同比增长11.84%，因此盈利质量全面改善。现金流的绝对值也比利润小。"
    errors=audit_text(bad,run["facts"],run["calculations"],2025)
    assert {e["type"] for e in errors}=={"value_mismatch","direction_mismatch","unsupported_conclusion","relation_mismatch"}
    good="营业收入4564.52亿元，归母净利润439.45亿元，经营现金流净额533.46亿元，同比下降11.84%。原因需要进一步调查。"
    assert audit_text(good,run["facts"],run["calculations"],2025)==[]

def test_model_hallucinated_reference_rejected(isolated):
    run=sample()
    with pytest.raises(ValueError):
        research.valid_memo({"summary":"结论","theses":[{"fact_ids":["invented"]}]},run)

def test_model_calculation_references_resolve_to_inputs(isolated):
    run=sample()
    memo=research.valid_memo({"summary":"现金流变化需要进一步核实。","theses":[{"title":"调查线索","body":"尚缺营运资本证据。","fact_ids":["yoy-n_cashflow_act"],"counter_evidence":"结算时点可能影响现金流。","invalidate_if":"取得现金流附注后核实并修订。"}]},run)
    assert memo["theses"][0]["fact_ids"]==["n_cashflow_act-2025","n_cashflow_act-2024"]
    assert memo["theses"][0]["calculation_ids"]==["yoy-n_cashflow_act"]

def test_different_year_amount_not_checked_as_current(isolated):
    run=sample()
    assert not audit_text("2024年营业收入4071.50亿元。2025年营业收入4564.52亿元。",run["facts"],run["calculations"],2025)

def test_pdf_negative_value_and_issuer_check(isolated,monkeypatch):
    doc=store.get_document(documents.SAMPLE_ID)
    pages=["000333 美的集团2025年年度报告摘要\n主要会计数据\n单位：千元\n2025年 2024年\n经营活动产生的现金流量净额 -100 -200"]
    monkeypatch.setattr(documents,"pages_for",lambda _:pages)
    facts,_,_=documents.extract(doc,"000333.SZ",2025,"2026-09-28")
    assert [f["value"] for f in facts]==["-100000","-200000"]
    monkeypatch.setattr(documents,"pages_for",lambda _:["2025年年度报告","其他内容","000333"])
    with pytest.raises(ValueError): documents.extract(doc,"000333.SZ",2025,"2026-09-28")

def test_cancel_before_work(isolated):
    run=store.create_run(RunRequest().model_dump(mode="json"))
    store.cancel_run(run["id"]);research.execute(run["id"])
    assert store.get_run(run["id"])["state"]=="cancelled"

def test_one_bounded_model_repair(isolated,monkeypatch):
    run=sample()
    narrative={"title":"调查线索","body":"经营现金流变化仍需附注明细。","fact_ids":["n_cashflow_act-2025"],"counter_evidence":"结算时点也可能造成差异。","invalidate_if":"取得营运资本桥接后重新判断。"}
    bad={"title":"草稿","summary":"第四季度现金流为负。","theses":[narrative]}
    good={"title":"草稿","summary":"年度现金流变化需进一步核实。","theses":[narrative],"questions":[]}
    replies=iter([{"focus":"核查","queries":["现金流量"]},bad,good])
    calls=[]
    def fake_model(*args):
        calls.append(args)
        return next(replies),{"model":"test-only","usage":{"total_tokens":1}}
    monkeypatch.setattr(research,"model_json",fake_model)
    research.model_research(run,[],None)
    assert len(calls)==3 and run["model"]["status"]=="completed"
    assert run["memo"]["summary"]==good["summary"]
    assert any(v["label"]=="修订研究草稿" for v in run["events"])

def test_model_failure_keeps_financial_facts(isolated,monkeypatch):
    run=sample();original=run["facts"].copy()
    def unavailable(*args): raise research.ProviderError("模拟接口超时")
    monkeypatch.setattr(research,"model_json",unavailable)
    research.model_research(run,[],None)
    assert run["model"]["status"]=="failed" and run["facts"]==original
    assert run["memo"]["origin"]=="deterministic"
    assert any(v["code"]=="model_unavailable" for v in run["gaps"])

def test_api_export_and_validation(isolated,monkeypatch):
    monkeypatch.setattr(main,"launch",lambda run:run)
    with TestClient(main.app) as c:
        assert c.get("/api/health").status_code==200
        assert c.post("/api/runs",json={"ticker":"../../etc"}).status_code==422
        assert c.post("/api/runs",json={},headers={"Origin":"https://untrusted.invalid"}).status_code==403
        run=sample()
        for fmt in ("markdown","json","html","bundle"):
            result=c.get(f"/api/runs/{run['id']}/export?format={fmt}")
            assert result.status_code==200 and len(result.content)>500
            if fmt=="html": assert "<table" in result.text and "<thead>" in result.text and "<!doctype html>" in result.text
            if fmt=="markdown": assert "|\n|---" in result.text and "|\n\n|" not in result.text
        result=c.post(f"/api/runs/{run['id']}/scenario",json={"growth_pct":10,"pe":15})
        assert result.status_code==200 and result.json()["output"]["price"] is None
        assert c.get("/api/documents/missing/file").status_code==404
        preview=c.get(f"/api/runs/{run['id']}/export?format=html&preview=true")
        assert preview.headers["content-disposition"].startswith("inline") and "<table" in preview.text and "<thead>" in preview.text

def test_upload_and_render_original_page(isolated):
    with TestClient(main.app) as c:
        content=(FIXTURES/"midea-2025-annual-summary.pdf").read_bytes()
        metadata={"ticker":"000333.SZ","year":"2025","announced_date":"2026-03-31"}
        result=c.post("/api/documents",data=metadata,files={"file":("annual.pdf",content,"application/pdf")})
        assert result.status_code==200 and "path" not in result.json()
        doc_id=result.json()["id"]
        assert c.get(f"/api/documents/{doc_id}/file").content==content
        assert c.get(f"/api/documents/{doc_id}/pages/5").headers["content-type"]=="image/png"
        assert c.get(f"/api/documents/{doc_id}/pages/999").status_code==404
        invalid=c.post("/api/documents",data=metadata,files={"file":("fake.pdf",b"plain text","application/pdf")})
        assert invalid.status_code==400
