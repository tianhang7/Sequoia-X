"""搜狐+东财数据源测试：code 映射、hq 解析、补拉优先搜狐、增量双源。"""


def test_sohu_code_mapping():
    from sequoia_x.data.eastmoney import _sohu_code
    assert _sohu_code("600519") == "cn_600519"
    assert _sohu_code("000001") == "cn_000001"
    assert _sohu_code("300750") == "cn_300750"
    assert _sohu_code("430001") == "bj_430001"  # 北交所走 bj 前缀


def test_sohu_parses_hq_and_converts_units():
    from sequoia_x.data.eastmoney import fetch_sohu
    hq = [["2026-01-09", "11.53", "11.46", "-0.05", "-0.43%", "11.44",
           "11.53", "983390", "112807.66", "0.51%"],
          ["bad"],
          ["2026-01-08", "11.64", "11.51", "-0.13", "-1.12%", "11.49",
           "11.65", "1100085", "126864.73", "0.57%"]]
    rows = fetch_sohu("000001", "2026-01-01", "2026-01-10",
                      requester=lambda u, p, t: ([{"hq": hq}]
                          if p["code"] == "cn_000001" else [{}]))
    assert [r[1] for r in rows] == ["2026-01-09", "2026-01-08"]
    # [s,date,open,high,low,close,vol(股),turnover(元)]
    assert rows[0][2:] == [11.53, 11.53, 11.44, 11.46, 98339000.0, 1128076600.0]


def test_fetch_kline_parses_and_reports_fqt():
    from sequoia_x.data import eastmoney as em
    seen = []
    def requester(url, params, timeout):
        seen.append(params["fqt"])
        return {"data": {"code": 1, "klines": [
            "2026-09-24,10.90,11.00,11.10,10.80,1000000,11000000.0,1.0",
            "bad-line",
            "2026-09-25,11.00,11.20,11.30,10.90,2000000,22000000.0,1.0"]}}
    rows = em.fetch_kline("600001", "20260924", "20260925", fqt=em.FQT_HFQ,
                          requester=requester)
    assert seen == [2]  # 后复权口径确实用 fqt=2 发出
    assert [r[1] for r in rows] == ["2026-09-24", "2026-09-25"]
    assert rows[0][2:] == [10.90, 11.10, 10.80, 11.00, 1000000.0, 11000000.0]


def test_sync_today_bulk_sohu_raw_plus_em_hfq(tmp_path):
    """增量同步双源：close=东财后复权、raw_close=搜狐不复权，同源对齐。"""
    from pathlib import Path
    from sequoia_x.core.config import Settings
    from sequoia_x.data import eastmoney as em
    from sequoia_x.data.engine import DataEngine
    from tests._seed import bar, insert_bars

    settings = Settings(db_path=str(Path(tmp_path) / "t.db"), start_date="2026-09-24")
    engine = DataEngine(settings)
    insert_bars(engine.db_path, "600001", [bar("2026-09-23", 10, 10, 10, 10)])

    class _FakeEM:
        FQT_HFQ = em.FQT_HFQ

        @staticmethod
        def fetch_windows(windows, fqt=em.FQT_HFQ, max_workers=8):
            rows = []
            for symbol, start, end in windows:
                rows.append([symbol, "2026-09-24", 1100.0, 1100.0, 1100.0, 1100.0, 1e6, 1.1e7])
                rows.append([symbol, "2026-09-25", 1100.0, 1100.0, 1100.0, 1100.0, 1e6, 1.1e7])
            return rows, []

        @staticmethod
        def fetch_sohu_many(symbols, start, end, max_workers=8):
            rows = []
            for s in symbols:
                rows.append([s, "2026-09-24", 11.0, 11.0, 11.0, 11.0, 1e6, 1.1e7])
                rows.append([s, "2026-09-25", 11.0, 11.0, 11.0, 11.0, 1e6, 1.1e7])
            return rows, []

    count = engine.sync_today_bulk(eastmoney=_FakeEM)
    assert count == 2
    df = engine.get_ohlcv("600001")
    assert float(df["close"].iloc[-1]) == 1100.0
    assert float(df["raw_close"].iloc[-1]) == 11.0


def test_backfill_raw_prefers_sohu(tmp_path):
    """补拉优先搜狐；搜狐失败子集才降级（替身全成功则无 baostock 调用）。"""
    from pathlib import Path
    from sequoia_x.core.config import Settings
    from sequoia_x.data.engine import DataEngine
    from tests._seed import flat_bars, insert_bars, weekdays_ending

    dates = weekdays_ending("2026-09-25", 3)
    settings = Settings(db_path=str(Path(tmp_path) / "t.db"), start_date="2026-09-24")
    engine = DataEngine(settings)
    insert_bars(engine.db_path, "600001", flat_bars(dates, 1100.0))

    class _FakeEM:
        calls = []

        @classmethod
        def fetch_sohu_many(cls, symbols, start, end, max_workers=8):
            cls.calls.append(list(symbols))
            rows = [[s, d, 11.0, 11.0, 11.0, 11.0, 1e6, 1.1e7]
                    for s in symbols for d in dates]
            return rows, []

    result = engine.backfill_raw(["600001"], eastmoney=_FakeEM)
    assert result["updated"] == 3
    assert result["failed"] == []
    assert _FakeEM.calls == [["600001"]]
    assert engine.get_raw_close("600001") == (dates[-1], 11.0)
