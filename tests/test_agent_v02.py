import json
from datetime import date
from decimal import Decimal
import httpx
import pytest
from fastapi.testclient import TestClient
from app import store,research,documents,connections,securities,agent,valuation,main,providers
from app.schemas import RunRequest,ProfileRequest
from app.data_sources import Eastmoney

@pytest.fixture
def isolated(tmp_path,monkeypatch):
    for module in (store,research,main,connections,securities,agent):
        monkeypatch.setattr(module,"DATA",tmp_path)
    monkeypatch.setattr(securities,"last_attempt",0)
    documents.seed()
    return tmp_path

@pytest.fixture
def run(isolated):
    r=store.create_run(RunRequest(as_of=date(2026,9,28)).model_dump(mode="json"))
    research.execute(r["id"])
    return store.get_run(r["id"])

@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows DPAPI integration requires Windows")
def test_secrets_encrypted_and_preserved(isolated):
    p=ProfileRequest(id="test",name="Test",provider="custom",base="https://example.com/v1",model="demo",api_key="not-a-real-secret").model_dump()
    public=connections.save_profile(p)
    assert "not-a-real-secret" not in json.dumps(public)
    assert "not-a-real-secret" not in (isolated/"connections.json").read_text()
    assert connections.resolve_model("test")["key"]=="not-a-real-secret"
    connections.save_profile(p|{"api_key":"","name":"Renamed"})
    assert connections.resolve_model("test")["key"]=="not-a-real-secret"
    with pytest.raises(ValueError): connections.save_profile(p|{"api_key":"","base":"https://other.example/v1"})
    connections.save_profile(p|{"api_key":"","clear_key":True,"base":"https://other.example/v1"})
    assert connections.resolve_model("test")["key"]==""
    connections.save_preferences({"tushare_token":"local-token"})
    assert connections.runtime_settings()["token"]=="local-token"
    assert "local-token" not in json.dumps(connections.public_settings())
    connections.save_preferences({"clear_tushare":True})
    assert connections.runtime_settings()["token"]==""

@pytest.mark.parametrize("address",["http://example.com","https://user:pass@example.com","https://example.com?key=secret","file:///tmp"])
def test_invalid_connection_addresses(address):
    with pytest.raises(ValueError): connections.validate_base(address)

@pytest.mark.parametrize("provider,protocol",[(p,"openai") for p in ("openai","deepseek","qwen","ollama","custom")]+[("anthropic","anthropic")])
def test_provider_protocols(monkeypatch,provider,protocol):
    cfg={"provider":provider,"protocol":protocol,"key":"test-key","id":"test","base":"https://example.com/v1","model":"test-model","json_mode":True}
    monkeypatch.setattr(connections,"resolve_model",lambda _:cfg)
    seen={}
    def send(url,**kwargs):
        seen.update(url=url,**kwargs)
        body={"content":[{"type":"text","text":'{"ok":true}'}],"stop_reason":"end_turn"} if protocol=="anthropic" else {"choices":[{"message":{"content":'{"ok":true}'},"finish_reason":"stop"}]}
        return httpx.Response(200,json=body,request=httpx.Request("POST",url))
    monkeypatch.setattr(providers.httpx,"post",send)
    value,meta=providers.model_json("test",{},profile_id="test")
    assert value["ok"] and meta["connection_id"]=="test"
    if protocol=="anthropic":
        assert seen["url"].endswith("/messages") and seen["headers"]["x-api-key"]=="test-key"
        assert "response_format" not in seen["json"]
    else:
        assert seen["url"].endswith("/chat/completions")
        assert ("max_completion_tokens" if provider=="openai" else "max_tokens") in seen["json"]
        assert ("thinking" in seen["json"])==(provider=="deepseek")

def test_search_names_pinyin_codes_and_stale_cache(isolated,monkeypatch):
    rows=[{"ts_code":"000333.SZ","name":"美的集团","cnspell":"MDJT","symbol":"000333"},{"ts_code":"600690.SH","name":"海尔智家","cnspell":"HEZJ","symbol":"600690"}]
    monkeypatch.setattr(securities,"tushare",lambda *a:rows)
    for query in ("美的","mdjt","000333","美地集团"):
        assert securities.search(query)["items"][0]["ts_code"]=="000333.SZ"
    monkeypatch.setattr(securities,"last_attempt",0)
    monkeypatch.setattr(securities,"tushare",lambda *a:(_ for _ in ()).throw(providers.ProviderError("unavailable")))
    result=securities.search("海尔",refresh=True)
    assert result["stale"] and result["items"][0]["name"]=="海尔智家"

@pytest.mark.parametrize("method,inputs,field,expected",[
    ("pe",{"growth_pct":10,"multiple":15},"equity_value_100m","7250.992815"),
    ("ps",{"growth_pct":0,"multiple":1},"equity_value_100m","4564.51731"),
    ("pb",{"book_equity_100m":200,"multiple":2},"equity_value_100m","400"),
    ("ev_ebitda",{"ebitda_100m":20,"multiple":8,"equity_bridge_100m":30},"equity_value_100m","130"),
    ("ddm",{"dividend_per_share":2,"cost_of_equity_pct":10,"terminal_growth_pct":2},"price","25.5"),
])
def test_valuation_arithmetic(run,method,inputs,field,expected):
    result=valuation.calculate(run,method,inputs)
    assert Decimal(result["output"][field])==Decimal(expected)
    if method!="ddm": assert result["output"]["price"] is None

def test_dcf_bridge_missing_and_gordon_boundary(run):
    args={"base_fcff_100m":100,"growth_pct":0,"years":5,"wacc_pct":10,"terminal_growth_pct":0}
    a=valuation.calculate(run,"dcf",args)
    assert abs(Decimal(a["output"]["enterprise_value_100m"])-1000)<Decimal("0.00000001")
    assert a["output"]["equity_value_100m"] is None and a["output"]["price"] is None
    b=valuation.calculate(run,"dcf",args|{"equity_bridge_100m":100,"shares_100m":10})
    assert abs(Decimal(b["output"]["price"])-90)<Decimal("0.00000001")
    for changed in ({"terminal_growth_pct":10},{"years":2.5},{"base_fcff_100m":"NaN"},{"wacc_pct":0}):
        with pytest.raises(ValueError): valuation.calculate(run,"dcf",args|changed)
    with pytest.raises(ValueError): valuation.calculate(run,"dcf",{"growth_pct":10})

def test_conflicting_fact_cannot_be_used_for_pe(run):
    next(f for f in run["facts"] if f["id"]=="n_income_attr_p-2025")["status"]="conflict"
    with pytest.raises(ValueError): valuation.calculate(run,"pe",{"growth_pct":0,"multiple":15})

def test_new_versions_and_transaction_rollback(run):
    a=valuation.save_result(run["id"],"pe",{"growth_pct":10,"multiple":15})
    b=valuation.save_result(run["id"],"pe",{"growth_pct":10,"multiple":12})
    assert a["id"]!=b["id"]
    assert len(store.get_run(run["id"])["valuation_history"])==2
    with pytest.raises(ValueError): valuation.save_result(run["id"],"pe",{"growth_pct":10,"multiple":-1})
    assert len(store.get_run(run["id"])["valuation_history"])==2

def test_documents_only_does_not_call_financial_provider(isolated,monkeypatch):
    monkeypatch.setattr(research,"tushare",lambda *a:pytest.fail("documents mode called Tushare"))
    r=store.create_run(RunRequest(mode="live",data_source="documents",use_model=False).model_dump(mode="json"))
    research.execute(r["id"]);saved=store.get_run(r["id"])
    assert saved["state"]=="completed_with_gaps" and len(saved["facts"])==6
    assert all(f["status"]=="document_only" for f in saved["facts"])

def test_public_adapter_keeps_revision_cutoff(monkeypatch):
    def get(url,**kwargs):
        if "Index" in url:
            return httpx.Response(200,text='<input id="hidctype" type="hidden" value="4" />',request=httpx.Request("GET",url))
        return httpx.Response(200,json={"data":[{"SECURITY_CODE":"000333","SECURITY_NAME_ABBR":"美的集团","REPORT_DATE":"2025-12-31",
            "NOTICE_DATE":"2026-03-31","UPDATE_DATE":"2026-08-29","OPERATE_INCOME":456451731000,"PARENT_NETPROFIT":43945411000}]},request=httpx.Request("GET",url))
    monkeypatch.setattr(httpx,"get",get)
    rows=Eastmoney("000333.SZ").statement("income",2025)
    assert research.eligible_record(rows,2025,"2026-08-28") is None
    assert research.eligible_record(rows,2025,"2026-08-29")["revenue"]==456451731000

def test_public_source_never_falls_through_to_tushare(isolated,monkeypatch):
    class Fake:
        def __init__(self,ticker): pass
        def statement(self,api,year): return [{"end_date":str(year)+"1231","ann_date":"20260331","report_type":"1","name":"美的集团","_source":"eastmoney","revenue":456451731000 if year==2025 else 407149600000,"n_income_attr_p":43945411000 if year==2025 else 38537237000,"n_cashflow_act":53345930000 if year==2025 else 60511572000}]
    monkeypatch.setattr("app.data_sources.Eastmoney",Fake)
    monkeypatch.setattr(research,"tushare",lambda *a:pytest.fail("public mode called Tushare"))
    r=store.create_run(RunRequest(mode="live",data_source="eastmoney",use_model=False).model_dump(mode="json"))
    research.execute(r["id"]);saved=store.get_run(r["id"])
    assert len([f for f in saved["facts"] if f["status"]=="verified"])==6
    assert any(e["id"].startswith("eastmoney-") for e in saved["evidence"])

def test_conversation_executes_tool_and_persists_reply(run,monkeypatch):
    chat=store.new_conversation(run["id"])
    chat=store.begin_turn(chat["id"],"按 PE 15 倍、增长 10% 计算。","test","tushare")
    with pytest.raises(ValueError): store.begin_turn(chat["id"],"another","test","tushare")
    decisions=iter([
        {"action":"tool","tool":"calculate_valuation","arguments":{"method":"pe","assumptions":{"multiple":15,"growth_pct":10},"assumption_quotes":{"multiple":"PE 15 倍","growth_pct":"增长 10%"}},"message":"计算已确认假设"},
        {"action":"respond","message":"已计算并保存情景，股本未提供，因此没有每股价值。"}
    ])
    monkeypatch.setattr(agent,"model_json",lambda *a,**kw:(next(decisions),{"model":"test"}))
    agent.execute(chat["id"])
    saved=store.get_conversation(chat["id"]);result=store.get_run(run["id"])
    assert saved["state"]=="idle" and len(saved["messages"])==2
    assert saved["steps"][0]["result"]["artifact"]=="valuation"
    assert result["valuation_history"][0]["origin"]=="conversation"
    follow=store.begin_turn(chat["id"],"PE 改 12，其余不变","test","tushare")
    agent.grounded_assumptions(follow,"pe",{"multiple":12,"growth_pct":10},{"multiple":"PE 改 12"})
    agent.grounded_assumptions(follow,"pe",{"multiple":12,"growth_pct":10},{"multiple":"PE 12"})
    with pytest.raises(ValueError): agent.grounded_assumptions(follow,"pe",{"multiple":20},{"multiple":"PE 改 20"})
    with pytest.raises(ValueError): agent.grounded_assumptions(follow,"dcf",{"wacc_pct":8},{})

def test_report_revision_changes_export_preserves_old_and_rejects_bad_refs(run):
    chat=store.new_conversation(run["id"])
    memo=research.base_memo(run)|{"summary":"本次研究重点转向现金流变化的潜在风险；营运资本原因仍待核实。"}
    result=agent.tool(chat,"revise_report",{"memo":memo,"change_note":"将重点转向现金流风险"})
    saved=store.get_run(run["id"])
    assert result["revision"]==2 and saved["memo"]["summary"]==memo["summary"]
    assert saved["memo_revisions"][0]["memo"]==run["memo"]
    memo["theses"][0]["fact_ids"]=["imaginary"]
    with pytest.raises(ValueError): agent.tool(chat,"revise_report",{"memo":memo})
    from app.exporting import markdown,printable
    assert saved["memo"]["summary"] in markdown(saved)
    assert "报告第 2 版" in printable(saved)
    assert store.get_run(run["id"])["report_revision"]==2

def test_cancel_prevents_model_call(run,monkeypatch):
    c=store.new_conversation(run["id"]);store.begin_turn(c["id"],"test","test","tushare");store.cancel_conversation(c["id"])
    monkeypatch.setattr(agent,"model_json",lambda *a,**kw:pytest.fail("cancelled before model"))
    agent.execute(c["id"])
    assert store.get_conversation(c["id"])["state"]=="cancelled"

def test_explicit_report_update_cannot_finish_with_only_a_plan(run,monkeypatch):
    c=store.new_conversation(run["id"])
    store.begin_turn(c["id"],"请把当前研究报告更新为现金流风险视角，请实际保存新版本。","test","tushare")
    memo=research.base_memo(run)|{"summary":"经营现金流变化需要进一步核实。"}
    decisions=iter([{"action":"respond","message":"如果认可我就保存"},
        {"action":"tool","tool":"revise_report","arguments":{"memo":memo,"change_note":"现金流风险视角"}},
        {"action":"respond","message":"已保存新版本"}])
    monkeypatch.setattr(agent,"model_json",lambda *a,**kw:(next(decisions),{"model":"test"}))
    agent.execute(c["id"])
    saved=store.get_conversation(c["id"])
    assert saved["messages"][-1]["content"].startswith("已保存新版本")
    assert saved["messages"][-1]["delivery"]["report_revision_after"]==2
    assert store.get_run(run["id"])["report_revision"]==2
    assert agent.requested_artifacts("先不要修改报告，也不要直接计算。")==set()
    assert agent.requested_artifacts("请把 DCF 假设整理成待确认方案卡片，先不计算。")=={"proposal"}

def test_api_settings_sources_and_valuation(run,monkeypatch):
    monkeypatch.setattr(main,"launch",lambda r:r)
    with TestClient(main.app) as client:
        cfg=client.get("/api/settings").json()
        assert len(cfg["presets"])==6 and len(cfg["data_sources"])==3
        assert all("secret" not in p and "key" not in p for p in cfg["profiles"])
        assert len(client.get("/api/runs/"+run["id"]+"/valuation-options").json())==6
        response=client.post("/api/runs/"+run["id"]+"/valuations",json={"method":"pe","assumptions":{"multiple":12,"growth_pct":10}})
        assert response.status_code==200
        assert response.json()["output"]["price"] is None
        new=client.post("/api/runs",json={"mode":"sample"}).json()
        assert new["request"]["data_source"]=="tushare" and new["request"]["model_profile"]

def test_proposal_requires_acceptance_and_reuses_result(run):
    c=store.new_conversation(run["id"])
    c=store.begin_turn(c["id"],"请提出一个 PE 参数方案，先不要计算。","test","tushare")
    proposal=agent.tool(c,"propose_valuation",{"method":"pe","assumptions":{"growth_pct":5,"multiple":12},"rationale":"仅为探索假设"})["proposal"]
    assert store.get_run(run["id"])["valuation_history"]==[]
    c=store.get_conversation(c["id"])
    with pytest.raises(ValueError): agent.tool(c,"calculate_proposal",{"proposal_id":proposal["id"]})
    store.update_conversation(c["id"],lambda v:v.update(state="idle"))
    c=store.begin_turn(c["id"],"采用方案 "+proposal["id"]+"，计算并保存","test","tushare")
    result=agent.tool(c,"calculate_proposal",{"proposal_id":proposal["id"]})
    assert result["result"]["origin"]=="accepted_proposal"
    c=store.get_conversation(c["id"])
    again=agent.tool(c,"calculate_proposal",{"proposal_id":proposal["id"]})
    assert again["result"]["id"]==result["result"]["id"]
    assert len(store.get_run(run["id"])["valuation_history"])==1
