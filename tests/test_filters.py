"""选股过滤器测试。"""

from pathlib import Path

import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.filters import (
    board_limit_pct,
    filter_symbols,
    is_limit_up,
    limit_up_price,
)

from tests._seed import bar, flat_bars, insert_bars, weekdays_ending

AS_OF = "2026-09-25"


def _make_settings(tmp_path, **kw) -> Settings:
    defaults = dict(
        db_path=str(Path(tmp_path) / "test.db"),
        start_date="2026-06-01",
        filter_min_bars=60,
        filter_min_avg_amount=100_000_000.0,
    )
    defaults.update(kw)
    return Settings(**defaults)


def _seed_universe(settings: Settings) -> DataEngine:
    """构造一只正常股 + 六只分别命中各过滤规则的股票。"""
    engine = DataEngine(settings)
    dates = weekdays_ending(AS_OF, 78)  # 78 个工作日，最后一天 = AS_OF

    # 600001：完全正常 → 应保留
    insert_bars(settings.db_path, "600001", flat_bars(dates, 10.0))

    # 430047：北交所 → non_main
    insert_bars(settings.db_path, "430047", flat_bars(dates, 10.0))

    # 600002：ST → st（需要名称缓存）
    insert_bars(settings.db_path, "600002", flat_bars(dates, 10.0))
    engine.save_stock_names({"600002": "ST测试"})

    # 600003：数据滞后（最后一根K线是前一日）→ stale
    insert_bars(settings.db_path, "600003", flat_bars(dates[:-1], 10.0))

    # 600004：次新股（只有 10 根K线）→ new_stock
    insert_bars(settings.db_path, "600004", flat_bars(dates[-10:], 10.0))

    # 600005：低流动性（20日均成交额 500 万）→ illiquid
    insert_bars(
        settings.db_path, "600005", flat_bars(dates, 10.0, t=5_000_000)
    )

    # 600006：信号日收盘涨停（10.00 → 11.00）→ limit_up
    rows = flat_bars(dates, 10.0)
    rows[-2] = bar(dates[-2], 10.0, 10.0, 10.0, 10.0)
    rows[-1] = bar(dates[-1], 10.0, 11.0, 10.0, 11.0)
    insert_bars(settings.db_path, "600006", rows)

    # 300001：创业板 20cm，10.00 → 11.00 不是涨停 → 应保留
    rows = flat_bars(dates, 10.0)
    rows[-1] = bar(dates[-1], 10.0, 11.0, 10.0, 11.0)
    insert_bars(settings.db_path, "300001", rows)

    return engine


def test_board_limit_pct():
    assert board_limit_pct("600519") == 0.10
    assert board_limit_pct("300750") == 0.20
    assert board_limit_pct("688981") == 0.20
    assert board_limit_pct("600519", "ST某股") == 0.05
    assert board_limit_pct("430047") == 0.30


def test_is_limit_up():
    assert is_limit_up("600001", 10.00, limit_up_price(10.00, 0.10)) is True
    assert is_limit_up("600001", 10.00, 11.01) is False
    # 创业板 20cm：11.00 不是涨停
    assert is_limit_up("300001", 10.00, 11.00) is False
    assert is_limit_up("300001", 10.00, 12.00) is True
    # ST 5cm
    assert is_limit_up("600002", 10.00, 10.50, "ST某股") is True


def test_filter_symbols_partitions_by_reason(tmp_path):
    settings = _make_settings(tmp_path)
    engine = _seed_universe(settings)

    universe = ["600001", "430047", "600002", "600003", "600004", "600005", "600006", "300001"]
    result = filter_symbols(universe, engine, AS_OF, settings)

    assert result.kept == ["600001", "300001"]
    assert result.dropped["non_main"] == ["430047"]
    assert result.dropped["st"] == ["600002"]
    assert result.dropped["stale"] == ["600003"]
    assert result.dropped["new_stock"] == ["600004"]
    assert result.dropped["illiquid"] == ["600005"]
    assert result.dropped["limit_up"] == ["600006"]
    assert result.total_dropped == 6
    assert "ST/退 1" in result.summary()


def test_filter_symbols_can_be_disabled(tmp_path):
    settings = _make_settings(tmp_path, enable_symbol_filters=False)
    engine = _seed_universe(settings)
    universe = ["600001", "430047", "600006"]
    result = filter_symbols(universe, engine, AS_OF, settings)
    assert result.kept == universe
    assert result.dropped == {}


def test_filter_limit_up_toggle(tmp_path):
    settings = _make_settings(tmp_path, filter_drop_limit_up=False)
    engine = _seed_universe(settings)
    result = filter_symbols(["600006"], engine, AS_OF, settings)
    assert result.kept == ["600006"]
    assert "limit_up" not in result.dropped


def test_empty_input_passthrough(tmp_path):
    settings = _make_settings(tmp_path)
    engine = DataEngine(settings)
    result = filter_symbols([], engine, AS_OF, settings)
    assert result.kept == []
    assert result.summary() == "无"
