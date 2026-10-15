import json
import socket

import httpx
import pytest

from app import web_research as web


def row(title="华宇软件半年报", url="https://finance.example.com/a/1", date="2026-08-26", snippet="华宇软件 300271 经营现金流"):
    return {"title": title, "url": url, "published_at": date, "snippet": snippet}


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://127.0.0.1", "http://127.1", "http://2130706433",
    "http://[::1]", "http://[::ffff:127.0.0.1]", "http://169.254.169.254/latest",
    "http://100.64.0.1", "http://10.0.0.1", "http://localhost.", "http://test.local",
    "http://224.0.0.1", "http://[ff02::1]", "http://example.com:443",
    "https://user:pass@example.com", "https://example.com:8920", "https://example.com\\@127.0.0.1",
])
def test_rejects_private_and_ambiguous_urls_without_fetch(monkeypatch, url):
    # Windows inet_aton/getaddrinfo accepts legacy abbreviated/numeric IPs.
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    result = web.read(url)
    assert result["status"] == "error"
    assert result["text"] == ""


def test_rejects_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.1", 443)),
    ])
    with pytest.raises(web.RetrievalError, match="非公开"):
        web._public_addresses("https://example.com/")


def mock_client(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(web.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


def test_transport_pins_dns_address_and_preserves_tls_hostname(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, text="hello", headers={"content-type": "text/plain"})
    mock_client(monkeypatch, handler)
    result = web._fetch("https://example.com/research?x=1")
    assert result["url"] == "https://example.com/research?x=1"
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["Host"] == "example.com"
    assert requests[0].extensions["sni_hostname"] == b"example.com"


def test_redirect_target_rechecked_before_request(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
    mock_client(monkeypatch, handler)
    result = web.read("https://example.com")
    assert result["status"] == "error"
    assert len(requests) == 1


def test_size_limit_and_pdf_not_misrepresented_as_read(monkeypatch):
    mock_client(monkeypatch, lambda r: httpx.Response(200, content=b"%PDF-FAKE", headers={"content-type": "application/pdf"}))
    result = web.read("https://example.com/report.pdf")
    assert result["status"] == "document_link"
    assert result["read_status"] == "pdf_link_only" and result["text"] == ""


def test_large_page_is_rejected(monkeypatch):
    mock_client(monkeypatch, lambda r: httpx.Response(200, content=b"long", headers={"content-length": str(web.MAX_BYTES + 1)}))
    assert web.read("https://example.com")["status"] == "error"


def test_page_extracts_article_excludes_script_and_does_not_invent_dates(monkeypatch):
    raw = "<html><head><title>华宇回款研究</title></head><body><nav>导航广告</nav><article><p>" + "经营现金流观察，回款并未兑现。" * 30 + "</p><script>steal secrets</script></article><footer>2026-10-02</footer></body></html>"
    monkeypatch.setattr(web, "_fetch", lambda url: {"url": url, "text": raw, "content_type": "text/html", "is_pdf": False})
    result = web.read("https://example.com/research", "2026-09-30")
    assert result["status"] == "ok"
    assert "steal" not in result["text"] and "导航广告" not in result["text"]
    assert result["published_at"] is None and result["date_status"] == "unknown"


def test_known_future_article_is_excluded(monkeypatch):
    raw = '<meta property="article:published_time" content="2026-10-02T09:00:00+08:00"><article>' + "华宇研究。" * 100 + "</article>"
    monkeypatch.setattr(web, "_fetch", lambda url: {"url": url, "text": raw, "content_type": "text/html", "is_pdf": False})
    result = web.read("https://example.com/research", "2026-09-30")
    assert result["status"] == "excluded_after_cutoff" and result["text"] == ""


def test_chinese_publication_container_and_related_links(monkeypatch):
    raw = '<!--generated at 2026-10-02--><div class="infos"><div class="item">2026年08月26日 12:41</div></div><article>' + '公司研究正文。' * 40 + '</article><a href="/company/model">万象大模型产品介绍</a><a href="http://127.0.0.1/private">内网不可使用链接</a>'
    monkeypatch.setattr(web, "_fetch", lambda url: {"url": url, "text": raw, "content_type": "text/html", "is_pdf": False})
    result = web.read("https://example.com/research", "2026-09-30")
    assert result["status"] == "ok" and result["published_at"] == "2026-08-26"
    assert result["links"] == [{"title": "万象大模型产品介绍", "url": "https://example.com/company/model", "read_status": "link_only"}]


def test_provider_auth_is_never_forwarded_on_redirect(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example.com"})
    mock_client(monkeypatch, handler)
    with pytest.raises(web.RetrievalError, match="异常重定向"):
        web._fetch("https://api.tavily.com/search", method="POST", headers={"Authorization": "Bearer secret"})
    assert len(seen) == 1


def test_search_dedup_cutoff_query_diversity_and_bad_urls(monkeypatch):
    def query(q, cutoff):
        values = [row(), row(url="https://finance.example.com/a/1?utm_source=test"), row("未来消息", date="2026-10-03"),
                  row("私网", "http://127.0.0.1"), row("公司技术合同", "https://issuer.example.com/new", date=None)]
        return [dict(r, query=q, engine="fixture") for r in values], [], ["fixture"]
    monkeypatch.setattr(web, "_query_search", query)
    result = web.search(["华宇软件 300271 半年报", "华宇软件 300271 合同"], "2026-10-02", 12)
    assert len(result["results"]) == 2
    assert result["excluded_after_cutoff"] == 2
    assert result["results"][0]["read_status"] == "search_snippet"
    assert result["results"][1]["published_at"] is None


def test_unrelated_finance_results_do_not_count_as_success(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    unrelated = lambda *a: [row("丰田竞争加剧", snippet="竞争 大模型 AI")]
    monkeypatch.setattr(web, "_eastmoney", unrelated)
    monkeypatch.setattr(web, "_bing", unrelated)
    monkeypatch.setattr(web, "_duckduckgo", unrelated)
    result = web.search(["华宇软件 万象 AI", "华宇软件 300271 竞争"])
    assert result["status"] == "empty" and not result["results"]
    assert result["errors"] and "bing" in result["engines"]


def test_finance_query_falls_back_to_exact_company_and_retains_original_intent(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    queries = []
    def finance(q, cutoff):
        queries.append(q)
        return [row()] if q == "华宇软件" else []
    monkeypatch.setattr(web, "_eastmoney", finance)
    monkeypatch.setattr(web, "_cninfo", lambda *a: [])
    monkeypatch.setattr(web, "_sina_disclosures", lambda *a: [])
    monkeypatch.setattr(web, "_bing", lambda *a: [])
    monkeypatch.setattr(web, "_duckduckgo", lambda *a: [])
    result = web.search(["华宇软件 300271 2026 半年报 AI 回款"])
    assert result["status"] == "ok"
    assert queries == ["华宇软件 300271 2026 半年报 AI 回款", "华宇软件"]
    assert result["results"][0]["query"] == queries[0]


def test_failure_never_exposes_api_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "secret-test-value")
    def failure(*a):
        raise ValueError("Authorization secret-test-value")
    monkeypatch.setattr(web, "_tavily", failure)
    monkeypatch.setattr(web, "_eastmoney", lambda *a: [])
    monkeypatch.setattr(web, "_bing", lambda *a: [])
    monkeypatch.setattr(web, "_duckduckgo", lambda *a: [])
    result = web.search(["华宇软件 300271"])
    assert result["status"] == "empty"
    assert "secret-test-value" not in json.dumps(result)


@pytest.mark.parametrize("queries", [[], ["a"], "华宇软件", ["a" * 241], ["华宇软件"] * 7])
def test_search_input_bounds(queries):
    with pytest.raises(ValueError):
        web.search(queries)


def test_site_filter_cannot_silently_fall_back_to_financial_news(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    def forbidden(*a):
        raise AssertionError("site:csrc.gov.cn must not become a finance-index company query")
    monkeypatch.setattr(web, "_eastmoney", forbidden)
    monkeypatch.setattr(web, "_bing", lambda *a: [row()])
    monkeypatch.setattr(web, "_duckduckgo", lambda *a: [row()])
    result = web.search(["华宇软件 警示函 site:csrc.gov.cn"])
    assert result["status"] == "empty"
    assert "eastmoney" not in result["engines"]


def test_precise_report_and_correction_queries_reject_wrong_topic_or_year():
    assert web._score("华宇软件 2026 半年度报告", row("华宇软件2025年半年度报告")) == -1
    assert web._score("华宇软件 2026 半年度报告", row("华宇软件2026年股东会公告")) == -1
    assert web._score("华宇软件 会计差错更正", row("华宇软件股东大会通知的更正公告")) == -1
    assert web._score("华宇软件 2026 半年度报告", row("华宇软件2026年半年度报告")) > 0


@pytest.mark.parametrize('query,keyword', [
    ('测试银行 2025年度 权益分派实施公告', '权益分派实施公告'),
    ('测试银行 2025年报 分红实施公告', '权益分派实施公告'),
    ('测试银行 2025 年度利润分配实施情况', '权益分派实施公告'),
    ('测试银行 2025年度利润分配方案', '利润分配'),
    ('测试银行 现金分红', '权益分派'),
    ('测试银行 2025年度股东会决议公告', '股东会决议'),
    ('测试银行 年度股东大会决议', '股东会决议'),
    ('测试银行 回购股份进展公告', '回购'),
])
def test_corporate_action_intents_take_precedence_over_report_mentions(query, keyword):
    assert web._topic(query)[0] == keyword


@pytest.mark.parametrize('query', ['白酒行业竞争格局', '银行逆回购利率走势', '行业利润率与销售增长', '年度经营展望'])
def test_generic_industry_words_do_not_trigger_disclosure_topics(query):
    assert web._topic(query) == ()


def test_dividend_implementation_query_routes_to_official_directory(monkeypatch):
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY', raising=False)
    calls = []
    def official(query, cutoff):
        calls.append((query, cutoff))
        return [row('测试银行：2025年年度权益分派实施公告',
                    'https://static.cninfo.com.cn/finalpage/2026-06-22/1234567890.PDF',
                    '2026-06-22', '上市公司公开披露附件；600000；PDF原文尚未读取。')]
    monkeypatch.setattr(web, '_cninfo', official)
    for name in ('_sina_disclosures', '_eastmoney', '_bing', '_duckduckgo'):
        monkeypatch.setattr(web, name, lambda *args: [])
    query = '测试银行 600000 2025 年报 分红实施公告'
    result = web.search([query], '2026-10-15')
    assert calls == [(query, '2026-10-15')]
    assert result['results'][0]['engine'] == 'cninfo'
    assert result['results'][0]['read_status'] == 'pdf_link_only'
    assert result['results'][0]['published_at'] == '2026-06-22'


def test_dividend_implementation_filters_report_and_proposal_but_accepts_official_name():
    query = '测试银行 600000 2025年报 分红实施公告'
    assert web._score(query, row('测试银行：2025年年度权益分派实施公告', snippet='600000')) > 0
    assert web._score(query, row('测试银行：2025年年度利润分配实施公告', snippet='600000')) > 0
    assert web._score(query, row('测试银行：2025年年度报告', snippet='600000')) == -1
    assert web._score(query, row('测试银行：2025年度利润分配预案', snippet='600000')) == -1
    assert web._score('测试银行 股东大会决议', row('测试银行：2025年度股东会决议公告')) > 0
    assert web._score('测试银行 股东大会决议', row('测试银行：董事会决议公告')) == -1


def test_official_directory_uses_real_returned_attachment_and_china_date(monkeypatch):
    monkeypatch.setattr(web, "_query_security", lambda q: {"code": "600000", "orgId": "org123", "zwjc": "测试银行"})
    from datetime import datetime, timezone
    stamp = int(datetime(2026, 8, 25, 16, tzinfo=timezone.utc).timestamp() * 1000)
    calls = []
    def fetch(url, **kw):
        calls.append(kw)
        return {"text": json.dumps({"announcements": [
            {"secCode": "600000", "adjunctUrl": "finalpage/2026-08-26/1234567890.PDF", "announcementTime": stamp, "announcementTitle": "2026年半年度报告"},
            {"secCode": "600001", "adjunctUrl": "finalpage/2026-08-26/1234567891.PDF", "announcementTime": stamp, "announcementTitle": "其他公司"},
        ]})}
    monkeypatch.setattr(web, "_fetch", fetch)
    result = web._cninfo("测试银行 600000 2026 半年报", "2026-10-02")
    assert len(result) == 1 and result[0]["published_at"] == "2026-08-26"
    assert result[0]["url"] == "https://static.cninfo.com.cn/finalpage/2026-08-26/1234567890.PDF"
    assert calls[0]["data"]["searchkey"] == "半年度报告"


def test_sina_mirror_is_discovered_from_directory_not_fabricated(monkeypatch):
    monkeypatch.setattr(web, "_query_security", lambda q: {"code": "600000", "zwjc": "测试银行"})
    raw = "2026-08-26 <a target='_blank' href='/corp/view/vCB_AllBulletinDetail.php?stockid=600000&id=987654'>测试银行：2026年半年度报告</a>"
    monkeypatch.setattr(web, "_fetch", lambda url: {"text": raw})
    result = web._sina_disclosures("测试银行 2026 半年报", "2026-10-02")
    assert result[0]["url"].endswith("?stockid=600000&id=987654")
    assert result[0]["source_type"] == "disclosure_mirror"


def test_pdf_original_and_html_mirror_survive_title_dedup(monkeypatch):
    title = "华宇软件：2026年半年度报告"
    rows = [dict(row(title, "https://static.cninfo.com.cn/finalpage/report.PDF"), engine="cninfo"),
            dict(row(title, "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=123"), engine="sina_disclosure")]
    monkeypatch.setattr(web, "_query_search", lambda *a: (rows, [], ["cninfo", "sina_disclosure"]))
    result = web.search(["华宇软件 2026 半年报"])
    assert len(result["results"]) == 2
    assert {r["read_status"] for r in result["results"]} == {"pdf_link_only", "search_snippet"}


def test_binary_pdf_download_has_explicit_official_allowlist_and_bytes(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"%PDF-fixture", headers={"content-type": "application/pdf"})
    mock_client(monkeypatch, handler)
    data = web._fetch("https://static.cninfo.com.cn/report.PDF", binary_pdf=True, max_bytes=1024)
    assert data["content"] == b"%PDF-fixture"
    with pytest.raises(web.RetrievalError, match="公开披露"):
        web._fetch("https://other.example.com/report.PDF", binary_pdf=True, max_bytes=1024)
    assert len(seen) == 1


def test_binary_pdf_redirect_rechecks_official_allowlist(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://other.example.com/file"})
    mock_client(monkeypatch, handler)
    with pytest.raises(web.RetrievalError, match="公开披露"):
        web._fetch("https://static.cninfo.com.cn/report.PDF", binary_pdf=True, max_bytes=1024)
    assert len(seen) == 1


def test_disclosure_reader_preserves_table_columns_and_excludes_navigation(monkeypatch):
    raw = '<title>公告镜像</title><div>公告日期:2026-08-26</div><div>导航杂项</div><div id="content"><p>' + '半年报正文。' * 30 + '</p><table><tr><th>项目</th><th>本期</th><th>上期调整后</th></tr><tr><td>营业收入</td><td>63800</td><td>59253</td></tr></table></div>'
    monkeypatch.setattr(web, "_fetch", lambda url: {"url": url, "text": raw, "content_type": "text/html", "is_pdf": False})
    result = web.read("https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=123", "2026-10-02")
    assert "营业收入 | 63800 | 59253" in result["text"]
    assert result["published_at"] == "2026-08-26" and "导航杂项" not in result["text"]
