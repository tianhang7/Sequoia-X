"""测试辅助：构造最小行情数据库与合成K线序列。"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta


def weekdays(start: str, n: int) -> list[str]:
    """从 start 起连续 n 个工作日（ISO 日期字符串）。"""
    out: list[str] = []
    d = date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def weekdays_ending(end: str, n: int) -> list[str]:
    """以 end 收尾的连续 n 个工作日（保证结果最后一天 == end）。"""
    out: list[str] = []
    d = date.fromisoformat(end)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    out.reverse()
    assert out[-1] == end, f"end {end} 不是交易日"
    return out


def bar(d: str, o: float, h: float, l: float, c: float,
        v: float = 1_000_000, t: float = 200_000_000) -> tuple:
    """单根K线行（date, open, high, low, close, volume, turnover）。"""
    return (d, o, h, l, c, v, t)


def flat_bars(dates: list[str], price: float, **kw) -> list[tuple]:
    """十字星序列：open=high=low=close=price，用于填充历史。"""
    return [bar(d, price, price, price, price, **kw) for d in dates]


def insert_bars(db_path: str, symbol: str, rows: list[tuple]) -> None:
    """把K线行写入 stock_daily（INSERT OR REPLACE）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily "
            "(symbol, date, open, high, low, close, volume, turnover) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(symbol, *r) for r in rows],
        )
        conn.commit()
    finally:
        conn.close()


def set_raw_closes(db_path: str, symbol: str, closes: dict[str, float]) -> None:
    """给已有行的 raw_close 列赋值（测试不复权价路径用）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "UPDATE stock_daily SET raw_close = ? WHERE symbol = ? AND date = ?",
            [(v, symbol, d) for d, v in closes.items()],
        )
        conn.commit()
    finally:
        conn.close()
