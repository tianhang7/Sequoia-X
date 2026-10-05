"""Yahoo Finance 数据源：作为 baostock 的备用/补充行情来源。

背景：baostock 偶发登录失败或返回空数据导致拉取中断（日志中「失败 1741 只」）。
本模块用 yfinance 拉取同一批 A 股日线，写入口径与 baostock 完全一致：

- ``close``  ：后复权收盘价（``auto_adjust=True``，与 baostock adjustflag="1" 对齐）
- ``raw_close``：不复权收盘价（``auto_adjust=False``，与 adjustflag="3" 对齐）
- ``turnover``：成交额。Yahoo 不提供该字段，用 ``close × volume`` 近似，
  仅供筛选器做量级粗筛（``filter_min_avg_amount``），不参与任何价格计算。

代码映射：6/9 开头 -> ``.SS``（上交所），0/2/3 开头 -> ``.SZ``（深交所），
4/8 开头 -> ``.BJ``（北交所）。
"""

from __future__ import annotations

from pathlib import Path

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

# 单次 download 的最大代码数：一次拉太多容易触发 Yahoo 限流（429）
_BATCH_SIZE = 50


def to_yahoo_symbol(symbol: str) -> str | None:
    """把纯数字 A 股代码转为 Yahoo ticker；无法识别时返回 None。"""
    if not symbol or not symbol.isdigit() or len(symbol) != 6:
        return None
    head = symbol[0]
    if head in ("6", "9"):
        return f"{symbol}.SS"
    if head in ("0", "2", "3"):
        return f"{symbol}.SZ"
    if head in ("4", "8"):
        return f"{symbol}.BJ"
    return None


def _configure_cache(cache_dir: str) -> None:
    """把 yfinance 的时区缓存指到项目目录。

    yfinance 默认写入用户缓存目录，在部分 Windows 环境下该目录不可写，
    会抛 ``peewee.OperationalError: unable to open database file``。
    """
    try:
        import yfinance as yf
    except ImportError:
        return
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(cache_dir)
    except Exception as exc:  # noqa: BLE001 - 缓存目录只影响时区换算，不该中断取数
        logger.warning(f"yfinance 缓存目录设置失败（忽略）：{exc}")


def _extract(frame, tickers: list[str], start: str, end: str, adjust: bool) -> list:
    """把 yf.download 的宽表拆成 [symbol, date, o, h, l, c, volume, turnover] 行。"""
    import pandas as pd

    rows: list = []
    if frame is None or frame.empty:
        return rows

    # group_by="ticker" 时列是 MultiIndex[(ticker, field)]；单只股票退化为 Index
    for ticker in tickers:
        if isinstance(frame.columns, pd.MultiIndex):
            if ticker not in frame.columns.get_level_values(0):
                continue
            sub = frame[ticker].dropna(how="all")
        else:
            if tickers[0] != ticker:
                continue
            sub = frame.dropna(how="all")
        if sub.empty or "Close" not in sub.columns:
            continue

        for idx, row in sub.iterrows():
            close = row.get("Close")
            if close is None or pd.isna(close):
                continue
            volume = row.get("Volume")
            volume = float(volume) if volume is not None and not pd.isna(volume) else 0.0
            day = idx.strftime("%Y-%m-%d")
            if day < start or day > end:
                continue
            rows.append([
                ticker,
                day,
                float(row["Open"]) if not pd.isna(row.get("Open")) else None,
                float(row["High"]) if not pd.isna(row.get("High")) else None,
                float(row["Low"]) if not pd.isna(row.get("Low")) else None,
                float(close),
                volume,
                float(close) * volume,
            ])
    return rows


def _download(tickers: list[str], start: str, end: str, adjust: bool) -> list:
    """按批下载日线，返回标准化行列表（失败批次仅记日志）。

    每行格式：``[ticker, date, open, high, low, close, volume, turnover]``。
    """
    import yfinance as yf

    rows: list = []
    for i in range(0, len(tickers), _BATCH_SIZE):
        batch = tickers[i : i + _BATCH_SIZE]
        try:
            frame = yf.download(
                batch,
                start=start,
                end=_end_exclusive(end),
                auto_adjust=adjust,
                actions=False,
                progress=False,
                group_by="ticker",
                threads=True,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - 单批失败不影响其余批次
            logger.warning(f"Yahoo 批次 [{i}/{len(tickers)}] 下载失败：{exc}")
            continue
        rows.extend(_extract(frame, batch, start, end, adjust))
    return rows


def _end_exclusive(end: str) -> str:
    """yf 的 end 是开区间，需要 +1 天才能覆盖当天。"""
    from datetime import date, timedelta

    return (date.fromisoformat(end) + timedelta(days=1)).isoformat()


def fetch_daily(
    symbols: list[str],
    start: str,
    end: str,
    adjust: bool,
    cache_dir: str = "data/yf_cache",
) -> list[tuple[str, list]]:
    """批量拉取日线，返回 ``[(symbol, 行列表), ...]``（行格式见 ``_extract``）。

    Args:
        symbols: 纯数字 A 股代码列表。
        start: 起始日 ``YYYY-MM-DD``（含）。
        end: 结束日 ``YYYY-MM-DD``（含）。
        adjust: True 取后复权价（写 close），False 取不复权价（写 raw_close）。
        cache_dir: yfinance 缓存目录。

    无法映射为 Yahoo ticker 的代码直接忽略（Yahoo 无此标的）。
    """
    _configure_cache(cache_dir)

    mapping: dict[str, str] = {}
    for symbol in symbols:
        ticker = to_yahoo_symbol(symbol)
        if ticker:
            mapping[ticker] = symbol

    if not mapping:
        return []

    tickers = list(mapping)
    logger.info(f"Yahoo Finance 拉取 {len(tickers)} 只（{start}~{end}，{'后复权' if adjust else '不复权'}）")

    rows = _download(tickers, start, end, adjust)
    grouped: dict[str, list] = {}
    for row in rows:
        symbol = mapping.get(row[0])
        if symbol:
            grouped.setdefault(symbol, []).append(row)

    logger.info(f"Yahoo Finance 返回 {len(rows)} 行 / 命中 {len(grouped)} 只股票")
    return [(symbol, grouped[symbol]) for symbol in mapping.values() if symbol in grouped]