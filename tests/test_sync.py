"""同步守卫测试：最新交易日已覆盖时跳过同步；登录失败快速返回。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from sequoia_x.data.engine import DataEngine, _bs_fetch_batch
from tests._seed import insert_bars


def _make_settings(tmp_path):
    from sequoia_x.core.config import Settings

    return Settings(
        db_path=str(tmp_path / "t.db"),
        start_date="2024-01-01",
    )


def _seed_calendar(db_path: str, dates: list[date]) -> None:
    """写入当天新鲜的日历缓存，避免 sync 内部触发联网刷新。"""
    payload = {
        "fetched_at": date.today().isoformat(),
        "dates": sorted(d.isoformat() for d in dates),
    }
    Path(db_path).parent.joinpath("trade_calendar.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _calendar_dates() -> list[date]:
    """覆盖 [today-30, today+7] 的全部工作日，保证任意运行日都确定。"""
    out: list[date] = []
    d = date.today() - timedelta(days=30)
    end = date.today() + timedelta(days=7)
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_sync_skips_when_local_covers_latest_trading_day(tmp_path, monkeypatch):
    """本地 MAX(date) == 最新交易日 → 跳过同步，绝不启动进程池。"""
    settings = _make_settings(tmp_path)
    engine = DataEngine(settings)
    dates = _calendar_dates()
    _seed_calendar(settings.db_path, dates)
    # 数据覆盖到最新交易日（取 ≤ latest 的最后 5 根）
    latest = max(d for d in dates if d <= date.today())
    covered = sorted(d for d in dates if d <= latest)[-5:]
    insert_bars(
        settings.db_path, "600000",
        [(d.isoformat(), 10, 10, 10, 10, 1000, 1000) for d in covered],
    )

    def _boom_pool(*a, **k):  # pragma: no cover - 守卫失败才会触发
        raise AssertionError("不应启动进程池")

    monkeypatch.setattr("multiprocessing.Pool", _boom_pool)
    assert engine.sync_today_bulk() == 0


def test_sync_proceeds_when_local_stale(tmp_path, monkeypatch):
    """本地 MAX(date) < 最新交易日 → 守卫放行，进入多进程同步路径。"""
    settings = _make_settings(tmp_path)
    engine = DataEngine(settings)
    dates = _calendar_dates()
    _seed_calendar(settings.db_path, dates)
    latest = max(d for d in dates if d <= date.today())
    # 数据故意停在最新交易日的 3 个工作日前
    stale_dates = sorted(d for d in dates if d < latest)[-3:]
    insert_bars(
        settings.db_path, "600000",
        [(d.isoformat(), 10, 10, 10, 10, 1000, 1000) for d in stale_dates],
    )

    class _ReachedPool:
        def __init__(self, *a, **k):
            raise RuntimeError("reached-pool")

    monkeypatch.setattr("multiprocessing.Pool", _ReachedPool)
    with pytest.raises(RuntimeError, match="reached-pool"):
        engine.sync_today_bulk()


def test_fetch_batch_returns_empty_when_login_fails(monkeypatch):
    """baostock 登录失败时应立即返回空，不进入逐只 query 循环。"""
    import sys
    import types

    class _Resp:
        error_code = "10002007"
        error_msg = "网络接收错误"

    queried = []

    def _query(*a, **k):  # pragma: no cover - 登录失败不应到达这里
        queried.append(a)
        return _Resp()

    fake_bs = types.SimpleNamespace(
        login=lambda: _Resp(),
        logout=lambda: None,
        query_history_k_data_plus=_query,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)

    assert _bs_fetch_batch([("600000", "sh.600000", "2026-09-24", "2026-09-24")]) == []
    assert queried == []


def test_turtle_market_caps_fail_fast_when_login_fails(monkeypatch, tmp_path):
    """Turtle 市值查询：登录失败立即返回空（否则 104 只 × 10s 超时卡死）。"""
    import sys
    import types

    from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy

    class _Resp:
        error_code = "10002007"

    queried = []

    def _query(*a, **k):  # pragma: no cover - 登录失败不应到达这里
        queried.append(a)
        return _Resp()

    fake_bs = types.SimpleNamespace(
        login=lambda: _Resp(),
        logout=lambda: None,
        query_history_k_data_plus=_query,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)

    strat = TurtleTradeStrategy(engine=DataEngine(_make_settings(tmp_path)), settings=_make_settings(tmp_path))
    assert strat._get_market_caps(["600000", "000001"]) == {}
    assert queried == []


def test_turtle_market_caps_breaks_on_query_error(monkeypatch, tmp_path):
    """Turtle 市值查询：首只 query 报错即终止，不逐只等到超时。"""
    import sys
    import types

    from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy

    class _Ok:
        error_code = "0"

    class _Bad:
        error_code = "10002001"
        error_msg = "服务器内部错误"

    queried = []

    def _query(*a, **k):
        queried.append(a)
        return _Bad()

    fake_bs = types.SimpleNamespace(
        login=lambda: _Ok(),
        logout=lambda: None,
        query_history_k_data_plus=_query,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)

    settings = _make_settings(tmp_path)
    strat = TurtleTradeStrategy(engine=DataEngine(settings), settings=settings)
    assert strat._get_market_caps(["600000", "000001", "000002"]) == {}
    assert len(queried) == 1  # 只尝试了第 1 只就终止