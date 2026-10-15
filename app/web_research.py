"""Bounded public research retrieval, independent of Tushare and the LLM.

No-key paths use Eastmoney's public news search and Bing/DuckDuckGo HTML.
Optional TAVILY_API_KEY / BRAVE_SEARCH_API_KEY use their documented APIs.
Search snippets are leads, never read articles or verified financial facts.
Network requests pin a validated public IP and revalidate every redirect.
"""
from __future__ import annotations
from . import competition

import base64
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from functools import lru_cache
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx

MAX_BYTES = 2_000_000
MAX_TEXT = 18000
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ResearchDesk/0.3", "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"}
COMMON_LIMITS = ["网页和搜索摘要属于外部资料，不是已核验财务事实。", "网页可能更新；未知发布日期不能证明在研究截止日前已公开。", "同一消息的转载不代表独立交叉验证。"]


class RetrievalError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plain(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()


def _date(value):
    if not value:
        return None
    text = str(value).strip()
    match = re.match(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    try:
        if match:
            return date(*map(int, match.groups())).isoformat()
        return parsedate_to_datetime(text).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _cutoff(as_of):
    if as_of is None:
        return None
    try:
        return date.fromisoformat(as_of).isoformat()
    except (TypeError, ValueError):
        raise ValueError("研究截止日应为 YYYY-MM-DD") from None


def _url_parts(url):
    if not isinstance(url, str) or len(url) > 3000 or re.search(r"[\x00-\x20\\]", url):
        raise RetrievalError("网页地址无效")
    try:
        p = urlsplit(url)
        host = (p.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = p.port or (443 if p.scheme == "https" else 80)
    except (ValueError, UnicodeError):
        raise RetrievalError("网页地址无效") from None
    if p.scheme not in ("http", "https") or not host or p.username or p.password or port != (443 if p.scheme == "https" else 80):
        raise RetrievalError("只允许无凭据、标准端口的公开 HTTP(S) 网页")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")) or "%" in host:
        raise RetrievalError("不能读取本地或内网地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not _global_address(ip):
        raise RetrievalError("不能读取本地、保留或内网地址")
    return p, host, port


def _global_address(value):
    ip = ipaddress.ip_address(value)
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def _public_addresses(url):
    p, host, port = _url_parts(url)
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError:
        raise RetrievalError("网页域名解析失败") from None
    if not addresses or any(not _global_address(ip) for ip in addresses):
        raise RetrievalError("网页域名解析到非公开网络，已拒绝读取")
    # IPv4 first avoids unnecessary failure on machines without IPv6 routing.
    return p, host, port, sorted(addresses, key=lambda value: ":" in value)


def _fetch(url, *, method="GET", data=None, json_body=None, headers=None, max_bytes=MAX_BYTES, binary_pdf=False):
    """Pin the resolved address: no second DNS lookup / proxy / cookies / credentials.

    Only fixed provider API calls supply auth headers; redirects on those calls
    are rejected so credentials can never follow a third-party redirect.
    """
    current = url
    deadline = time.monotonic() + 20
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(10, connect=5)) as client:
        for hop in range(5):
            competition.check_network(current)
            p, host, port, addresses = _public_addresses(current)
            if binary_pdf and not any(host == d or host.endswith("." + d) for d in ("cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn", "csrc.gov.cn")):
                raise RetrievalError("PDF 原文只允许来自交易所、巨潮和证监会公开披露域名")
            response_info = None
            for address in addresses[:2]:
                if time.monotonic() >= deadline:
                    raise RetrievalError("网页读取超时")
                authority = f"[{address}]" if ":" in address else address
                pinned = urlunsplit((p.scheme, f"{authority}:{port}", p.path or "/", p.query, ""))
                request_headers = {**HEADERS, **(headers or {}), "Host": host if port in (80, 443) else f"{host}:{port}"}
                try:
                    with client.stream(method, pinned, data=data, json=json_body, headers=request_headers,
                                       extensions={"sni_hostname": host.encode("ascii")}) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            if method != "GET" or headers:
                                raise RetrievalError("搜索服务发生异常重定向")
                            location = response.headers.get("location")
                            if not location:
                                raise RetrievalError("网页重定向缺少地址")
                            response_info = ("redirect", urljoin(current, location))
                            break
                        if response.status_code >= 400:
                            raise RetrievalError(f"公开站点返回 HTTP {response.status_code}")
                        content_type = response.headers.get("content-type", "").lower()
                        if "application/pdf" in content_type and not binary_pdf:
                            return {"url": current, "text": "", "content_type": content_type, "is_pdf": True}
                        if response.headers.get("content-length", "").isdigit() and int(response.headers["content-length"]) > max_bytes:
                            raise RetrievalError("网页响应超过大小上限")
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise RetrievalError("网页响应超过大小上限")
                            if time.monotonic() >= deadline:
                                raise RetrievalError("网页读取超时")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        if raw.startswith(b"%PDF-") or "application/pdf" in content_type:
                            result = {"url": current, "text": "", "content_type": "application/pdf", "is_pdf": True}
                            if binary_pdf:
                                result["content"] = raw
                            return result
                        encoding = re.search(r"charset=[\"']?([\w-]+)", content_type)
                        if not encoding:
                            encoding = re.search(r"charset=[\"']?([\w-]+)", raw[:4000].decode("ascii", "ignore"), re.I)
                        charset = encoding.group(1) if encoding else "utf-8"
                        try:
                            text = raw.decode(charset, "replace")
                        except LookupError:
                            text = raw.decode("utf-8", "replace")
                        return {"url": current, "text": text, "content_type": content_type, "is_pdf": False}
                except httpx.HTTPError:
                    if address == addresses[:2][-1]:
                        raise RetrievalError("公开站点连接失败或超时") from None
            if response_info:
                current = response_info[1]
            else:
                raise RetrievalError("公开站点未返回内容")
    raise RetrievalError("网页重定向次数过多")


def _canonical(url):
    p, host, port = _url_parts(html.unescape(url))
    query = [(k, v) for k, values in parse_qs(p.query, keep_blank_values=True).items()
             if not k.lower().startswith("utm_") and k.lower() not in ("spm", "from", "source") for v in values]
    return urlunsplit((p.scheme, host, p.path or "/", urlencode(query), ""))


def _unwrap(url):
    url = html.unescape(url)
    p = urlsplit(url)
    args = parse_qs(p.query)
    if p.hostname and p.hostname.endswith("bing.com") and p.path == "/ck/a" and args.get("u"):
        value = args["u"][0]
        if value.startswith("a1"):
            try:
                return base64.urlsafe_b64decode(value[2:] + "=" * (-len(value[2:]) % 4)).decode("utf-8")
            except (ValueError, UnicodeError):
                return ""
    if p.hostname and p.hostname.endswith("duckduckgo.com") and args.get("uddg"):
        return args["uddg"][0]
    return url


def _source_type(url):
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("finance.sina.com.cn") and "vCB_AllBulletinDetail" in urlsplit(url).path:
        return "disclosure_mirror"
    if any(host == domain or host.endswith("." + domain) for domain in ("cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn")):
        return "official_disclosure"
    if host.endswith(".gov.cn"):
        return "government"
    if any(host == domain or host.endswith("." + domain) for domain in ("eastmoney.com", "cnstock.com", "cs.com.cn", "stcn.com", "cls.cn", "sina.com.cn", "yicai.com")):
        return "financial_news"
    return "web_page"


def _tokens(query):
    return re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z][a-zA-Z0-9+-]*|\d{4,6}", re.sub(r"site:\S+", "", query))


def _site(query):
    match = re.search(r"(?:^|\s)site:([a-zA-Z0-9.-]+)", query)
    return match.group(1).rstrip(".").lower() if match else None


def _on_site(url, domain):
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def _topic(query):
    # An implementation notice or meeting resolution is more specific than a
    # fiscal-year/report mention. Route it to the official disclosure directory
    # using the exchange's actual naming, not only the user's "分红" wording.
    for needles, terms in ((r"(?:权益分派|利润分配|分红|派息).{0,12}(?:实施|发放|到账)|(?:实施|发放|到账).{0,12}(?:权益分派|利润分配|分红|派息)",
                            ("权益分派实施公告", "利润分配实施公告", "分红实施公告", "派息实施公告", "现金红利发放")),
                           (r"股东(?:大)?会.{0,12}决议", ("股东会决议", "股东大会决议")),
                           (r"(?:股份|股票|集中竞价).{0,8}回购|回购.{0,12}(?:股份|股票|公告|方案|进展|实施|完成)", ("回购",)),
                           (r"利润分配", ("利润分配", "权益分派", "分红", "派息")),
                           (r"权益分派|分红|派息", ("权益分派", "利润分配", "分红", "派息")),
                           (r"半年度报告|半年报|中报", ("半年度报告", "半年报", "中报", "上半年")),
                           (r"会计差错|差错更正", ("会计差错", "差错更正")),
                           (r"警示函", ("警示函",)), (r"监管函", ("监管函",)),
                           (r"年报|年度报告", ("年度报告", "年报"))):
        if re.search(needles, query):
            return terms
    return ()


def _score(query, row):
    tokens = _tokens(query)
    haystack = (row.get("title", "") + " " + row.get("snippet", "")).lower()
    domain = _site(query)
    if domain and not _on_site(row.get("url", ""), domain):
        return -1
    codes = re.findall(r"(?<!\d)\d{6}(?!\d)", query)
    names = [x for x in tokens if re.fullmatch(r"[\u4e00-\u9fff]{3,12}", x)]
    # When both company and ticker are provided, either must be visible in the
    # retrieved title/summary. Rejecting unrelated 200 responses is essential.
    anchors = codes + names[:1]
    if anchors and not any(anchor.lower() in haystack for anchor in anchors):
        return -1
    topic = _topic(query)
    if topic and not any(term in haystack for term in topic):
        return -1
    years = re.findall(r"(?<!\d)20\d{2}(?!\d)", query)
    if len(set(years)) == 1 and topic and topic[0] in ("半年度报告", "年度报告"):
        title_years = re.findall(r"(?<!\d)20\d{2}(?!\d)", row.get("title", ""))
        if title_years and years[0] not in title_years:
            return -1
    score = sum(3 if term.lower() in row.get("title", "").lower() else 1 for term in tokens if term.lower() in haystack)
    if not score:
        return -1
    kind = row.get("source_type") or _source_type(row.get("url", ""))
    if kind in ("official_disclosure", "government"):
        score += 5
    elif kind == "disclosure_mirror":
        score += 4
    if topic and any(term in row.get("title", "") for term in topic):
        score += 3
    if topic and "摘要" in row.get("title", "") and "摘要" not in query:
        score -= 3
    if re.search(r"融资净|融资余额|融券余额|龙虎榜|主力资金|涨停分析", row.get("title", "")) and not re.search(r"融资|融券|龙虎榜|资金流|涨停", query):
        score -= 5
    return score


@lru_cache(maxsize=128)
def _security(term):
    response = _fetch("https://www.cninfo.com.cn/new/information/topSearch/query", method="POST", data={"keyWord": term, "maxNum": "10"})
    rows = json.loads(response["text"])
    matches = [r for r in rows if r.get("category") == "A股" and (r.get("code") == term or term in (r.get("zwjc", ""), r.get("secName", "")))]
    return matches[0] if len(matches) == 1 else None


def _query_security(query):
    code = re.search(r"(?<!\d)\d{6}(?!\d)", query)
    term = code.group() if code else next((t for t in _tokens(query) if re.fullmatch(r"[\u4e00-\u9fff]{3,12}", t)), None)
    return _security(term) if term else None


def _cninfo(query, as_of=None):
    security = _query_security(query)
    if not security:
        return []
    cutoff = as_of or date.today().isoformat()
    years = [int(y) for y in re.findall(r"(?<!\d)20\d{2}(?!\d)", query)]
    start_year = min(years) if years else int(cutoff[:4]) - 2
    topic = _topic(query)
    key = topic[0] if topic else ""
    # Both legal names are in use. The shared directory keyword discovers both;
    # _score still requires the specific shareholder-meeting resolution title.
    if key == '股东会决议': key = '决议'
    code = security["code"]
    response = _fetch("https://www.cninfo.com.cn/new/hisAnnouncement/query", method="POST", data={
        "pageNum": "1", "pageSize": "50", "column": "sse" if code.startswith(("6", "9")) else "bse" if code.startswith(("4", "8")) else "szse",
        "tabName": "fulltext", "stock": code + "," + security["orgId"], "searchkey": key, "category": "",
        "seDate": f"{start_year}-01-01~{cutoff}", "isHLtitle": "false"})
    result = []
    for row in json.loads(response["text"]).get("announcements") or []:
        path = row.get("adjunctUrl", "")
        if row.get("secCode") != code or not re.fullmatch(r"finalpage/\d{4}-\d{2}-\d{2}/\d+\.pdf", path, re.I):
            continue
        stamp = datetime.fromtimestamp(int(row["announcementTime"]) / 1000, timezone(timedelta(hours=8))).date().isoformat()
        result.append({"title": security.get("zwjc", code) + "：" + _plain(row.get("announcementTitle")),
                       "url": "https://static.cninfo.com.cn/" + path, "snippet": "上市公司公开披露附件；" + code + "；PDF原文尚未读取。",
                       "published_at": stamp, "publisher": "巨潮资讯·上市公司公告", "source_type": "official_disclosure"})
    return result


def _sina_disclosures(query, as_of=None):
    security = _query_security(query)
    if not security:
        return []
    base = "https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllBulletin/stockid/" + security["code"] + ".phtml"
    urls = [base]
    if re.search(r"会计差错|更正", query):
        urls = [base + "?ftype=gzbc"]
    elif re.search(r"半年度报告|半年报|中报", query):
        urls = ["https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_BulletinZhong/stockid/" + security["code"] + "/page_type/zqbg.phtml"]
    rows = []
    for url in urls:
        raw = _fetch(url)["text"]
        for match in re.finditer(r'(\d{4}-\d{2}-\d{2})\s*(?:&nbsp;|\s)*<a\b[^>]*href=["\']([^"\']*vCB_AllBulletinDetail[^"\']+)["\'][^>]*>(.*?)</a>', raw, re.S | re.I):
            years = [int(y) for y in re.findall(r"(?<!\d)20\d{2}(?!\d)", query)]
            oldest_year = min(years) if years else int((as_of or date.today().isoformat())[:4]) - 2
            if int(match.group(1)[:4]) < oldest_year:
                continue
            rows.append({"title": _plain(match.group(3)), "url": urljoin(url, html.unescape(match.group(2))),
                         "published_at": match.group(1), "snippet": "证券代码 " + security["code"] + "；上市公司公告文本镜像，可查阅正文并核对原始附件。",
                         "publisher": "新浪财经·公司公告镜像", "source_type": "disclosure_mirror"})
    return rows


def _eastmoney(query, as_of=None):
    kind = "cmsArticleWebOld"
    param = {"uid": "", "keyword": query, "type": [kind], "client": "web", "clientType": "web", "clientVersion": "curr",
             "param": {kind: {"searchScope": "default", "sort": "default", "pageIndex": 1, "pageSize": 30, "preTag": "", "postTag": ""}}}
    url = "https://search-api-web.eastmoney.com/search/jsonp?" + urlencode({"cb": "research", "param": json.dumps(param, ensure_ascii=False)})
    raw = _fetch(url)["text"].strip()
    if raw.startswith("research("):
        raw = raw[len("research("):].rstrip("; ").removesuffix(")")
    payload = json.loads(raw)
    if payload.get("code") not in (None, 0):
        raise RetrievalError("公开财经搜索服务返回错误")
    return [{"title": _plain(row.get("title")), "url": row.get("url", ""), "snippet": _plain(row.get("content")),
             "publisher": _plain(row.get("mediaName")), "published_at": _date(row.get("date"))}
            for row in payload.get("result", {}).get(kind, []) if row.get("url")]


def _bing(query, as_of=None):
    raw = _fetch("https://cn.bing.com/search?" + urlencode({"q": query, "ensearch": "0"}))["text"]
    rows = []
    for block in re.findall(r'<li\b[^>]*class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>', raw, re.S | re.I):
        heading = re.search(r"<h2\b[^>]*>(.*?)</h2>", block, re.S | re.I)
        anchor = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', heading.group(1), re.S | re.I) if heading else None
        if not anchor:
            continue
        snippet = re.search(r"<p\b[^>]*>(.*?)</p>", block, re.S | re.I)
        rows.append({"title": _plain(anchor.group(2)), "url": _unwrap(anchor.group(1)), "snippet": _plain(snippet.group(1)) if snippet else ""})
    return rows


def _duckduckgo(query, as_of=None):
    raw = _fetch("https://html.duckduckgo.com/html/?" + urlencode({"q": query}))["text"]
    rows = []
    for match in re.finditer(r'<a\b(?=[^>]*class="[^"]*result__a)[^>]*href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S | re.I):
        tail = raw[match.end():match.end()+3000]
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', tail, re.S | re.I)
        rows.append({"title": _plain(match.group(2)), "url": _unwrap(urljoin("https://duckduckgo.com", match.group(1))), "snippet": _plain(snippet.group(1)) if snippet else ""})
    return rows


def _tavily(query, as_of=None):
    payload = {"query": query, "search_depth": "advanced", "max_results": 12, "include_answer": False, "include_raw_content": False, "include_published_date": True}
    if as_of:
        payload["end_date"] = as_of
    raw = _fetch("https://api.tavily.com/search", method="POST", json_body=payload,
                 headers={"Authorization": "Bearer " + os.environ["TAVILY_API_KEY"]})
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", ""), "published_at": _date(r.get("published_date"))}
            for r in json.loads(raw["text"]).get("results", [])]


def _brave(query, as_of=None):
    raw = _fetch("https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query, "count": 12, "search_lang": "zh-hans"}),
                 headers={"X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"]})
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": _plain(r.get("description")), "published_at": _date(r.get("page_age"))}
            for r in json.loads(raw["text"]).get("web", {}).get("results", [])]


def _query_search(query, as_of):
    results, errors, engines = [], [], []
    deadline = time.monotonic() + 35
    providers = []
    if os.getenv("TAVILY_API_KEY", "").strip():
        providers.append(("tavily", _tavily))
    elif os.getenv("BRAVE_SEARCH_API_KEY", "").strip():
        providers.append(("brave_api", _brave))
    domain = _site(query)
    if _topic(query) or domain and any(part in domain for part in ("cninfo", "sina")):
        if not domain or _on_site("https://static.cninfo.com.cn", domain):
            providers.append(("cninfo", _cninfo))
        if not domain or _on_site("https://vip.stock.finance.sina.com.cn", domain):
            providers.append(("sina_disclosure", _sina_disclosures))
    if not domain or _on_site("https://finance.eastmoney.com", domain):
        providers.append(("eastmoney", _eastmoney))
    providers.append(("bing", _bing))
    for engine, provider in providers:
        if time.monotonic() >= deadline:
            errors.append({"engine": engine, "query": query, "error": "本轮搜索时间预算已用尽"})
            break
        engines.append(engine)
        try:
            rows = provider(query, as_of)
            valid = [r for r in rows if _score(query, r) >= 0]
            # Finance search uses AND-like matching; long research questions
            # often return no rows. Broaden only this provider to the exact
            # company or ticker, then rank against the original query.
            if engine == "eastmoney" and not valid:
                tokens = _tokens(query)
                base = next((x for x in tokens if re.fullmatch(r"[\u4e00-\u9fff]{3,12}", x)), None)
                if not base:
                    base = next(iter(re.findall(r"(?<!\d)\d{6}(?!\d)", query)), None)
                if base and base != query and time.monotonic() < deadline:
                    valid = [r for r in provider(base, as_of) if _score(query, r) >= 0]
            if not valid:
                errors.append({"engine": engine, "query": query, "error": "未返回相关结果，可能没有匹配内容或受到限制"})
            results.extend({**r, "engine": engine, "query": query} for r in valid)
        except (RetrievalError, ValueError, KeyError, TypeError):
            # Do not surface raw requests/exceptions: provider keys can occur
            # in headers or bodies. Return a useful but redacted failure.
            errors.append({"engine": engine, "query": query, "error": "搜索服务暂不可用或响应格式变化"})
    if time.monotonic() < deadline and not any(row["engine"] in ("bing", "tavily", "brave_api") for row in results):
        engines.append("duckduckgo")
        try:
            rows = [r for r in _duckduckgo(query, as_of) if _score(query, r) >= 0]
            results.extend({**r, "engine": "duckduckgo", "query": query} for r in rows)
            if not rows:
                errors.append({"engine": "duckduckgo", "query": query, "error": "未返回相关结果，可能受到验证码或访问限制"})
        except (RetrievalError, ValueError, KeyError, TypeError):
            errors.append({"engine": "duckduckgo", "query": query, "error": "搜索服务暂不可用"})
    return results, errors, engines


def search(queries: list[str], as_of: str | None = None, max_results: int = 12) -> dict:
    """Search up to 6 focused company/sector queries; return up to 24 leads."""
    if not isinstance(queries, list) or not queries or len(queries) > 6 or any(not isinstance(q, str) or not 2 <= len(q.strip()) <= 240 for q in queries):
        raise ValueError("请提供 1–6 条、每条 2–240 字的检索词，建议保留公司全称和证券代码")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 24:
        raise ValueError("搜索结果上限应为 1–24")
    as_of = _cutoff(as_of)
    queries = list(dict.fromkeys(q.strip() for q in queries))
    fetched = _now()
    collected, errors, engines, excluded = [], [], [], 0
    with ThreadPoolExecutor(max_workers=min(6, len(queries))) as pool:
        batches = list(pool.map(lambda q: _query_search(q, as_of), queries))
    for query, (rows, errs, used) in zip(queries, batches):
        errors.extend(errs)
        engines.extend(used)
        normalized = []
        for row in rows:
            try:
                url = _canonical(_unwrap(row.get("url", "")))
            except (ValueError, TypeError):
                continue
            published = _date(row.get("published_at"))
            if as_of and published and published > as_of:
                excluded += 1
                continue
            title = _plain(row.get("title"))[:240]
            snippet = _plain(row.get("snippet"))[:1000]
            if not title:
                continue
            normalized.append({"id": "web_" + hashlib.sha256(url.encode()).hexdigest()[:12], "title": title, "url": url,
                               "snippet": snippet, "source_type": _source_type(url), "published_at": published,
                               "retrieved_at": fetched, "publisher": _plain(row.get("publisher")) or urlsplit(url).hostname,
                               "query": query, "engine": row.get("engine"), "read_status": "pdf_link_only" if urlsplit(url).path.lower().endswith(".pdf") else "search_snippet",
                               "date_status": "dated_before_cutoff" if published else "unknown", "relevance": _score(query, {"title": title, "snippet": snippet, "url": url, "source_type": _source_type(url)})})
        normalized.sort(key=lambda r: (r["relevance"], r["published_at"] or ""), reverse=True)
        collected.append(normalized)
    # Round robin prevents the first query from consuming the entire budget.
    items, seen, seen_titles = [], set(), set()
    for index in range(max([len(batch) for batch in collected], default=0)):
        for batch in collected:
            if index >= len(batch):
                continue
            row = batch[index]
            # A PDF disclosure and its HTML mirror are different access paths:
            # retain both so a reader can verify the original and read tables.
            title_key = (re.sub(r"\W", "", row["title"]), row["read_status"] == "pdf_link_only")
            if row["url"] in seen or title_key in seen_titles:
                continue
            seen.add(row["url"]); seen_titles.add(title_key)
            items.append(row)
            if len(items) == max_results:
                break
        if len(items) == max_results:
            break
    limits = list(COMMON_LIMITS)
    if errors:
        limits.append("部分通用搜索引擎不可用或未返回相关结果；返回范围以成功引擎为准，不声称覆盖全网。")
    if items and all(row["engine"] == "eastmoney" for row in items):
        limits.append("本轮有效线索全部来自东方财富公开索引；可继续查阅原文及公司、监管或行业一手来源。")
    return {"status": "ok" if items else "empty", "results": items, "queries": queries, "engines": list(dict.fromkeys(engines)),
            "retrieved_at": fetched, "as_of": as_of, "excluded_after_cutoff": excluded, "errors": errors, "limitations": limits,
            "next_action": "选择相关且有分歧的多篇结果调用 read_webpage 读取原文；摘要不能作为已读全文。" if items else "缩短查询或拆分研究问题；也可配置 Tavily / Brave 搜索 API。"}


class _Page(HTMLParser):
    """Small semantic extractor; no scripts, remote resources or JS execution."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.skip = 0
        self.title = []
        self.body = []
        self.sections = []
        self.active = []
        self.meta = {}
        self.times = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self.skip += 1
        if tag == "meta":
            self.meta[(attrs.get("property") or attrs.get("name") or "").lower()] = attrs.get("content", "")
        if tag == "time" and attrs.get("datetime"):
            self.times.append(attrs["datetime"])
        identity = (attrs.get("id", "") + " " + attrs.get("class", "")).lower()
        chosen = tag in ("article", "main") or bool(re.search(r"contentbody|article[-_]?body|article[-_]?content|news[-_]?content|article[-_]?text|post[-_]?content|^content\s", identity))
        if chosen:
            section = []
            self.sections.append(section)
            self.active.append((len(self.stack), section))
        if tag not in ("meta", "link", "br", "hr", "img", "input", "source", "wbr", "embed", "area", "base", "param", "col"):
            self.stack.append(tag)
        if tag in ("p", "br", "div", "tr", "h1", "h2", "h3", "li"):
            self._append("\n")
        if tag in ("td", "th"):
            self._append(" | ")

    def handle_endtag(self, tag):
        if tag in self.stack:
            idx = len(self.stack) - 1 - self.stack[::-1].index(tag)
            removed = self.stack[idx:]
            self.skip = max(0, self.skip - sum(t in ("script", "style", "noscript", "svg", "nav", "footer", "header") for t in removed))
            self.stack = self.stack[:idx]
            self.active = [(depth, section) for depth, section in self.active if depth < idx]
        if tag in ("p", "div", "tr", "h1", "h2", "h3", "li"):
            self._append("\n")

    def _append(self, text):
        if not self.skip:
            self.body.append(text)
            for _, section in self.active:
                section.append(text)

    def handle_data(self, data):
        if "title" in self.stack:
            self.title.append(data)
        self._append(data)


def _extract(raw):
    page = _Page()
    page.feed(raw)
    candidates = ["".join(section) for section in page.sections]
    content = max(candidates, key=len) if candidates else "".join(page.body)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in content.splitlines()]
    text = "\n".join(line for line in lines if line)
    title = page.meta.get("og:title") or "".join(page.title).strip()
    published = None
    for key in ("article:published_time", "datepublished", "pubdate", "publishdate", "publish_date", "date", "dc.date.issued"):
        published = _date(page.meta.get(key))
        if published:
            break
    if not published:
        published = next((value for raw_value in page.times if (value := _date(raw_value))), None)
    if not published:
        # Exact publication metadata only: avoid mistaking fiscal period,
        # copyright year or site's rendering date for the article date.
        match = re.search(r'["\']datePublished["\']\s*:\s*["\']([^"\']+)', raw)
        published = _date(match.group(1)) if match else None
    if not published:
        # Publication timestamp container used by Chinese finance publishers.
        # Do not read the site's "generated at" comment or footer date.
        match = re.search(r'<(?:div|span)\b[^>]*(?:class|id)=["\'][^"\']*\b(?:infos|publish-time|pubtime|news-time)\b[^"\']*["\'][^>]*>(.{0,600})', raw, re.S | re.I)
        stamp = re.search(r"\d{4}[年/-]\d{1,2}[月/-]\d{1,2}", _plain(match.group(1))) if match else None
        published = _date(stamp.group()) if stamp else None
    if not published:
        match = re.search(r"公告日期\s*[:：]\s*(\d{4}-\d{2}-\d{2})", _plain(raw))
        published = _date(match.group(1)) if match else None
    return title, text, published


def _links(raw, base_url):
    """Navigation leads remain explicitly unread and are never fetched here."""
    out, seen = [], set()
    for match in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw, re.S | re.I):
        title = _plain(match.group(2))
        if not 2 <= len(title) <= 180 or any(word in title for word in ("ICP备", "免费开户", "立即开户", "登录", "注册")):
            continue
        try:
            url = _canonical(urljoin(base_url, html.unescape(match.group(1))))
        except ValueError:
            continue
        if url in seen or url == base_url:
            continue
        if len(title) < 6 and not urlsplit(url).path.lower().endswith(".pdf"):
            continue
        seen.add(url)
        out.append({"title": title, "url": url, "read_status": "link_only"})
    base_host = urlsplit(base_url).hostname
    out.sort(key=lambda item: (urlsplit(item["url"]).hostname == base_host, len(urlsplit(item["url"]).path), len(item["title"])), reverse=True)
    return out[:16]


def read(url: str, as_of: str | None = None) -> dict:
    """Read public HTML/text. PDF URLs explicitly remain unread document leads."""
    as_of = _cutoff(as_of)
    stamp = _now()
    base = {"url": url, "title": "", "text": "", "published_at": None, "retrieved_at": stamp, "as_of": as_of,
            "source_type": "web_page", "limitations": list(COMMON_LIMITS)}
    try:
        _url_parts(url)
        result = _fetch(url)
        base.update(url=result["url"], source_type=_source_type(result["url"]))
        if result["is_pdf"]:
            return {**base, "status": "document_link", "read_status": "pdf_link_only", "limitations": base["limitations"] + ["这是 PDF 原文链接，尚未读取 PDF 内容。请下载并接入资料库后用文档工具读取。"]}
        if "html" not in result["content_type"] and not result["content_type"].startswith("text/") and "xml" not in result["content_type"]:
            raise RetrievalError("此地址不是可读网页或文本")
        title, text, published = _extract(result["text"])
        base.update(title=title[:300], published_at=published)
        if as_of and published and published > as_of:
            return {**base, "status": "excluded_after_cutoff", "read_status": "excluded", "limitations": base["limitations"] + ["原文发布时间晚于研究截止日，内容已排除。"]}
        if len(text) < 120 or any(token in title.lower() for token in ("captcha", "access denied", "访问验证", "安全验证", "人机验证")):
            raise RetrievalError("未取得可用正文，网页可能要求登录、验证码或 JavaScript")
        return {**base, "status": "ok", "text": text[:MAX_TEXT], "read_status": "article_text", "truncated": len(text) > MAX_TEXT,
                "characters": len(text), "date_status": "dated_before_cutoff" if published else "unknown",
                "links": _links(result["text"], result["url"]),
                "limitations": base["limitations"] + (["正文已截断，请聚焦相关段落，不能声称已读全部内容。"] if len(text) > MAX_TEXT else [])}
    except (RetrievalError, ValueError, TypeError, OSError) as exc:
        safe = str(exc) if isinstance(exc, RetrievalError) else "网页读取失败"
        return {**base, "status": "error", "read_status": "failed", "error": safe, "errors": [safe]}
