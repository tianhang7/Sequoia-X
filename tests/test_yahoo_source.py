"""Yahoo Finance 备用数据源测试：代码映射、行解析、补齐通道。

全部离线：monkeypatch 掉 yfinance 的 download，不发起任何网络请求。
"""

from __future__ import annotations

import pandas as pd
import pytest

from sequoia_x.data.yahoo_source import _extract, fetch_daily, to_yahoo_symbol


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000", "600000.SS"),
        ("601318", "601318.SS"),
        ("900901", "900901.SS"),
        ("000001", "000001.SZ"),
        ("002415", "002415.SZ"),
        ("300750", "300750.SZ"),
        ("830799", "830799.BJ"),
        ("430418", "430418.BJ"),
        ("12345", None),
        ("abcdef", None),
        ("", None),
    ],
)
def test_to_yahoo_symbol(symbol, expected):
    """沪深北三大市场的代码前缀映射正确。"""
    assert to_yahoo_symbol(symbol) == expected


def _wide_frame():
    """构造 group_by="ticker" 的宽表（列 = MultiIndex[(ticker, field)]）。"""
    cols = pd.MultiIndex.from_product(
        [["600000.SS", "000001.SZ"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    idx = pd.to_datetime(["2026-09-24", "2026-09-25"]).tz_localize("Asia/Shanghai")
    data = [
        [9.0, 9.2, 8.9, 9.1, 1000, 12.0, 12.3, 11.8, 12.0, 2000],
        [9.3, 9.4, 9.1, 9.2, 1100, 12.2, 12.6, 12.0, 12.3, 2100],
    ]
    return pd.DataFrame(data, index=idx, columns=cols)


def test_extract_splits_multiticker_frame():
    """宽表按 ticker 正确拆分为标准行，turnover = close × volume。"""
    rows = _extract(_wide_frame(), ["600000.SS", "000001.SZ"], "2026-09-24", "2026-09-25", True)

    assert len(rows) == 4
    first = [r for r in rows if r[0] == "600000.SS"][0]
    assert first[1] == "2026-09-24"
    assert first[5] == pytest.approx(9.1)  # Close
    assert first[6] == pytest.approx(1000)  # Volume
    assert first[7] == pytest.approx(9.1 * 1000)  # 近似成交额


def test_extract_filters_out_of_window_dates():
    """窗口外的日期被丢弃（Yahoo 会多返回边界日）。"""
    rows = _extract(_wide_frame(), ["600000.SS"], "2026-09-25", "2026-09-25", True)
    assert len(rows) == 1
    assert rows[0][1] == "2026-09-25"


def test_extract_tolerates_empty_frame():
    """空表/None 返回空列表，不抛异常。"""
    assert _extract(None, ["600000.SS"], "2026-09-24", "2026-09-25", True) == []
    assert _extract(pd.DataFrame(), ["600000.SS"], "2026-09-24", "2026-09-25", True) == []


def test_fetch_daily_groups_by_symbol(monkeypatch):
    """fetch_daily 返回 (纯数字代码, 行) 列表，且未映射代码被忽略。"""
    import sys

    import sequoia_x.data.yahoo_source as ys

    captured: dict = {}

    class _FakeYf:
        @staticmethod
        def set_tz_cache_location(path):
            captured["cache"] = path

        @staticmethod
        def download(batch, **kwargs):
            captured["batch"] = batch
            captured["auto_adjust"] = kwargs["auto_adjust"]
            return _wide_frame()

    monkeypatch.setattr(ys, "_configure_cache", lambda cache_dir: None)
    monkeypatch.setitem(sys.modules, "yfinance", _FakeYf)

    result = fetch_daily(
        ["600000", "000001", "12345"], "2026-09-24", "2026-09-25", adjust=False
    )

    assert captured["auto_adjust"] is False  # 不复权 → raw_close 口径
    assert "12345" not in captured["batch"]
    assert {s for s, _ in result} == {"600000", "000001"}
    assert all(len(rows) == 2 for _, rows in result)