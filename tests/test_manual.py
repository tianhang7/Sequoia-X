"""当日操作手册（sequoia_x.manual）测试。"""

from pathlib import Path

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.manual import build_push_summary, generate_manual
from sequoia_x.portfolio import ExitSignal

from tests._seed import flat_bars, insert_bars, set_raw_closes, weekdays_ending

AS_OF = "2026-09-24"
DATES = weekdays_ending(AS_OF, 30)


def _setup(tmp_path, **overrides) -> tuple[DataEngine, Settings]:
    """隔离配置：显式给出止损/止盈比例，避免受本地 .env 影响。"""
    params = dict(
        db_path=str(Path(tmp_path) / "test.db"),
        start_date="2024-01-01",
        selections_dir=str(Path(tmp_path) / "selections"),
        telegram_bot_token=None,
        telegram_chat_id=None,
        position_stop_loss=0.07,
        manual_take_profit=0.20,
    )
    params.update(overrides)
    return DataEngine(Settings(**params)), Settings(**params)


def _seed_quotes(engine: DataEngine) -> None:
    """两只候选：信号日收盘 20.00 / 10.00。"""
    insert_bars(engine.db_path, "600001", flat_bars(DATES, 20.0))
    insert_bars(engine.db_path, "600002", flat_bars(DATES, 10.0))


def _exit(symbol: str, reason: str) -> ExitSignal:
    return ExitSignal(
        symbol=symbol, strategy="turtle", qty=100, buy_date="2026-09-01",
        buy_price=10.0, stop_price=9.30, current_price=9.50,
        ret_pct=-5.0, reason=reason, detail="测试明细",
    )


def _generate(engine: DataEngine, settings: Settings, **kwargs) -> str:
    """生成手册并断言落盘位置，返回文件文本。"""
    path = generate_manual(settings, engine, as_of=AS_OF, latest_iso=AS_OF, **kwargs)
    assert path.name == "manual_20260924.md"
    assert path.parent == Path(settings.selections_dir).parent / "manuals"
    return path.read_text(encoding="utf-8")


def test_buy_rows_carry_three_target_prices(tmp_path):
    """买入行给出限价 / 止损 / 止盈三档价格，且跨策略候选去重。"""
    engine, settings = _setup(tmp_path)
    _seed_quotes(engine)
    text = _generate(engine, settings, selections={
        "TurtleTradeStrategy": {"symbols": ["600001", "600002"]},
        "MaVolumeStrategy": {"symbols": ["600001"]},
    })
    assert "建议买入（2 只）" in text              # 600001 被两个策略命中，只列一次
    assert "TurtleTrade、MaVolume" in text
    assert "| 600001 | - | TurtleTrade、MaVolume | 20.00 | 20.00 | 18.60 | 24.00 | 后复权 |" in text
    assert "| 600002 | - | TurtleTrade | 10.00 | 10.00 | 9.30 | 12.00 | 后复权 |" in text


def test_take_profit_ratio_is_configurable(tmp_path):
    """止盈目标随 MANUAL_TAKE_PROFIT 变化，止损沿用持仓规则。"""
    engine, settings = _setup(tmp_path, manual_take_profit=0.10)
    _seed_quotes(engine)
    text = _generate(
        engine, settings, selections={"TurtleTradeStrategy": {"symbols": ["600001"]}}
    )
    assert "| 600001 | - | TurtleTrade | 20.00 | 20.00 | 18.60 | 22.00 | 后复权 |" in text


def test_missing_kline_degrades_to_placeholder(tmp_path):
    """本地无行情时价格列降级为 '-'，不抛异常。"""
    engine, settings = _setup(tmp_path)
    text = _generate(
        engine, settings, selections={"TurtleTradeStrategy": {"symbols": ["600003"]}}
    )
    assert "| 600003 | - | TurtleTrade | - | - | - | - | - |" in text


def test_sell_rows_distinguish_stop_from_current(tmp_path):
    """硬止损以止损价为目标价，破位/时间止损以现价为目标价。"""
    engine, settings = _setup(tmp_path)
    text = _generate(engine, settings, exit_signals=[
        _exit("000001", "硬止损"),
        _exit("000002", "跌破MA20"),
    ])
    assert "建议卖出（2 笔）" in text
    assert "| 000001 | - | turtle | 9.50 | 9.30 | 9.30 | -5.0% | 硬止损 | 后复权 | 测试明细 |" in text
    assert "| 000002 | - | turtle | 9.50 | 9.50 | 9.30 | -5.0% | 跌破MA20 | 后复权 | 测试明细 |" in text


def test_stock_names_are_used_when_cached(tmp_path):
    """名称缓存命中时展示名称，未命中降级为 '-'。"""
    engine, settings = _setup(tmp_path)
    _seed_quotes(engine)
    engine.save_stock_names({"600001": "测试股份"})
    text = _generate(
        engine, settings, selections={"TurtleTradeStrategy": {"symbols": ["600001"]}}
    )
    assert "| 600001 | 测试股份 | TurtleTrade |" in text


def test_empty_day_still_writes_a_manual(tmp_path):
    """无信号无候选也要留下当日手册，便于确认“今天确实跑过且无需操作”。"""
    engine, settings = _setup(tmp_path)
    text = _generate(engine, settings)
    assert "今日无持仓触发离场信号" in text
    assert "今日无通过过滤器的策略候选" in text
    assert "今日无需操作" in text


def test_stale_data_is_flagged(tmp_path):
    """数据滞后于最新交易日时在手册顶部显著标注。"""
    engine, settings = _setup(tmp_path)
    path = generate_manual(settings, engine, as_of=AS_OF, latest_iso="2026-09-25")
    assert "数据滞后，本清单不可执行" in path.read_text(encoding="utf-8")


def test_raw_price_basis_is_used_and_labeled(tmp_path):
    """补齐 raw_close 后手册用原始价并打标“原始价”，三档价格可直接下单。"""
    engine, settings = _setup(tmp_path)
    _seed_quotes(engine)
    set_raw_closes(engine.db_path, "600001", {DATES[-1]: 11.00})
    text = _generate(engine, settings, selections={
        "TurtleTradeStrategy": {"symbols": ["600001", "600002"]},
    })
    assert "| 600001 | - | TurtleTrade | 11.00 | 11.00 | 10.23 | 13.20 | 原始价 |" in text
    assert "| 600002 | - | TurtleTrade | 10.00 | 10.00 | 9.30 | 12.00 | 后复权 |" in text
    assert "原始价覆盖 1/2" in text


def test_push_summary_counts_and_top_n(tmp_path):
    """摘要含买卖数量与前 N 条，超量截断并提示总数；N=0 返回空串。"""
    engine, settings = _setup(tmp_path)
    _seed_quotes(engine)
    signals = [_exit("000001", "硬止损"), _exit("000002", "跌破MA20")]
    summary = build_push_summary(
        AS_OF, signals,
        {"600001": ["TurtleTrade"], "600002": ["MaVolume"]},
        {"000001": "测试一", "600001": "测试股份"},
        top_n=1,
    )
    assert "卖出 2 笔｜买入 2 只" in summary
    assert "000001" in summary and "000002" not in summary  # 卖出只取前 1
    assert "600002" not in summary  # 买入只取前 1
    assert "等共 2 笔" in summary and "等共 2 只" in summary
    assert build_push_summary(AS_OF, signals, {"600001": ["TurtleTrade"]}, top_n=0) == ""


def test_push_summary_empty_day(tmp_path):
    """空信号日摘要为短消息而非空串（调用方仍可推送一条确认消息）。"""
    summary = build_push_summary(AS_OF, [], {}, top_n=10)
    assert "今日无需操作" in summary
