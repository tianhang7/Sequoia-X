"""股票名称本地缓存测试。

覆盖点：
- 名称缓存表的写入 / 读取 / 覆盖
- 推送阶段只读本地缓存，不发起任何网络请求
- 缓存刷新（sync_stock_names）的成功、TTL 跳过、失败保留旧缓存
- 通知器注入 engine 后使用缓存名称，名称缺失时降级为只展示代码
"""

import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from sequoia_x.core.config import Settings
from sequoia_x.data import engine as engine_module
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.telegram import TelegramNotifier


def make_settings_in(tmp_dir: str, **overrides: object) -> Settings:
    kwargs: dict = {
        "db_path": str(Path(tmp_dir) / "test.db"),
        "start_date": "2024-01-01",
        # 显式置空，避免受本地 .env 影响
        "telegram_bot_token": None,
        "telegram_chat_id": None,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def make_engine_in(tmp_dir: str) -> DataEngine:
    return DataEngine(make_settings_in(tmp_dir))


def test_save_and_read_cached_names() -> None:
    """名称缓存：写入后可读、可覆盖、未缓存代码不返回。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = make_engine_in(tmp_dir)
        assert engine.get_cached_stock_names() == {}

        assert engine.save_stock_names({"000001": "平安银行", "600519": "贵州茅台"}) == 2
        assert engine.get_cached_stock_names(["000001"]) == {"000001": "平安银行"}
        assert engine.get_cached_stock_names() == {
            "000001": "平安银行",
            "600519": "贵州茅台",
        }
        # 未缓存的代码不出现在结果里（调用方按「只显示代码」降级）
        assert engine.get_cached_stock_names(["300750"]) == {}

        # 同一代码重复写入应覆盖而不新增行
        assert engine.save_stock_names({"000001": "平安银行A"}) == 1
        with closing(sqlite3.connect(engine.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM stock_name").fetchone()[0]
        assert count == 2
        assert engine.get_cached_stock_names(["000001"]) == {"000001": "平安银行A"}

        # 空映射不写入
        assert engine.save_stock_names({}) == 0


def test_get_stock_names_reads_cache_without_network() -> None:
    """缓存命中时不应发起任何 baostock 请求（推送阶段零网络依赖）。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = make_engine_in(tmp_dir)
        engine.save_stock_names({"000001": "平安银行"})

        with patch.object(
            engine_module,
            "fetch_stock_names_from_baostock",
            side_effect=AssertionError("推送阶段不应联网"),
        ):
            names = engine.get_stock_names(["000001", "300750"])

        assert names == {"000001": "平安银行"}


def test_sync_stock_names_writes_cache_and_skips_when_fresh() -> None:
    """刷新成功写入缓存；缓存新鲜时不再联网。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = make_engine_in(tmp_dir)
        mapping = {"000001": "平安银行", "600519": "贵州茅台"}

        with patch.object(
            engine_module, "fetch_stock_names_from_baostock", return_value=mapping
        ) as mock_fetch:
            assert engine.sync_stock_names() == 2
            assert engine.get_cached_stock_names() == mapping
            # 第二次调用命中 TTL，跳过联网
            assert engine.sync_stock_names() == 0
            assert mock_fetch.call_count == 1



def test_sync_stock_names_refreshes_when_cache_stale() -> None:
    """缓存超过 TTL 后，即使不 force 也会重新联网刷新。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = make_engine_in(tmp_dir)
        engine.save_stock_names({"000001": "旧名称"})

        stale = datetime.now() - timedelta(days=engine.NAME_CACHE_TTL_DAYS + 1)
        with closing(sqlite3.connect(engine.db_path)) as conn:
            conn.execute(
                "UPDATE stock_name SET updated_at = ?",
                (stale.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            conn.commit()

        with patch.object(
            engine_module,
            "fetch_stock_names_from_baostock",
            return_value={"000001": "平安银行"},
        ) as mock_fetch:
            assert engine.sync_stock_names() == 1
            assert mock_fetch.call_count == 1

        assert engine.get_cached_stock_names(["000001"]) == {"000001": "平安银行"}


def test_sync_stock_names_keeps_old_cache_on_failure() -> None:
    """联网刷新失败时不抛异常，且保留已有缓存。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = make_engine_in(tmp_dir)
        engine.save_stock_names({"000001": "平安银行"})

        with patch.object(engine_module, "fetch_stock_names_from_baostock", return_value={}):
            assert engine.sync_stock_names(force=True) == 0

        assert engine.get_cached_stock_names(["000001"]) == {"000001": "平安银行"}


def test_rows_to_names_parses_stock_basic_rows() -> None:
    """字段顺序：code, code_name, ipoDate, outDate, type, status；只保留上市股票。"""
    rows = [
        ["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"],
        ["sz.000001", "平安银行", "1991-04-03", "", "1", "1"],
        ["sh.000001", "上证指数", "1991-07-15", "", "2", "1"],  # 指数，非股票
        ["sz.300001", "已退市", "2010-01-01", "2021-01-01", "1", "0"],  # 已退市
    ]
    assert engine_module._rows_to_names(rows) == {"600000": "浦发银行", "000001": "平安银行"}


def test_telegram_notifier_reads_engine_cache() -> None:
    """注入 engine 后，Telegram 消息中的名称来自本地缓存。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine = make_engine_in(tmp_dir)
        engine.save_stock_names({"000001": "平安银行"})

        notifier = TelegramNotifier(
            make_settings_in(
                tmp_dir,
                telegram_bot_token="123456:ABC",
                telegram_chat_id="-100123",
            ),
            engine=engine,
        )

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"ok": True, "result": {}}
            mock_post.return_value = mock_resp

            assert notifier.send(["000001"], "MaVolumeStrategy") is True

        text = mock_post.call_args.kwargs["json"]["text"]
        assert "平安银行(000001)" in text
