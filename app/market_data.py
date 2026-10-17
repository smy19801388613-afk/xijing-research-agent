"""Observed A-share closing prices, kept separate from valuation assumptions.

The daily endpoint is an unadjusted close, not an intraday quote. A successful
API record is one observed source; it is never promoted to audited evidence.
Official contracts: https://tushare.pro/document/2?doc_id=27 and
https://tushare.pro/document/1?doc_id=130.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import time

import httpx

from . import competition, connections, execution_budget, store
from .providers import ProviderError


ENDPOINT = 'https://api.tushare.pro'
DOCUMENTATION = 'https://tushare.pro/document/2?doc_id=27'
MAX_AGE_DAYS = 30


def _today():
    return datetime.now(timezone(timedelta(hours=8))).date()


def _date(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError(label + '须为 YYYY-MM-DD 日期')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(label + '日期无效') from None


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('收盘价不是有效数字')
    if len(str(value)) > 80:
        raise ValueError('收盘价数值长度异常')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('收盘价不是有效数字') from None
    if not result.is_finite() or result <= 0:
        raise ValueError('收盘价必须为有限正数')
    if not -12 <= result.adjusted() <= 12:
        raise ValueError('收盘价数量级异常，未采用本次响应')
    text = format(result, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _check():
    store.check_execution()
    deadline = execution_budget.deadline()
    if deadline is not None and time.monotonic() >= deadline:
        raise execution_budget.BudgetExhausted('市场报价查询达到本轮时间预算，未覆盖已有报价')
    return deadline


def _token():
    """Read the data credential without requiring any model connection."""
    settings = connections.load()
    if settings.get('tushare_legacy', True):
        return connections.legacy_settings().get('token', '')
    return connections.protect(settings.get('tushare_secret', ''), True)


def _safe_error(value, token):
    # A provider error must remain useful without echoing a credential returned
    # by a misbehaving endpoint. Do not persist response bodies or request JSON.
    message = str(value)
    if token:
        message = message.replace(token, '[已隐藏密钥]')
    message = re.sub(r'(?i)(token|api[_ -]?key|authorization)\s*[:=]\s*[^\s,，;；]+', r'\1=[已隐藏]', message)
    message = re.sub(r'[A-Za-z0-9_-]{32,}', '[已隐藏长标识]', message)
    return re.sub(r'\s+', ' ', message).strip()[:500]


def _daily(ticker, start, end, token):
    deadline = _check()
    competition.check_network(ENDPOINT)
    timeout = min(25.0, max(0.001, deadline - time.monotonic())) if deadline is not None else 25.0
    try:
        response = httpx.post(ENDPOINT, json={
            'api_name': 'daily', 'token': token,
            'params': {'ts_code': ticker, 'start_date': start.strftime('%Y%m%d'), 'end_date': end.strftime('%Y%m%d')},
            'fields': 'ts_code,trade_date,close',
        }, timeout=timeout)
        response.raise_for_status()
        _check()
        payload = response.json(parse_float=Decimal)
    except httpx.TimeoutException:
        _check()
        raise ProviderError('Tushare daily 请求超时，未取得本次报价；本次未自动重试') from None
    except httpx.HTTPStatusError as error:
        raise ProviderError(f'Tushare daily HTTP {error.response.status_code}，未取得本次报价') from None
    except httpx.RequestError as error:
        raise ProviderError('Tushare daily 网络请求失败（' + type(error).__name__ + '），未取得本次报价') from None
    except (ValueError, TypeError):
        raise ProviderError('Tushare daily 未返回有效 JSON，未采用本次响应') from None
    if not isinstance(payload, dict):
        raise ProviderError('Tushare daily 响应结构无效')
    if type(payload.get('code')) is not int or payload['code'] != 0:
        code = _safe_error(payload.get('code'), token)
        detail = _safe_error(payload.get('msg') or '未提供错误详情', token)
        raise ProviderError(f'Tushare daily 返回错误 code={code}：{detail}')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise ProviderError('Tushare daily 缺少 data 记录')
    fields, items = data.get('fields'), data.get('items')
    if (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)
            or len(set(fields)) != len(fields) or not {'ts_code', 'trade_date', 'close'}.issubset(fields)
            or not isinstance(items, list) or len(items) > 200):
        raise ProviderError('Tushare daily 字段或记录结构无效')
    if any(not isinstance(row, list) or len(row) != len(fields) for row in items):
        raise ProviderError('Tushare daily 记录长度与字段不一致')
    return [dict(zip(fields, row)) for row in items]


def _latest(rows, ticker, start, end):
    days = {}
    for row in rows:
        if row['ts_code'] != ticker:
            raise ValueError('Tushare daily 返回证券代码与本次研究不一致，未采用整批响应')
        compact = row['trade_date']
        if not isinstance(compact, str) or not re.fullmatch(r'\d{8}', compact):
            raise ValueError('Tushare daily 交易日期格式无效')
        traded = _date(f'{compact[:4]}-{compact[4:6]}-{compact[6:]}', '交易')
        if not start <= traded <= end:
            raise ValueError('Tushare daily 交易日期超出请求范围或晚于可用截止日，未采用整批响应')
        price = _positive(row['close'])
        if traded in days and days[traded] != price:
            raise ValueError('Tushare daily 同一交易日存在冲突收盘价，未采用本次响应')
        days[traded] = price
    if not days:
        return None
    traded = max(days)
    return traded, days[traded]


def reference(snapshot):
    """Expose only the observed original record, not a failed-fetch placeholder."""
    if not isinstance(snapshot, dict) or snapshot.get('status') != 'available':
        return None
    try:
        price = _positive(snapshot.get('price'))
        traded = _date(snapshot.get('trade_date'), '交易')
        cutoff = _date(snapshot.get('as_of'), '研究截止')
        queried = _date(snapshot.get('query_as_of', snapshot.get('as_of')), '报价查询截止')
    except ValueError:
        return None
    if (not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', str(snapshot.get('ticker', '')))
            or snapshot.get('currency') != 'CNY' or traded > min(cutoff, queried, _today())
            or (queried - traded).days > MAX_AGE_DAYS or not snapshot.get('source_id')
            or not snapshot.get('text') or snapshot.get('provider') != 'tushare'):
        return None
    return {'id': snapshot['source_id'], 'kind': 'api', 'source_type': 'market_data',
            'read_status': 'original_record', 'status': 'api_only',
            'title': f"{snapshot['ticker']} {traded.isoformat()} 未复权收盘价",
            'published_at': traded.isoformat(), 'text': snapshot['text'],
            'url': snapshot.get('source_url', ENDPOINT), 'provider': 'tushare',
            'retrieved_at': snapshot.get('retrieved_at'), 'value': price, 'currency': 'CNY',
            'unit': '元/股', 'ticker': snapshot['ticker'],
            'note': '单一行情 API 原始记录；尚未经独立来源交叉核验。'}


def fetch_quote(run_id, as_of=None):
    """Fetch one bounded daily window and atomically save success or failure.

    Failure never presents an old price as a newly fetched one. Earlier snapshots
    stay in market_snapshot_history; no report or valuation is rewritten.
    Cancellation and access-policy blocks propagate rather than becoming data.
    """
    _check()
    run = store.get_run(run_id)
    request = run.get('request', {})
    ticker = request.get('ticker')
    if not isinstance(ticker, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', ticker):
        raise ValueError('市场报价仅支持带 SH、SZ 或 BJ 后缀的六位 A 股代码')
    cutoff = _date(request.get('as_of'), '研究截止')
    today = _today()
    queried = _date(as_of, '报价查询截止') if as_of is not None else min(cutoff, today)
    if queried > min(cutoff, today):
        raise ValueError('报价查询截止日不能晚于研究截止日或当前日期')
    start = queried - timedelta(days=MAX_AGE_DAYS)
    result = {'artifact': 'market_quote', 'status': 'missing', 'ticker': ticker,
              'price': None, 'currency': 'CNY', 'unit': '元/股', 'trade_date': None,
              'as_of': cutoff.isoformat(), 'query_as_of': queried.isoformat(),
              'source_id': None, 'text': '', 'source_url': ENDPOINT, 'documentation_url': DOCUMENTATION,
              'provider': 'tushare', 'retrieved_at': store.now(), 'gaps': [], 'notes': [
                  '查询未复权日线收盘价，不是盘中实时价格；未与独立行情来源交叉核验。'],
              'query': {'api_name': 'daily', 'start_date': start.isoformat(), 'end_date': queried.isoformat()}}
    if cutoff > today:
        result['notes'].append('研究截止日在未来，本次仅查询至当前日期，未补造未来行情。')
    if queried < min(cutoff, today):
        result['notes'].append('本次显式查询早于研究截止日的历史报价，不代表截止日最新报价。')
    token = ''
    try:
        token = _token()
    except ValueError as error:
        result['gaps'].append('Tushare 凭据读取失败：' + _safe_error(error, ''))
    if result['gaps']:
        pass
    elif not token:
        result['gaps'].append('未配置 Tushare Token，未能取得实际市场报价。')
    else:
        try:
            latest = _latest(_daily(ticker, start, queried, token), ticker, start, queried)
            if latest is None:
                result['gaps'].append('Tushare daily 在本次最近 30 日窗口内未返回记录；可能为停牌、未上市或数据不可用，原因未核验。')
            else:
                traded, price = latest
                identity = json.dumps({'provider': 'tushare', 'ticker': ticker, 'date': traded.isoformat(), 'price': price}, sort_keys=True)
                text = (f'Tushare Pro daily 原始记录：ts_code={ticker}；trade_date={traded.isoformat()}；'
                        f'close={price}（人民币元/股，未复权收盘价）。')
                age = (queried - traded).days
                result.update(status='available', price=price, trade_date=traded.isoformat(),
                              source_id='market-quote-' + hashlib.sha256(identity.encode()).hexdigest()[:20],
                              text=text, price_age_days=age, age_days=age,
                              raw_record={'ts_code': ticker, 'trade_date': traded.strftime('%Y%m%d'), 'close': price})
                if age > 7:
                    result['notes'].append(f'该收盘价距查询截止日已有 {age} 个自然日；节假日、停牌或数据迟延原因未核验，应检查报价时效。')
        except competition.PolicyError:
            raise
        except (ProviderError, ValueError) as error:
            result['gaps'].append(_safe_error(error, token))
    _check()

    def save(current):
        if current.get('request', {}).get('ticker') != ticker or current.get('request', {}).get('as_of') != cutoff.isoformat():
            raise ValueError('查询期间研究对象或截止日已变化，未保存旧请求的报价')
        if current.get('market_snapshot'):
            current.setdefault('market_snapshot_history', []).append(deepcopy(current['market_snapshot']))
        current['market_snapshot'] = result

    store.update_run(run_id, save)
    competition.audit('market_quote_observed', run_id=run_id, snapshot=result)
    return result
