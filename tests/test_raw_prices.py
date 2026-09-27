"""数据引擎原始价（raw_close）测试：双轮同步、老库补拉、缺失降级。"""


def _fake_fetcher_factory(adj: str):
    """构造 baostock 抓取替身：返回固定两行，close 按口径区分。"""

    class _RS:
        error_code = "0"

        def __init__(self, rows):
            self._rows = rows
            self._i = 0

        def next(self):
            self._i += 1
            return self._i <= len(self._rows)

        def get_row_data(self):
            return self._rows[self._i - 1]

    def fetcher(code, fields, start_date=None, end_date=None, frequency=None, adjustflag=None):
        assert adjustflag == adj, f"口径错误：期望 {adj}，实际 {adjustflag}"
        price = "11.00" if adj == "3" else "1100.00"  # 不复权 11 元 vs 后复权 1100
        return _RS([
            ["2026-09-24", price, price, price, price, "1000000", "11000000"],
            ["2026-09-25", price, price, price, price, "1000000", "11000000"],
        ])

    return fetcher


def test_sync_today_bulk_writes_both_bases(tmp_path):
    """增量同步一次写两列：close=后复权、raw_close=不复权。

    sync_today_bulk 是增量口径（从本地 MAX(date)+1 拉到今天），故先 seed 一根
    旧 K 线；智能 fetcher 按 adjustflag 分支返回两种口径的价格。
    """
    from pathlib import Path

    from sequoia_x.core.config import Settings
    from sequoia_x.data.engine import DataEngine

    from tests._seed import bar, insert_bars

    settings = Settings(
        db_path=str(Path(tmp_path) / "test.db"),
        start_date="2026-09-24",
    )
    engine = DataEngine(settings)
    insert_bars(engine.db_path, "600001", [bar("2026-09-23", 10, 10, 10, 10)])

    def _smart(code, fields, start_date=None, end_date=None, frequency=None, adjustflag=None):
        return _fake_fetcher_factory("1" if adjustflag == "1" else "3")(
            code, fields, start_date=start_date, end_date=end_date,
            frequency=frequency, adjustflag=adjustflag,
        )

    count = engine.sync_today_bulk(fetcher=_smart)

    assert count == 2
    df = engine.get_ohlcv("600001")
    assert float(df["close"].iloc[-1]) == 1100.0
    assert float(df["raw_close"].iloc[-1]) == 11.0
    assert engine.get_raw_close("600001") == ("2026-09-25", 11.0)


def test_backfill_raw_updates_existing_rows_and_skips_done(tmp_path):
    """补拉只 UPDATE 已有行；已齐股票跳过；返回 updated/skipped/failed。"""
    from pathlib import Path

    from sequoia_x.core.config import Settings
    from sequoia_x.data.engine import DataEngine

    from tests._seed import flat_bars, insert_bars, weekdays_ending

    dates = weekdays_ending("2026-09-25", 5)
    settings = Settings(db_path=str(Path(tmp_path) / "test.db"), start_date="2026-09-24")
    engine = DataEngine(settings)
    insert_bars(engine.db_path, "600001", flat_bars(dates, 1100.0))
    insert_bars(engine.db_path, "600002", flat_bars(dates, 2200.0))

    result = engine.backfill_raw(["600001", "600002"], fetcher=_fake_fetcher_factory("3"))
    assert result["updated"] == 4  # 替身只返回 2 天 × 2 只（窗口内命中行）
    assert result["skipped"] == 0
    assert result["failed"] == []

    again = engine.backfill_raw(["600001"], fetcher=_fake_fetcher_factory("3"))
    assert again["skipped"] == 0  # 替身窗口外 3 行仍缺 raw → 继续尝试，不误判跳过
    assert again["updated"] == 2


def test_backfill_raw_survives_fetch_failure(tmp_path):
    """抓取抛异常时 backfill_raw 不吞错：异常向上传播由 CLI 记录，库保持可用。"""
    from pathlib import Path

    import pytest

    from sequoia_x.core.config import Settings
    from sequoia_x.data.engine import DataEngine

    from tests._seed import flat_bars, insert_bars, weekdays_ending

    dates = weekdays_ending("2026-09-25", 3)
    settings = Settings(db_path=str(Path(tmp_path) / "test.db"), start_date="2026-09-24")
    engine = DataEngine(settings)
    insert_bars(engine.db_path, "600001", flat_bars(dates, 1100.0))

    def _boom(*a, **k):
        raise RuntimeError("baostock down")

    with pytest.raises(RuntimeError):
        engine.backfill_raw(["600001"], fetcher=_boom)
    assert engine.get_raw_close("600001") is None


def test_get_raw_close_none_when_missing(tmp_path):
    """无 raw_close 时返回 None，调用方降级后复权。"""
    from pathlib import Path

    from sequoia_x.core.config import Settings
    from sequoia_x.data.engine import DataEngine

    from tests._seed import flat_bars, insert_bars, weekdays_ending

    dates = weekdays_ending("2026-09-25", 3)
    settings = Settings(db_path=str(Path(tmp_path) / "test.db"), start_date="2026-09-24")
    engine = DataEngine(settings)
    insert_bars(engine.db_path, "600001", flat_bars(dates, 1100.0))
    assert engine.get_raw_close("600001") is None