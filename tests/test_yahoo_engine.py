"""数据引擎 Yahoo Finance 备用通道测试（fetch_daily 全部 mock，离线运行）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine

from tests._seed import bar, flat_bars, insert_bars, weekdays_ending


def test_backfill_raw_yahoo_updates_rows(monkeypatch, tmp_path):
    """Yahoo 通道只 UPDATE raw_close，不新增行；已齐股票跳过。"""
    dates = weekdays_ending("2026-09-25", 5)
    settings = Settings(db_path=str(Path(tmp_path) / "test.db"), start_date=dates[0])
    engine = DataEngine(settings)
    insert_bars(engine.db_path, "600001", flat_bars(dates, 1100.0))
    insert_bars(engine.db_path, "600002", flat_bars(dates, 2200.0))

    monkeypatch.setattr(
        "sequoia_x.data.yahoo_source.fetch_daily",
        lambda symbols, start, end, adjust, **kw: [
            # 行格式：[ticker, date, open, high, low, close, volume, turnover]
            (s, [["TICKER", d, 1.0, 1.0, 1.0, 11.0, 100.0, 1100.0] for d in dates])
            for s in symbols
        ],
    )

    result = engine.backfill_raw_yahoo(["600001", "600002"])
    assert result["updated"] == 10  # 2 只 × 5 天
    assert result["failed"] == []
    assert engine.get_raw_close("600001") == ("2026-09-25", 11.0)
    # close 仍为原后复权值，未被 Yahoo 覆盖
    assert float(engine.get_ohlcv("600001")["close"].iloc[-1]) == pytest.approx(1100.0)

    again = engine.backfill_raw_yahoo(["600001", "600002"])
    assert again == {"updated": 0, "skipped": 2, "failed": []}


def test_backfill_raw_yahoo_reports_missing(monkeypatch, tmp_path):
    """Yahoo 无数据的代码计入 failed，可重跑续传。"""
    settings = Settings(db_path=str(Path(tmp_path) / "test.db"), start_date="2026-09-24")
    engine = DataEngine(settings)
    dates = weekdays_ending("2026-09-25", 3)
    insert_bars(engine.db_path, "600001", flat_bars(dates, 1100.0))

    monkeypatch.setattr("sequoia_x.data.yahoo_source.fetch_daily", lambda *a, **k: [])

    result = engine.backfill_raw_yahoo(["600001"])
    assert result["updated"] == 0
    assert result["failed"] == ["600001"]


def test_backfill_history_yahoo_inserts_adjusted_rows(monkeypatch, tmp_path):
    """Yahoo 历史回填写入后复权 OHLCV，且按本地 MAX(date) 续传。"""
    settings = Settings(db_path=str(Path(tmp_path) / "test.db"), start_date="2026-09-23")
    engine = DataEngine(settings)
    insert_bars(engine.db_path, "600001", [bar("2026-09-23", 10, 10, 10, 10)])

    calls: list = []

    def _fake(symbols, start, end, adjust, **kw):
        calls.append((start, end, adjust))
        # 行格式：[ticker, date, open, high, low, close, volume, turnover]
        return [("600001", [["600001.SS", "2026-09-24", 1.0, 1.2, 0.9, 1.1, 5000.0, 5500.0]])]

    monkeypatch.setattr("sequoia_x.data.yahoo_source.fetch_daily", _fake)

    result = engine.backfill_history_yahoo(["600001"])
    assert result["updated"] == 1
    assert calls[0][2] is True  # 后复权口径
    assert calls[0][0] == "2026-09-24"  # 续传起点 = 本地 MAX(date) + 1

    df = engine.get_ohlcv("600001")
    assert float(df["close"].iloc[-1]) == pytest.approx(1.1)
    assert float(df["turnover"].iloc[-1]) == pytest.approx(5500.0)


def test_backfill_history_yahoo_skips_up_to_date(monkeypatch, tmp_path):
    """本地已有最新K线的股票直接跳过，不发起下载。"""
    from datetime import date, timedelta

    settings = Settings(db_path=str(Path(tmp_path) / "test.db"), start_date="2026-09-23")
    engine = DataEngine(settings)
    today = date.today()
    insert_bars(engine.db_path, "600001", [bar(today.isoformat(), 10, 10, 10, 10)])
    insert_bars(
        engine.db_path,
        "600002",
        [bar((today - timedelta(days=10)).isoformat(), 10, 10, 10, 10)],
    )

    monkeypatch.setattr(
        "sequoia_x.data.yahoo_source.fetch_daily",
        lambda *a, **k: pytest.fail("不应为已最新股票发起下载"),
    )

    result = engine.backfill_history_yahoo(["600001"])
    assert result == {"updated": 0, "skipped": 1, "failed": []}


def test_backfill_history_yahoo_up_to_date_not_reported_as_failed(monkeypatch, tmp_path):
    """本地已追平最新交易日时：Yahoo 无新行属正常完成，计入 skipped 而非 failed。

    回归场景：MAX(date) 恰为最近交易日时 start 落到休市日之后，
    Yahoo 窗口内无数据，旧逻辑会把全部股票误报为 failed。
    """
    from datetime import date, timedelta

    settings = Settings(db_path=str(Path(tmp_path) / "test.db"), start_date="2026-09-01")
    engine = DataEngine(settings)
    today = date.today()
    insert_bars(engine.db_path, "600001", [bar(today.isoformat(), 10, 10, 10, 10)])

    monkeypatch.setattr(
        "sequoia_x.data.yahoo_source.fetch_daily",
        # 行格式：[ticker, date, open, high, low, close, volume, turnover]
        lambda *a, **k: [("600001", [["600001.SS", today.isoformat(), 1.0, 1.0, 1.0, 9.9, 100.0, 990.0]])],
    )

    # start = MAX(date)+1 落在明天，窗口内没有晚于 start 的行 → df 为空
    result = engine.backfill_history_yahoo(["600001"])
    assert result["updated"] == 0
    assert result["failed"] == []
    assert result["skipped"] == 1
    assert (today + timedelta(days=1)).isoformat() > today.isoformat()


def test_backfill_history_yahoo_reports_failures(monkeypatch, tmp_path):
    """Yahoo 无数据的代码计入 failed。"""
    settings = Settings(db_path=str(Path(tmp_path) / "test.db"), start_date="2026-09-24")
    engine = DataEngine(settings)

    monkeypatch.setattr("sequoia_x.data.yahoo_source.fetch_daily", lambda *a, **k: [])

    result = engine.backfill_history_yahoo(["600001", "000002"])
    assert result["updated"] == 0
    assert sorted(result["failed"]) == ["000002", "600001"]