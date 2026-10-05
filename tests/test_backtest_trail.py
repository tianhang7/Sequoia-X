"""移动止损回测：MA_N 离场、启用阈值、不设时间止损、硬止损兜底。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from sequoia_x.backtest import Backtester
from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine

from tests._seed import weekdays_ending


def _frame(rows: list[tuple]) -> pd.DataFrame:
    """由 (date, o, h, l, c) 构造回测用行情帧，预计算 ma10/ma20。"""
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    return df


def _bt(tmp_path, **kw) -> Backtester:
    settings = Settings(db_path=str(Path(tmp_path) / "t.db"), start_date="2026-01-01")
    bt = Backtester(settings=settings, engine=DataEngine(settings), strategies=[], **kw)
    return bt


def test_trail_ma_exits_when_close_drops_below(tmp_path):
    """浮盈达标后收盘跌破 MA10 → 次日开盘离场（不再等 MA20/时间止损）。"""
    dates = weekdays_ending("2026-09-25", 40)
    # 先涨 20 天（浮盈达标），再连续下跌击穿 MA10
    rows = []
    price = 10.0
    for i, d in enumerate(dates):
        if i < 20:
            price *= 1.02
        else:
            price *= 0.97
        rows.append((d, round(price, 3), round(price * 1.01, 3),
                     round(price * 0.99, 3), round(price, 3)))
    df = _frame(rows)

    bt = _bt(tmp_path, trail_ma=10, trail_activate=0.05, time_stop_days=3)
    trade = bt._simulate_one(df, "T", "600001", dates[0])

    assert trade is not None
    assert trade.reason == "跌破MA10"
    assert trade.hold_bars < 38  # 远早于窗口结束，说明是被 MA10 扫出的


def test_trail_activate_blocks_exit_below_threshold(tmp_path):
    """浮盈未达启用阈值时，跌破 MA10 也不离场（继续持有）。"""
    dates = weekdays_ending("2026-09-25", 40)
    # 温和下跌：全程无浮盈
    rows = []
    price = 10.0
    for d in dates:
        price *= 0.995
        rows.append((d, round(price, 3), round(price * 1.01, 3),
                     round(price * 0.99, 3), round(price, 3)))
    df = _frame(rows)

    bt = _bt(tmp_path, trail_ma=10, trail_activate=0.05, stop_loss=0.10, time_stop_days=3)
    trade = bt._simulate_one(df, "T", "600001", dates[0])

    # 触发的是硬止损(10%)而非 MA10 —— 说明阈值成功屏蔽了移动止损
    assert trade is not None
    assert trade.reason == "硬止损"


def test_trail_mode_disables_time_stop(tmp_path):
    """移动止损模式下时间止损不生效：持续小跌不会被「时间止损」砍掉。"""
    dates = weekdays_ending("2026-09-25", 40)
    rows = []
    price = 10.0
    for d in dates:
        price *= 0.999  # 极缓下跌，长期不盈利但也不会大跌触发硬止损
        rows.append((d, round(price, 3), round(price * 1.001, 3),
                     round(price * 0.999, 3), round(price, 3)))
    df = _frame(rows)

    bt = _bt(tmp_path, trail_ma=10, trail_activate=0.05, stop_loss=0.50, time_stop_days=5)
    trade = bt._simulate_one(df, "T", "600001", dates[0])

    assert trade is not None
    assert trade.reason != "时间止损"
    assert trade.hold_bars > 5  # 明显超过 time_stop_days=5


def test_baseline_still_uses_time_stop(tmp_path):
    """对照组：不启用移动止损时，原有 MA20/时间止损逻辑保持不变（回归保护）。"""
    dates = weekdays_ending("2026-09-25", 40)
    rows = []
    price = 10.0
    for d in dates:
        price *= 0.999
        rows.append((d, round(price, 3), round(price * 1.001, 3),
                     round(price * 0.999, 3), round(price, 3)))
    df = _frame(rows)

    bt = _bt(tmp_path, trail_ma=None, stop_loss=0.50, time_stop_days=5)
    trade = bt._simulate_one(df, "T", "600001", dates[0])

    assert trade is not None
    assert trade.reason == "时间止损"


def test_trail_still_respects_hard_stop(tmp_path):
    """移动止损不能取代硬止损：暴跌仍按硬止损离场。"""
    dates = weekdays_ending("2026-09-25", 40)
    rows = []
    price = 10.0
    for i, d in enumerate(dates):
        if i >= 3:
            price *= 0.85  # 立刻腰斩式下跌
        rows.append((d, round(price, 3), round(price * 1.01, 3),
                     round(price * 0.99, 3), round(price, 3)))
    df = _frame(rows)

    bt = _bt(tmp_path, trail_ma=10, trail_activate=0.0, stop_loss=0.07)
    trade = bt._simulate_one(df, "T", "600001", dates[0])

    assert trade is not None
    assert trade.reason == "硬止损"


def test_trail_defaults_zero_activation(tmp_path):
    """trail_activate 缺省为 0.0：不设阈值时从第一根K线起即可触发移动止损。"""
    bt = _bt(tmp_path, trail_ma=10)
    assert bt.trail_activate == 0.0
    assert bt.trail_ma == 10


def test_trail_ignores_missing_ma_column(tmp_path):
    """行情帧缺少 ma10 列时降级：不因找不到列而崩溃。"""
    dates = weekdays_ending("2026-09-25", 30)
    rows = [(d, 10.0, 10.1, 9.9, 10.0 + i * 0.01) for i, d in enumerate(dates)]
    df = _frame(rows).drop(columns=["ma10"])

    bt = _bt(tmp_path, trail_ma=10, stop_loss=0.50)
    trade = bt._simulate_one(df, "T", "600001", dates[0])
    # 降级后走 MA20/时间止损分支，不会抛异常
    assert trade is not None