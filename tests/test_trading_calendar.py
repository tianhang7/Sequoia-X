"""交易日历模块测试。"""

from datetime import date

from sequoia_x.core.trading_calendar import TradingCalendar


def test_weekday_fallback_when_fetch_fails(tmp_path):
    """抓取失败且无缓存时，按「周一~周五为交易日」推断。"""
    def bad_fetcher():
        raise RuntimeError("network down")

    cal = TradingCalendar(str(tmp_path / "x.db"), fetcher=bad_fetcher)
    # 2026-09-26 是周六 → 最近交易日回退到周五 2026-09-25
    assert cal.latest_trade_date(date(2026, 9, 26)) == date(2026, 9, 25)
    # 2026-09-28 是周一 → 当日即交易日
    assert cal.latest_trade_date(date(2026, 9, 28)) == date(2026, 9, 28)
    assert cal.is_trading_day(date(2026, 9, 26)) is False
    assert cal.is_trading_day(date(2026, 9, 28)) is True


def test_latest_trade_date_from_fetcher(tmp_path):
    """有日历时，latest_trade_date 返回不晚于 ref 的最近交易日。"""
    fetch_dates = {date(2026, 9, 24), date(2026, 9, 25)}
    cal = TradingCalendar(str(tmp_path / "x.db"), fetcher=lambda: set(fetch_dates))

    assert cal.latest_trade_date(date(2026, 9, 26)) == date(2026, 9, 25)
    assert cal.latest_trade_date(date(2026, 9, 25)) == date(2026, 9, 25)
    assert cal.latest_trade_date(date(2026, 9, 24)) == date(2026, 9, 24)
    assert cal.is_trading_day(date(2026, 9, 25)) is True
    assert cal.is_trading_day(date(2026, 9, 26)) is False


def test_cache_roundtrip(tmp_path):
    """第一个实例联网成功写出缓存；第二个实例断网也能读缓存。"""
    db_path = str(tmp_path / "x.db")

    cal1 = TradingCalendar(db_path, fetcher=lambda: {date(2026, 9, 24), date(2026, 9, 25)})
    assert cal1.latest_trade_date(date(2026, 9, 25)) == date(2026, 9, 25)
    assert cal1.cache_path.exists()

    def bad_fetcher():
        raise RuntimeError("network down")

    cal2 = TradingCalendar(db_path, fetcher=bad_fetcher)
    assert cal2.latest_trade_date(date(2026, 9, 26)) == date(2026, 9, 25)


def test_trade_dates_range(tmp_path):
    """trade_dates 返回区间内全部交易日（升序）。"""
    fetch_dates = {date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 24), date(2026, 9, 25)}
    cal = TradingCalendar(str(tmp_path / "x.db"), fetcher=lambda: set(fetch_dates))
    got = cal.trade_dates(date(2026, 9, 22), date(2026, 9, 25))
    assert got == [date(2026, 9, 22), date(2026, 9, 24), date(2026, 9, 25)]
