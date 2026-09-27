"""持仓管理（PositionManager）测试。"""

from pathlib import Path

import pytest

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.portfolio import PositionManager

from tests._seed import bar, flat_bars, insert_bars, weekdays_ending

AS_OF = "2026-09-25"
DATES = weekdays_ending(AS_OF, 40)


def _setup(tmp_path) -> tuple[DataEngine, Settings, PositionManager]:
    settings = Settings(
        db_path=str(Path(tmp_path) / "test.db"),
        start_date="2026-06-01",
    )
    engine = DataEngine(settings)
    return engine, settings, PositionManager(engine, settings)


def _seed_scenarios(engine: DataEngine) -> None:
    """构造四个持仓场景 + 一个数据滞后场景。"""
    # 600101：买入后盘中最低 9.20 触及止损 9.30 → 硬止损
    rows = flat_bars(DATES, 10.0)
    rows[10] = bar(DATES[10], 10.0, 10.0, 9.20, 9.60)
    insert_bars(engine.db_path, "600101", rows)

    # 600102：阴跌但未触止损，收盘跌破 MA20 → 跌破MA20
    closes = [10.0] + [9.45] * (len(DATES) - 2) + [9.40]
    rows = [
        bar(d, c, c, c, c) for d, c in zip(DATES, closes)
    ]
    insert_bars(engine.db_path, "600102", rows)

    # 600103：横盘微亏超过 10 个交易日 → 时间止损（收盘 9.96 高于 MA20 以避开浮点边界）
    closes = [10.0] + [9.95] * (len(DATES) - 2) + [9.96]
    rows = [bar(d, c, c, c, c) for d, c in zip(DATES, closes)]
    insert_bars(engine.db_path, "600103", rows)

    # 600104：稳步上涨 → 无卖出信号
    closes = [10.0 + i * 0.05 for i in range(len(DATES))]
    rows = [bar(d, c, c, c, c) for d, c in zip(DATES, closes)]
    insert_bars(engine.db_path, "600104", rows)

    # 600105：数据滞后（缺最后一日）但价格已击穿止损 → 应被跳过
    rows = flat_bars(DATES[:-1], 10.0)
    rows[10] = bar(DATES[10], 10.0, 10.0, 8.00, 8.50)
    insert_bars(engine.db_path, "600105", rows)


def test_add_generates_default_stop(tmp_path):
    _, _, pm = _setup(tmp_path)
    pos_id = pm.add("600101", buy_date=DATES[0], buy_price=10.0, qty=1000, strategy="turtle")
    assert pos_id > 0
    opens = pm.list_open()
    assert len(opens) == 1
    assert opens[0]["symbol"] == "600101"
    assert opens[0]["stop_price"] == pytest.approx(9.30, abs=1e-4)


def test_add_rejects_duplicate_and_bad_params(tmp_path):
    _, _, pm = _setup(tmp_path)
    pm.add("600101", buy_date=DATES[0], buy_price=10.0, qty=100)
    with pytest.raises(ValueError):
        pm.add("600101", buy_date=DATES[1], buy_price=10.0, qty=100)
    with pytest.raises(ValueError):
        pm.add("600102", buy_date=DATES[0], buy_price=-1, qty=100)
    with pytest.raises(ValueError):
        pm.add("600103", buy_date=DATES[0], buy_price=10.0, qty=0)
    with pytest.raises(ValueError):
        pm.add("600104", buy_date=DATES[0], buy_price=10.0, qty=100, stop_price=10.0)


def test_evaluate_priority_and_stale_skip(tmp_path):
    engine, _, pm = _setup(tmp_path)
    _seed_scenarios(engine)

    for sym, price in [
        ("600101", 10.0), ("600102", 10.0), ("600103", 10.0),
        ("600104", 10.0), ("600105", 10.0),
    ]:
        pm.add(sym, buy_date=DATES[0], buy_price=price, qty=100)

    signals = pm.evaluate(as_of=AS_OF)
    by_symbol = {s.symbol: s for s in signals}

    assert set(by_symbol) == {"600101", "600102", "600103"}  # 600104 无信号、600105 数据滞后跳过
    assert by_symbol["600101"].reason == "硬止损"
    assert by_symbol["600101"].stop_price == pytest.approx(9.30, abs=1e-4)
    assert by_symbol["600102"].reason == "跌破MA20"
    assert by_symbol["600103"].reason == "时间止损"
    # 渲染不抛异常且包含代码
    assert "600101" in by_symbol["600101"].render_plain()
    assert "600101" in by_symbol["600101"].render_html()


def test_evaluate_empty_when_no_positions(tmp_path):
    _, _, pm = _setup(tmp_path)
    assert pm.evaluate(as_of=AS_OF) == []


def test_close_and_stats(tmp_path):
    engine, _, pm = _setup(tmp_path)
    _seed_scenarios(engine)
    pm.add("600104", buy_date=DATES[0], buy_price=10.0, qty=100)
    ret = pm.close("600104", close_price=11.45, reason="止盈")
    assert ret == pytest.approx(14.5, abs=1e-6)
    assert pm.list_open() == []
    stats = pm.closed_stats()
    assert stats["count"] == 1
    assert stats["win_rate"] == 100.0
    assert stats["avg_ret"] == pytest.approx(14.5, abs=1e-6)
    with pytest.raises(ValueError):
        pm.close("600104", close_price=11.0)
