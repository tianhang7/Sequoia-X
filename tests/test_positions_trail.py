"""持仓移动止损（POSITION_TRAIL_MA）测试：MA_N 离场、浮盈阈值、停用时间止损、硬止损兜底。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.portfolio import PositionManager

from tests._seed import bar, insert_bars, weekdays_ending

AS_OF = "2026-09-25"
DATES = weekdays_ending(AS_OF, 40)


def _setup(tmp_path, **kw) -> PositionManager:
    defaults = dict(
        db_path=str(Path(tmp_path) / "t.db"),
        start_date="2026-06-01",
        position_trail_ma=10,
        position_trail_activate=0.05,
    )
    defaults.update(kw)
    settings = Settings(**defaults)
    return PositionManager(DataEngine(settings), settings)


def _seed(engine: DataEngine, symbol: str, closes: list[float]) -> None:
    insert_bars(
        engine.db_path,
        symbol,
        [bar(d, c, c, c, c) for d, c in zip(DATES, closes)],
    )


def test_default_is_ma10_trailing():
    """默认配置即 MA10 + 浮盈5%启用（与回测 --bt-trail-ma 10 同口径）。"""
    s = Settings()
    assert s.position_trail_ma == 10
    assert s.position_trail_activate == pytest.approx(0.05)


def _rise_then_cool() -> list[float]:
    """冲高到 12（浮盈20%）后回落至 10.55：收盘严格低于 MA10(≈10.78)，且守住 +5%。

    回落必须守住 +5%——移动止损的启用条件看的是**当前**浮盈（与回测
    ``armed = close/entry - 1 >= trail_activate`` 同口径），浮盈回吐到阈值
    以下就不再触发离场。回落分两段是为���让 MA10 明显高于收盘（平台期取等值）。
    """
    return [10.0] * 20 + [12.0] * 5 + [11.0] * 8 + [10.55] * (len(DATES) - 33)


def test_trail_exit_when_profitable_then_below_ma10(tmp_path):
    """浮盈仍超5% 且收盘跌破 MA10 → 移动止损离场。"""
    pm = _setup(tmp_path)
    _seed(pm.engine, "600201", _rise_then_cool())
    pm.add("600201", buy_date=DATES[0], buy_price=10.0, qty=100)

    signals = pm.evaluate(as_of=AS_OF)
    assert len(signals) == 1
    assert signals[0].reason == "跌破MA10"
    assert "移动止损" in signals[0].detail
    # 渲染不报错
    assert "600201" in signals[0].render_html()


def test_no_trail_exit_before_profit_threshold(tmp_path):
    """浮盈未达5%阈值时，即使跌破 MA10 也不离场。"""
    pm = _setup(tmp_path)
    # 最高只到 10.3（浮盈3% < 5%），随后回落
    closes = [10.0] * 20 + [10.3] * 5 + [9.7] * (len(DATES) - 25)
    _seed(pm.engine, "600202", closes)
    pm.add("600202", buy_date=DATES[0], buy_price=10.0, qty=100)

    assert pm.evaluate(as_of=AS_OF) == []


def test_trail_mode_disables_time_stop(tmp_path):
    """移动止损模式下时间止损停用：长期微亏不会被时间止损砍掉。"""
    pm = _setup(tmp_path, position_stop_loss=0.50)
    # 全程缓慢下跌，无浮盈 → 既不满足移动止损，也不触硬止损
    closes = [10.0 - i * 0.01 for i in range(len(DATES))]
    _seed(pm.engine, "600203", closes)
    pm.add("600203", buy_date=DATES[0], buy_price=10.0, qty=100)

    # 持有 39 天远超 time_stop_days=10，但移动止损模式下不产生信号
    assert pm.evaluate(as_of=AS_OF) == []


def test_legacy_mode_still_uses_time_stop(tmp_path):
    """回退模式（TRAIL_MA=0）保留原时间止损行为（回归保护）。"""
    pm = _setup(tmp_path, position_trail_ma=0, position_stop_loss=0.50)
    # 横盘在 9.9：MA20 == 现价不触发跌破；现价 ≤ 买入价 → 命中时间止损
    closes = [10.0] + [9.9] * (len(DATES) - 1)
    _seed(pm.engine, "600204", closes)
    pm.add("600204", buy_date=DATES[0], buy_price=10.0, qty=100)

    signals = pm.evaluate(as_of=AS_OF)
    assert len(signals) == 1
    assert signals[0].reason == "时间止损"


def test_hard_stop_takes_priority_over_trail(tmp_path):
    """硬止损优先级最高：浮盈达标后暴跌，仍按硬止损而非移动止损离场。"""
    pm = _setup(tmp_path)
    # 先冲高到12（浮盈达标），再暴跌至 8.0（击穿 -7% 硬止损）
    closes = [10.0] * 20 + [12.0] * 3 + [8.0] * (len(DATES) - 23)
    _seed(pm.engine, "600205", closes)
    pm.add("600205", buy_date=DATES[0], buy_price=10.0, qty=100)

    signals = pm.evaluate(as_of=AS_OF)
    assert len(signals) == 1
    assert signals[0].reason == "硬止损"


def test_trail_needs_enough_history(tmp_path):
    """K线不足 MA_N 根时不计算移动止损（避免均线 NaN 误报）。"""
    pm = _setup(tmp_path)
    short = weekdays_ending(AS_OF, 5)
    # 只有 5 根K线（< MA10），价格暴跌也不会触发移动止损
    closes = [10.0, 10.0, 12.0, 12.0, 12.0]
    insert_bars(
        pm.engine.db_path,
        "600206",
        [bar(d, c, c, c, c) for d, c in zip(short, closes)],
    )
    pm.add("600206", buy_date=short[0], buy_price=10.0, qty=100)

    assert pm.evaluate(as_of=AS_OF) == []


def test_trail_respects_raw_price_basis(tmp_path):
    """raw_close 存在时移动止损按原始价坐标系判断，标记 basis=raw。"""
    from tests._seed import set_raw_closes

    pm = _setup(tmp_path)
    _seed(pm.engine, "600207", _rise_then_cool())
    # 当日原始价 = 后复权收盘 10.55 × scale(0.5)
    set_raw_closes(pm.engine.db_path, "600207", {DATES[-1]: 5.275})
    pm.add("600207", buy_date=DATES[0], buy_price=5.0, qty=100)  # 买入价用原始价口径

    signals = pm.evaluate(as_of=AS_OF)
    assert len(signals) == 1
    assert signals[0].price_basis == "raw"
    assert signals[0].current_price == pytest.approx(5.275, abs=1e-6)
    assert signals[0].reason == "跌破MA10"