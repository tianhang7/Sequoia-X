"""当日操作手册：把选股候选与持仓离场信号汇总为可执行的 Markdown 清单。

日常模式在归档选股结果之后自动生成，落盘于 ``selections_dir`` 同级的
``manuals`` 目录（默认 ``data/manuals/manual_YYYYMMDD.md``），包含：

  - 建议卖出：持仓离场信号（硬止损/跌破MA20/时间止损），列现价、目标卖出价、止损价；
  - 建议买入：过滤后的策略候选（跨策略去重），列收盘价、建议限价、止损价、止盈目标价。

价格口径提示：本地行情库为后复权价（与持仓提醒模块一致），表中的绝对价格用于表达
相对比例关系，实际下单时请以行情软件的实时价格为准。
手册仅供参考，不构成投资建议，所有买卖均由人工执行。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.portfolio import ExitSignal

logger = get_logger(__name__)


def _fmt(value: float | None) -> str:
    """价格展示格式；无行情时返回 '-'。"""
    return "-" if value is None else f"{value:.2f}"


def _short_strategy(name: str) -> str:
    """TurtleTradeStrategy → TurtleTrade，表格里更易读。"""
    return name.removesuffix("Strategy")


def _merge_candidates(selections: dict[str, dict]) -> dict[str, list[str]]:
    """合并各策略候选为 {代码: [命中策略, ...]}，按首次出现顺序跨策略去重。"""
    merged: dict[str, list[str]] = {}
    for strategy_name, payload in selections.items():
        for symbol in payload.get("symbols", []):
            strategies = merged.setdefault(symbol, [])
            short = _short_strategy(strategy_name)
            if short not in strategies:
                strategies.append(short)
    return merged


def _reference_close(engine: DataEngine, symbol: str) -> float | None:
    """取本地最新一根K线收盘价作为参考价（正常即信号日收盘）。"""
    df = engine.get_ohlcv(symbol)
    if df is None or df.empty:
        return None
    return float(df["close"].iloc[-1])


def _sell_target(signal: ExitSignal) -> float:
    """目标卖出价：硬止损以止损价为底线，破位/时间止损以现价尽快成交。"""
    return signal.stop_price if signal.reason == "硬止损" else signal.current_price


def _sell_table(signals: list[ExitSignal], names: dict[str, str]) -> list[str]:
    lines = [
        "| 代码 | 名称 | 来源策略 | 现价 | 目标卖出价 | 止损价 | 盈亏 | 离场信号 | 说明 |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for s in signals:
        lines.append(
            f"| {s.symbol} | {names.get(s.symbol, '-')} | {_short_strategy(s.strategy)} "
            f"| {_fmt(s.current_price)} | {_fmt(_sell_target(s))} | {_fmt(s.stop_price)} "
            f"| {s.ret_pct:+.1f}% | {s.reason} | {s.detail} |"
        )
    return lines


def _buy_table(
    candidates: dict[str, list[str]],
    engine: DataEngine,
    names: dict[str, str],
    stop_loss: float,
    take_profit: float,
) -> list[str]:
    lines = [
        "| 代码 | 名称 | 命中策略 | 收盘价(参考) | 建议限价 | 止损价 | 止盈目标 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for symbol, strategies in candidates.items():
        close = _reference_close(engine, symbol)
        stop = None if close is None else close * (1 - stop_loss)
        target = None if close is None else close * (1 + take_profit)
        lines.append(
            f"| {symbol} | {names.get(symbol, '-')} | {'、'.join(strategies)} "
            f"| {_fmt(close)} | {_fmt(close)} | {_fmt(stop)} | {_fmt(target)} |"
        )
    return lines


def _sell_checklist(signals: list[ExitSignal], names: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for idx, s in enumerate(signals, start=1):
        name = names.get(s.symbol, "")
        lines.append(
            f"{idx}. 卖出 **{s.symbol}** {name}：{s.reason}，目标价 {_fmt(_sell_target(s))}"
            f"（止损底线 {_fmt(s.stop_price)}，持仓 {s.qty} 股 @ {_fmt(s.buy_price)}，"
            f"{s.ret_pct:+.1f}%）"
        )
    return lines


def _buy_checklist(
    candidates: dict[str, list[str]],
    engine: DataEngine,
    names: dict[str, str],
    stop_loss: float,
    take_profit: float,
) -> list[str]:
    lines: list[str] = []
    for idx, (symbol, strategies) in enumerate(candidates.items(), start=1):
        close = _reference_close(engine, symbol)
        stop = None if close is None else close * (1 - stop_loss)
        target = None if close is None else close * (1 + take_profit)
        name = names.get(symbol, "")
        lines.append(
            f"{idx}. 买入 **{symbol}** {name}：限价 ≤ {_fmt(close)}；"
            f"止损 {_fmt(stop)}；止盈目标 {_fmt(target)}（{'、'.join(strategies)}）"
        )
    return lines


def generate_manual(
    settings: Settings,
    engine: DataEngine,
    *,
    as_of: str,
    latest_iso: str | None = None,
    exit_signals: list[ExitSignal] | None = None,
    selections: dict[str, dict] | None = None,
    out_dir: str | Path | None = None,
) -> Path:
    """生成当日操作手册，返回写出的 Markdown 文件路径。

    只读本地名称缓存与K线，不发起任何网络请求；同一天重复运行会覆盖旧文件。

    Args:
        settings: 全局配置（止损比例 ``position_stop_loss``、
            止盈比例 ``manual_take_profit``、归档目录 ``selections_dir``）。
        engine: 数据引擎。
        as_of: 信号基准日（ISO 字符串），即数据截至日。
        latest_iso: 交易日历推算的最新交易日；与 ``as_of`` 不一致时提示数据滞后。
        exit_signals: 持仓离场信号（``PositionManager.evaluate`` 的结果）。
        selections: 选股结果 ``{策略名: {"symbols": [...]}}``，与归档 JSON 同构。
        out_dir: 输出目录覆盖；默认为 ``selections_dir`` 同级的 ``manuals``。

    Returns:
        Path: 手册文件路径。
    """
    exit_signals = exit_signals or []
    candidates = _merge_candidates(selections or {})
    symbols = [s.symbol for s in exit_signals] + list(candidates)
    names = engine.get_stock_names(symbols) if symbols else {}
    stop_loss = settings.position_stop_loss
    take_profit = settings.manual_take_profit

    freshness = f"数据截至 {as_of}"
    if latest_iso and as_of != latest_iso:
        freshness += f"｜最新交易日 {latest_iso}（**数据滞后，本清单不可执行**）"

    lines: list[str] = [
        f"# Sequoia-X 当日操作手册 · {as_of}",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- {freshness}",
        "- 价格口径：本地库为后复权价（与持仓提醒一致），表中价格表达相对比例，"
        "下单请以行情软件实时价为准",
        f"- 买入规则：建议限价 = 信号日收盘价（次日高开越过限价不追高）；"
        f"止损 = 收盘 × (1 − {stop_loss:.1%})；止盈目标 = 收盘 × (1 + {take_profit:.1%})",
        "- 卖出规则：触发离场信号的持仓次日尽快人工卖出；"
        "硬止损以止损价为底线，跌破MA20/时间止损以现价尽快成交",
        "- 免责声明：本手册由程序按固定规则自动生成，仅供参考，不构成投资建议",
        "",
        f"## 一、建议卖出（{len(exit_signals)} 笔）",
        "",
    ]
    lines += _sell_table(exit_signals, names) if exit_signals else ["✅ 今日无持仓触发离场信号。"]
    lines += ["", f"## 二、建议买入（{len(candidates)} 只）", ""]
    lines += (
        _buy_table(candidates, engine, names, stop_loss, take_profit)
        if candidates
        else ["✅ 今日无通过过滤器的策略候选。"]
    )

    lines += ["", "## 三、执行清单（速览）", ""]
    sell_items = _sell_checklist(exit_signals, names)
    buy_items = _buy_checklist(candidates, engine, names, stop_loss, take_profit)
    if not sell_items and not buy_items:
        lines.append("- 今日无需操作。")
    else:
        if sell_items:
            lines += ["**卖出：**", ""] + sell_items + [""]
        if buy_items:
            lines += ["**买入：**", ""] + buy_items + [""]

    filename = f"manual_{as_of.replace('-', '')}.md"
    lines += ["---", f"*手册文件：{filename}｜持仓登记：``python main.py --position-add ...``*"]

    target_dir = Path(out_dir) if out_dir else Path(settings.selections_dir).parent / "manuals"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(
        f"当日操作手册已生成：{path}（卖出 {len(exit_signals)} 笔 / 买入 {len(candidates)} 只）"
    )
    return path
