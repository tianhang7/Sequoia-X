"""行情数据源：搜狐历史 K 线（主力，免登录）+ 东财 push2（补充不复权）。

实测结论（2026-09-27，本机网络）：
- push2his（历史主力）被运营商/地域性阻断：TCP 建连即被 RST，换 UA/Referer/Host
  均无效；push2.eastmoney.com 的 kline/get 偶发返回空壳（dktotal=0，需重试）。
- 搜狐 hisHq（q.stock.sohu.com/hisHq?code=cn_XXXXXX...）稳定可用，长窗口一次
  返回（6 年约 134KB），无登录、无 key；但只有“实际成交价”（可执行价），
  北交所代码需加 bj_ 前缀且部分老代码报 non-existent。
- 腾讯 day/query、新浪 getKLineData 同期实测已下线/报参错，不可用。

因此 engine 双源策略为：搜狐（不复权 close + raw_close 同源）优先，
东财 push2 fqt=2（后复权）补充策略用 close；两者失败子集再降级 baostock。
腾讯 qt.gtimg 仅做实时快照备用（fetch_spot）。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import requests

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

KLINE_URL = "https://push2.eastmoney.com/api/qt/stock/kline/get"
SOHU_URL = "https://q.stock.sohu.com/hisHq"
SPOT_URL = "https://qt.gtimg.cn/q={codes}"

# fqt 口径：0=不复权 / 1=前复权 / 2=后复权
FQT_RAW = 0
FQT_QFQ = 1
FQT_HFQ = 2

_PAGE_LIMIT = 1000  # 单次请求最多返回条数（实测 1000 安全）
_TIMEOUT = 15
_RETRIES = 3
_SOHU_BACKOFF = (2, 5, 10)  # 搜狐 503/429 限流时退避秒数（全量补拉必被限流）

# push2his 无 UA/Referer 时直接断连接（RemoteDisconnected），必须带浏览器头
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    """复用 TCP 连接的 Session（5000+ 请求时避免每次建连；带浏览器头）。"""
    global _SESSION
    if _SESSION is None:
        sess = requests.Session()
        sess.headers.update(_HEADERS)
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16,
                                                max_retries=0)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _SESSION = sess
    return _SESSION


def to_secid(symbol: str) -> str:
    """纯数字代码 → 东财 secid（沪市 1. / 深市·北交所 0.）。"""
    prefix = "1" if symbol[:1] in ("6", "9") else "0"
    return f"{prefix}.{symbol}"


def _parse_klines(symbol: str, klines: list[str]) -> list:
    """解析 klines 字符串数组 → [symbol,date,open,high,low,close,volume,turnover]。

    东财字段顺序：日期,开,收,高,低,成交量(股),成交额(元),振幅,...。异常行跳过。
    """
    rows = []
    for line in klines or []:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        try:
            rows.append([symbol, parts[0], float(parts[1]), float(parts[3]),
                         float(parts[4]), float(parts[2]), float(parts[5]), float(parts[6])])
        except (TypeError, ValueError):
            continue
    return rows
def _sohu_code(symbol: str) -> str:
    """纯数字代码 → 搜狐 code（沪深 cn_ 前缀；北交所 4/8 开头用 bj_）。"""
    prefix = "bj" if symbol[:1] in ("4", "8") else "cn"
    return f"{prefix}_{symbol}"


def _parse_sohu(symbol: str, hq: list) -> list:
    """解析搜狐 hq 数组 → [symbol,date,open,high,low,close,volume,turnover]。

    搜狐行格式：[日期,开,收,涨跌额,涨跌幅,最低,最高,成交量(手),成交额(万),换手率]。
    无成交量/成交额→换算为股/元。异常行跳过。
    """
    rows = []
    for item in hq or []:
        if len(item) < 9:
            continue
        try:
            rows.append([symbol, item[0], float(item[1]), float(item[6]),
                         float(item[5]), float(item[2]),
                         float(item[7]) * 100.0, float(item[8]) * 10000.0])
        except (TypeError, ValueError):
            continue
    return rows


def fetch_sohu(symbol: str, start: str, end: str, requester=None) -> list:
    """搜狐单只历史 K 线（start/end YYYY-MM-DD）；返回 baostock 同格式行。"""
    code = _sohu_code(symbol)
    params = {"code": code, "start": start.replace("-", ""), "end": end.replace("-", ""),
              "stat": "1", "order": "D", "period": "d", "rt": "json"}
    if requester is not None:
        data = requester(SOHU_URL, params, _TIMEOUT)
    else:
        data = None
        for attempt in range(_RETRIES):
            try:
                resp = _session().get(SOHU_URL, params=params, timeout=_TIMEOUT)
                if resp.status_code in (429, 503):
                    raise RuntimeError(f"HTTP={resp.status_code}（限流，退避重试）")
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP={resp.status_code}")
                data = resp.json()
                break
            except Exception as exc:  # noqa: BLE001 - 重试后仍失败则本只记缺失
                wait = _SOHU_BACKOFF[min(attempt, len(_SOHU_BACKOFF) - 1)]
                logger.warning(f"搜狐 K 线 {symbol} 失败（{attempt + 1}/{_RETRIES}，{wait}s 后重试）: {exc}")
                time.sleep(wait)
        if data is None:
            return []
    hq = ((data or [{}])[0] or {}).get("hq") or []
    rows = _parse_sohu(symbol, hq)
    beg, end_c = start.replace("-", ""), end.replace("-", "")
    return [r for r in rows if beg <= r[1].replace("-", "") <= end_c]


def fetch_sohu_many(symbols, start, end, max_workers=8, requester=None, delay=0.0):
    """搜狐多只并发；返回 (rows, failed_symbols)。requester 传入时退化串行。

    ``delay``：每次请求前 sleep 秒数（搜狐限流时建议 0.15~0.3，
    配合 max_workers=4；单只 2~3s + 成功后仍需节流）。
    """
    def _one(symbol):
        if delay:
            time.sleep(delay)
        return fetch_sohu(symbol, start, end, requester=requester)

    rows, failed = [], []
    if requester is not None:
        for s in symbols:
            got = _one(s)
            (rows.extend(got) if got else failed.append(s))
        return rows, failed
    workers = max(1, min(max_workers, len(symbols))) if symbols else 1
    from concurrent.futures import ThreadPoolExecutor as Pool
    with Pool(max_workers=workers) as pool:
        for s, got in zip(symbols, pool.map(_one, symbols)):
            (rows.extend(got) if got else failed.append(s))
    return rows, failed


def fetch_kline(symbol, beg, end, fqt=FQT_HFQ, requester=None):
    """拉取单只股票窗口内 K 线，自动分页；返回 baostock 同格式行列表。

    beg/end 为 YYYYMMDD。requester 供测试注入，签名 (url, params, timeout)
    -> dict（已解析 JSON）；传入时跳过 requests/重试 sleep。
    """
    secid = to_secid(symbol)
    all_rows = []
    cursor = beg
    while True:
        params = {"secid": secid, "klt": 101, "fqt": fqt, "beg": cursor,
                  "end": end, "lmt": _PAGE_LIMIT,
                  "fields1": "f1,f2,f3,f4,f5",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58"}
        if requester is not None:
            data = requester(KLINE_URL, params, _TIMEOUT)
        else:
            data = None
            for attempt in range(_RETRIES):
                try:
                    resp = requests.get(KLINE_URL, params=params, timeout=_TIMEOUT,
                                        headers=_HEADERS)
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP={resp.status_code}")
                    data = resp.json()
                    break
                except Exception as exc:  # noqa: BLE001 - 重试后仍失败则本只记缺失
                    logger.warning(f"东财 K 线 {symbol} 失败（{attempt + 1}/{_RETRIES}）: {exc}")
                    time.sleep(1 + attempt)
            if data is None:
                return all_rows
        payload = (data or {}).get("data") or {}
        klines = payload.get("klines") or []
        rows = _parse_klines(symbol, klines)
        all_rows.extend(rows)
        if len(klines) < _PAGE_LIMIT or not rows:
            break
        last_day = rows[-1][1].replace("-", "")
        if last_day >= end.replace("-", "") or last_day <= cursor:
            break
        cursor = last_day
        if requester is not None:
            break  # 测试替身单页返回，避免翻页循环
    return all_rows


def fetch_many(symbols, start, end, fqt=FQT_HFQ, max_workers=8, requester=None):
    """多只并发拉取；返回 (rows, failed_symbols)。

    start/end 为 YYYY-MM-DD。IO 密集用线程池（Windows 友好）。
    requester 传入时退化串行（单元测试用）。
    """
    beg, end_c = start.replace("-", ""), end.replace("-", "")

    def _one(symbol):
        return fetch_kline(symbol, beg, end_c, fqt=fqt, requester=requester)

    rows, failed = [], []
    if requester is not None:
        for s in symbols:
            got = _one(s)
            (rows.extend(got) if got else failed.append(s))
        return rows, failed
    workers = max(1, min(max_workers, len(symbols))) if symbols else 1
    from concurrent.futures import ThreadPoolExecutor as Pool
    with Pool(max_workers=workers) as pool:
        for s, got in zip(symbols, pool.map(_one, symbols)):
            (rows.extend(got) if got else failed.append(s))
    return rows, failed


def fetch_windows(windows, fqt=FQT_HFQ, max_workers=8, requester=None):
    """按各自窗口 [(symbol, start, end)] 拉取（start/end 为 YYYY-MM-DD）。

    日常增量每只起点不同（本地 MAX(date)+1），无法用统一窗口；
    本函数在线程池内逐只调 fetch_kline。返回 (rows, failed_symbols)。
    requester 传入时退化串行（单元测试用）。
    """
    def _one(task):
        symbol, start, end = task
        return fetch_kline(symbol, start.replace("-", ""), end.replace("-", ""),
                           fqt=fqt, requester=requester)

    rows, failed = [], []
    if requester is not None:
        for t in windows:
            got = _one(t)
            (rows.extend(got) if got else failed.append(t[0]))
        return rows, failed
    workers = max(1, min(max_workers, len(windows))) if windows else 1
    from concurrent.futures import ThreadPoolExecutor as Pool
    with Pool(max_workers=workers) as pool:
        for t, got in zip(windows, pool.map(_one, windows)):
            (rows.extend(got) if got else failed.append(t[0]))
    return rows, failed


def fetch_spot(symbols, timeout=10):
    """腾讯实时快照备用：{纯数字代码: {name, price, prev_close, open}}；失败返回 {}。"""
    codes = [("sh" if s[:1] in ("6", "9") else "sz") + s for s in symbols]
    try:
        resp = requests.get(SPOT_URL.format(codes=",".join(codes)), timeout=timeout)
        text = resp.content.decode("gbk", errors="ignore")
    except Exception as exc:  # noqa: BLE001 - 备用链路失败即降级
        logger.warning(f"腾讯实时快照失败（已降级）：{exc}")
        return {}
    out = {}
    for line in text.strip().splitlines():
        if '="' not in line:
            continue
        head, payload = line.split('="', 1)
        fields = payload.strip().strip('";').split("~")
        try:
            out[head.split("_")[-1][-6:]] = {
                "name": fields[1] if len(fields) > 1 else "",
                "price": float(fields[3]) if len(fields) > 3 and fields[3] else None,
                "prev_close": float(fields[4]) if len(fields) > 4 and fields[4] else None,
                "open": float(fields[5]) if len(fields) > 5 and fields[5] else None,
            }
        except (TypeError, ValueError):
            continue
    return out
