"""回测引擎测试。"""

import random
from pathlib import Path

import pandas as pd
import pytest

from sequoia_x.backtest import Backtester, StrategyReport
from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy

from tests._seed import bar, flat_bars, insert_bars, weekdays_ending

AS_OF = "2026-09-25"
DATES = weekdays_ending(AS_OF, 130)


def _setup(tmp_path, **kw) -> tuple[DataEngine, Settings]:
    defaults = dict(
        db_path=str(Path(tmp_path) / "test.db"),
        start_date="2026-01-01",
    )
    defaults.update(kw)
    settings = Settings(**defaults)
    return DataEngine(settings), settings


def _random_walk_rows(dates: list[str], seed: int) -> list[tuple]:
    rng = random.Random(seed)
    price = 10.0
    rows = []
    for d in dates:
        o = price
        c = price * (1 + rng.uniform(-0.03, 0.035))
        h = max(o, c) * 1.01
        l = min(o, c) * 0.99
        rows.append(bar(d, round(o, 3), round(h, 3), round(l, 3), round(c, 3),
                        2_000_000, 200_000_000))
        price = c
    return rows


def _seed_market(engine: DataEngine) -> None:
    for i, symbol in enumerate(["600001", "600002", "300001"]):
        insert_bars(engine.db_path, symbol, _random_walk_rows(DATES, seed=i))


def test_backtest_integration(tmp_path):
    """端到端：3 只股票 × 多信号日，输出结构与交易不变量。"""
    engine, settings = _setup(tmp_path)
    _seed_market(engine)

    strategies = [
        MaVolumeStrategy(engine=engine, settings=settings),
        # TurtleTradeStrategy 已下线，改用在役的 RpsBreakoutStrategy
        RpsBreakoutStrategy(engine=engine, settings=settings),
        # 事件型策略必须被自动剔除（不参与回测）
        PrivatePlacementStrategy(engine=engine, settings=settings),
    ]
    bt = Backtester(settings=settings, engine=engine, strategies=strategies,
                    days=120, step=40)
    reports = bt.run()

    assert set(reports) == {"MaVolumeStrategy", "RpsBreakoutStrategy"}
    assert "PrivatePlacementStrategy" not in reports
    assert len(bt.signal_dates) >= 2
    assert isinstance(bt.benchmark_pct, float)

    for rep in reports.values():
        assert isinstance(rep, StrategyReport)
        for t in rep.trades:
            # 无前视：进场日必须晚于信号日
            assert t.entry_date > t.signal_date
            assert t.entry_price > 0 and t.exit_price > 0
            assert t.hold_bars >= 0
            assert t.reason in {"硬止损", "跌破MA20", "时间止损", "窗口结束"}
            assert t.ret_pct == pytest.approx(
                (t.exit_price / t.entry_price - 1) * 100 - 0.1, abs=1e-6
            )


def test_private_placement_skips_history(tmp_path):
    """事件型策略在历史模式下直接返回空（不触发任何网络请求）。"""
    _, settings = _setup(tmp_path)
    strategy = PrivatePlacementStrategy(engine=None, settings=settings)  # type: ignore[arg-type]
    assert strategy.run(as_of=AS_OF) == []


def _make_frame(rows: list[tuple]) -> pd.DataFrame:
    df = pd.DataFrame(
        rows, columns=["date", "open", "high", "low", "close", "volume", "turnover"]
    )
    df["ma20"] = df["close"].rolling(20).mean()
    return df


def test_simulate_hard_stop(tmp_path):
    """盘中触及止损价 → 按止损价成交，收益已扣费。"""
    engine, settings = _setup(tmp_path)
    dates = weekdays_ending(AS_OF, 30)
    rows = flat_bars(dates, 10.0)
    rows[11] = bar(dates[11], 10.0, 10.2, 9.9, 10.1)   # 进场日
    rows[12] = bar(dates[12], 9.5, 9.6, 9.2, 9.4)       # 次日盘中击穿 9.30
    df = _make_frame(rows)

    bt = Backtester(settings=settings, engine=engine, strategies=[],
                    stop_loss=0.07, time_stop_days=10, fee_rate=0.001)
    trade = bt._simulate_one(df, "T", "600001", dates[10])

    assert trade is not None
    assert trade.entry_date == dates[11]
    assert trade.entry_price == 10.0
    assert trade.exit_date == dates[12]
    assert trade.exit_price == pytest.approx(9.30)
    assert trade.reason == "硬止损"
    assert trade.hold_bars == 1
    assert trade.ret_pct == pytest.approx((9.30 / 10.0 - 1) * 100 - 0.1, abs=1e-6)


def test_simulate_ma20_break(tmp_path):
    """收盘跌破 MA20 → 次日开盘卖出。"""
    engine, settings = _setup(tmp_path)
    dates = weekdays_ending(AS_OF, 40)
    closes = (
        [10.0] * 7            # idx 0..6（idx6 = 进场日）
        + [10.5] * 12         # idx 7..18
        + [11.0] * 16         # idx 19..34
        + [10.2]              # idx 35 收盘跌破 MA20
        + [10.3] * 4          # idx 36..39
    )
    rows = [bar(d, c, c, c, c) for d, c in zip(dates, closes)]
    rows[35] = bar(dates[35], 10.4, 10.4, 10.2, 10.2)
    df = _make_frame(rows)

    bt = Backtester(settings=settings, engine=engine, strategies=[],
                    stop_loss=0.07, time_stop_days=10, fee_rate=0.001)
    trade = bt._simulate_one(df, "T", "600001", dates[5])

    assert trade is not None
    assert trade.reason == "跌破MA20"
    assert trade.entry_date == dates[6]
    assert trade.exit_date == dates[36]
    assert trade.exit_price == pytest.approx(10.3)
    assert trade.hold_bars == 30


def test_simulate_limit_up_open_skipped(tmp_path):
    """次日涨停开盘 → 买不进，返回 None。"""
    engine, settings = _setup(tmp_path)
    dates = weekdays_ending(AS_OF, 30)
    rows = flat_bars(dates, 10.0)
    rows[11] = bar(dates[11], 11.0, 11.0, 10.5, 10.8)  # 开盘即涨停 11.00
    df = _make_frame(rows)

    bt = Backtester(settings=settings, engine=engine, strategies=[],
                    stop_loss=0.07, time_stop_days=10, fee_rate=0.001)
    assert bt._simulate_one(df, "T", "600001", dates[10]) is None


def test_simulate_no_next_bar_returns_none(tmp_path):
    """信号日没有下一根K线 → 无法进场，返回 None。"""
    engine, settings = _setup(tmp_path)
    dates = weekdays_ending(AS_OF, 30)
    df = _make_frame(flat_bars(dates, 10.0))

    bt = Backtester(settings=settings, engine=engine, strategies=[],
                    stop_loss=0.07, time_stop_days=10, fee_rate=0.001)
    assert bt._simulate_one(df, "T", "600001", dates[-1]) is None

