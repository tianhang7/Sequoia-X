"""Sequoia-X V2 主程序入口。

运行模式：
  python main.py                  # 日常模式：增量补数据 → 数据新鲜度断言 → 持仓卖出评估
                                  #           → 策略选股 + 过滤 → 归档 → Telegram 推送
  python main.py --backfill       # 回填模式：baostock 拉全市场历史K线（首次/补数据用，约12分钟）
  python main.py --backtest       # 回测模式：验证策略历史信号质量（不推送任何消息）
  python main.py --position-list  # 查看持仓与复盘统计
  python main.py --position-add 600519 --position-qty 100 --position-price 1500
  python main.py --position-close 600519 --close-price 1600
  python main.py --position-exits # 仅评估持仓卖出条件（不推送）
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import socket
socket.setdefaulttimeout(10.0)

from sequoia_x.core.config import Settings, get_settings
from sequoia_x.core.logger import configure_file_logging, get_logger
from sequoia_x.core.trading_calendar import TradingCalendar
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.telegram import TelegramNotifier
from sequoia_x.portfolio import PositionManager
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.filters import filter_symbols
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy


def _build_strategies(engine: DataEngine, settings: Settings) -> list[BaseStrategy]:
    """策略列表（新增策略在此追加即可）。"""
    return [
        MaVolumeStrategy(engine=engine, settings=settings),
        TurtleTradeStrategy(engine=engine, settings=settings),
        HighTightFlagStrategy(engine=engine, settings=settings),
        LimitUpShakeoutStrategy(engine=engine, settings=settings),
        UptrendLimitDownStrategy(engine=engine, settings=settings),
        RpsBreakoutStrategy(engine=engine, settings=settings),
        PrivatePlacementStrategy(engine=engine, settings=settings),
    ]


def _alert_stale(
    engine: DataEngine,
    tg: TelegramNotifier,
    as_of: str,
    latest_iso: str,
) -> None:
    """推送「数据不新鲜」告警；同一 (as_of, latest) 标记只推一次，防止刷屏。"""
    marker = f"{as_of}>{latest_iso}"
    if engine.get_meta("stale_alerted_marker") == marker:
        logger = get_logger(__name__)
        logger.warning(f"数据不新鲜告警已推送过（{marker}），本次跳过重复告警")
        return

    body_html = (
        f"<b>库内最新K线：</b> {as_of}\n"
        f"<b>最新交易日：</b> {latest_iso}\n"
        f"<b>影响：</b> 选股与卖出提醒基于过期数据，strict 模式已拒绝推送。\n"
        f"<b>处理：</b> 等 baostock 同步恢复后重跑 python main.py。"
    )
    tg.send_alert("Sequoia-X 数据不新鲜", body_html)
    engine.set_meta("stale_alerted_marker", marker)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 拉取全市场历史 K 线（约12分钟）",
    )

    # ── 回测模式 ──
    parser.add_argument("--backtest", action="store_true", help="回测模式：验证策略历史信号质量（不推送）")
    parser.add_argument("--bt-start", dest="bt_start", default=None, help="回测起始日 YYYY-MM-DD（默认 end 前 N 个交易日）")
    parser.add_argument("--bt-end", dest="bt_end", default=None, help="回测结束日 YYYY-MM-DD（默认最新交易日）")
    parser.add_argument("--bt-days", dest="bt_days", type=int, default=60, help="信号窗口长度，交易日数（默认 60）")
    parser.add_argument("--bt-step", dest="bt_step", type=int, default=10, help="信号日抽样步长：每 N 个交易日取 1 天（默认 10）")
    parser.add_argument("--bt-fee", dest="bt_fee", type=float, default=0.001, help="往返费用率（默认 0.001 即 0.1%%）")
    parser.add_argument(
        "--bt-strategies",
        dest="bt_strategies",
        default=None,
        help="仅回测指定策略：逗号分隔的策略标识，如 ma_volume,turtle（默认除定增外全部）",
    )

    # ── 持仓管理 ──
    parser.add_argument("--position-add", dest="position_add", metavar="SYMBOL", default=None,
                        help="登记买入（配合 --position-qty --position-price）")
    parser.add_argument("--position-qty", dest="position_qty", type=int, default=0, help="买入股数（--position-add 用）")
    parser.add_argument("--position-price", dest="position_price", type=float, default=0.0, help="买入价（--position-add 用）")
    parser.add_argument("--position-strategy", dest="position_strategy", default="", help="来源策略标识（--position-add 用）")
    parser.add_argument("--position-stop", dest="position_stop", type=float, default=None, help="止损价（缺省 买入价×(1-7%%)）")
    parser.add_argument("--position-date", dest="position_date", default=None, help="买入日期 YYYY-MM-DD（缺省今天）")
    parser.add_argument("--position-close", dest="position_close", metavar="SYMBOL", default=None,
                        help="登记平仓（配合 --close-price）")
    parser.add_argument("--close-price", dest="close_price", type=float, default=0.0, help="平仓价（--position-close 用）")
    parser.add_argument("--close-reason", dest="close_reason", default="手动平仓", help="平仓原因（--position-close 用）")
    parser.add_argument("--close-date", dest="close_date", default=None, help="平仓日期 YYYY-MM-DD（缺省今天）")
    parser.add_argument("--position-list", dest="position_list", action="store_true", help="查看持仓与复盘统计")
    parser.add_argument("--position-exits", dest="position_exits", action="store_true", help="仅评估持仓卖出条件（不推送）")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志（控制台 + 文件落盘）
        logger = get_logger(__name__)
        if settings.log_file:
            configure_file_logging(settings.log_file)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.backfill:
            # ── 回填模式：单线程保守拉历史 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        # ── 持仓管理子命令（不同步数据、不跑策略、不推送）──
        if args.position_list or args.position_exits or args.position_add or args.position_close:
            _run_position_cli(args, engine, settings)
            return

        # ── 回测模式（不同步、不推送）──
        if args.backtest:
            _run_backtest(args, engine, settings)
            return

        # ── 日常模式：单次 API 补今天 + 策略 + 推送 ──
        logger.info("开始拉取最新快照...")
        count = engine.sync_today_bulk()
        logger.info(f"快照同步完成，写入 {count} 只股票")

        # 3.1 刷新本地股票名称缓存（缓存新鲜或联网失败时自动跳过，不影响主流程）
        #     推送阶段只读该缓存，不再逐个请求 baostock
        engine.sync_stock_names()

        # 4. 数据新鲜度断言（最高优先级：防止按过期信号下单）
        as_of = engine.get_max_date()
        if as_of is None:
            logger.error("数据库无K线数据，请先执行 python main.py --backfill 完成首次回填")
            raise SystemExit(1)

        calendar = TradingCalendar(settings.db_path)
        latest = calendar.latest_trade_date()
        latest_iso = latest.isoformat()

        tg_notifier = TelegramNotifier(settings, engine=engine)
        if not tg_notifier.is_enabled:
            logger.warning(
                "未配置 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID，本次运行将跳过所有推送（仅记录日志）"
            )

        if as_of < latest_iso:
            as_of_date = date.fromisoformat(as_of)
            gap_days = len(
                [d for d in calendar.trade_dates(as_of_date, latest) if d > as_of_date]
            )
            msg = (
                f"数据不新鲜：库内最新K线 {as_of}，最新交易日 {latest_iso}"
                f"（落后 {gap_days} 个交易日）"
            )
            if settings.data_freshness == "strict":
                logger.error(msg + "｜strict 模式：拒绝基于过期数据选股与推送，程序退出")
                _alert_stale(engine, tg_notifier, as_of, latest_iso)
                raise SystemExit(1)
            if settings.data_freshness == "warn":
                logger.warning(msg + "｜warn 模式：继续运行，结果仅供参考")
            else:
                logger.info(msg + f"｜data_freshness={settings.data_freshness}：不拦截")
        else:
            logger.info(f"数据新鲜度检查通过：库内最新K线 {as_of} = 最新交易日 {latest_iso}")

        # 5. 持仓卖出评估（优先推送，避免错过止损）
        pm = PositionManager(engine, settings)
        exit_signals = pm.evaluate(as_of=as_of)
        if exit_signals:
            if tg_notifier.is_enabled:
                tg_notifier.send_alert(
                    "持仓卖出提醒",
                    "\n\n".join(s.render_html() for s in exit_signals),
                    emoji="🔔",
                )
            else:
                logger.warning(
                    f"触发 {len(exit_signals)} 条卖出提醒，但 Telegram 未配置，仅记录日志"
                )

        # 6. 策略选股 + 过滤 + 推送
        strategies = _build_strategies(engine, settings)
        selections: dict[str, dict] = {}

        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            selected: list[str] = strategy.run()
            raw_count = len(selected)
            dropped: dict[str, int] = {}

            if selected and settings.enable_symbol_filters:
                fres = filter_symbols(selected, engine, as_of, settings)
                selected = fres.kept
                dropped = {k: len(v) for k, v in fres.dropped.items() if v}
                logger.info(
                    f"{strategy_name} 过滤：{raw_count} → {len(selected)}"
                    f"（剔除 {fres.summary()}）"
                )
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票（数据截至 {as_of}）")

            selections[strategy_name] = {
                "raw_count": raw_count,
                "count": len(selected),
                "dropped": dropped,
                "symbols": selected,
            }

            if selected:
                # Telegram 推送（未配置凭证时 send 内部返回 False，见运行开头的告警）
                tg_notifier.send(
                    symbols=selected,
                    strategy_name=strategy_name,
                )
            else:
                logger.info(f"{strategy_name} 无选股结果，跳过推送")

        # 7. 归档当日选股结果（JSON，可复盘/喂给后续工具）
        _archive_selections(settings, as_of, latest_iso, selections)

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


def _run_position_cli(args: argparse.Namespace, engine: DataEngine, settings: Settings) -> None:
    """处理持仓相关子命令（--position-list / -exits / -add / -close）。"""
    logger = get_logger(__name__)
    pm = PositionManager(engine, settings)

    if args.position_list:
        opens = pm.list_open()
        if not opens:
            logger.info("当前无未平仓持仓")
        for pos in opens:
            df = engine.get_ohlcv(pos["symbol"])
            if df.empty:
                cur, last_date = 0.0, "-"
            else:
                cur = float(df["close"].iloc[-1])
                last_date = str(df["date"].iloc[-1])
            ret = (cur / float(pos["buy_price"]) - 1.0) * 100.0 if cur else 0.0
            logger.info(
                f"持仓 {pos['symbol']} [{pos['strategy'] or '-'}] "
                f"买 {pos['buy_date']} @{pos['buy_price']} ×{pos['qty']}股 "
                f"止损 {pos['stop_price']} | 最新 {last_date} 收盘 {cur} ({ret:+.1f}%)"
            )
        stats = pm.closed_stats()
        logger.info(
            f"已平仓 {int(stats['count'])} 笔，胜率 {stats['win_rate']:.1f}%，"
            f"平均盈亏 {stats['avg_ret']:+.1f}%"
        )
        return

    if args.position_exits:
        as_of = engine.get_max_date()
        signals = pm.evaluate(as_of=as_of)
        if not signals:
            logger.info(f"持仓评估（截至 {as_of}）：无卖出信号")
        for sig in signals:
            logger.warning(f"卖出信号 {sig.render_plain()}")
        return

    if args.position_add:
        if not args.position_qty or not args.position_price:
            raise ValueError("--position-add 需要同时指定 --position-qty 与 --position-price")
        pos_id = pm.add(
            symbol=args.position_add,
            buy_date=args.position_date or date.today().isoformat(),
            buy_price=args.position_price,
            qty=args.position_qty,
            strategy=args.position_strategy or "",
            stop_price=args.position_stop,
        )
        logger.info(f"买入登记完成：{args.position_add}（持仓 id={pos_id}）")
        return

    if args.position_close:
        if not args.close_price:
            raise ValueError("--position-close 需要指定 --close-price")
        ret = pm.close(
            symbol=args.position_close,
            close_price=args.close_price,
            reason=args.close_reason or "手动平仓",
            close_date=args.close_date,
        )
        logger.info(f"平仓登记完成：{args.position_close}，实现盈亏 {ret:+.1f}%")
        return


def _archive_selections(
    settings: Settings,
    as_of: str,
    latest_iso: str,
    selections: dict[str, dict],
) -> None:
    """把当日选股结果落盘为 selections_YYYYMMDD.json（失败不阻断主流程）。"""
    logger = get_logger(__name__)
    try:
        out_dir = Path(settings.selections_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"selections_{date.today():%Y%m%d}.json"
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "as_of": as_of,
            "latest_trade_date": latest_iso,
            "strategies": selections,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"选股结果已归档：{out}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"选股归档失败（不影响主流程）：{exc}")


def _run_backtest(args: argparse.Namespace, engine: DataEngine, settings: Settings) -> None:
    """回测模式：不联网同步数据、不推送任何消息，结果打印 + JSON 落盘。"""
    from sequoia_x.backtest import Backtester

    logger = get_logger(__name__)
    logger.info("进入回测模式（不推送）...")

    strategies = _build_strategies(engine, settings)
    if args.bt_strategies:
        wanted = {n.strip().lower() for n in args.bt_strategies.split(",") if n.strip()}
        strategies = [s for s in strategies if s.strategy_key.lower() in wanted]
        if not strategies:
            available = ",".join(s.strategy_key for s in _build_strategies(engine, settings))
            raise ValueError(
                f"--bt-strategies 未匹配到任何策略：{args.bt_strategies}（可用：{available}）"
            )

    backtester = Backtester(
        settings=settings,
        engine=engine,
        strategies=strategies,
        start=args.bt_start,
        end=args.bt_end,
        days=args.bt_days,
        step=args.bt_step,
        fee_rate=args.bt_fee,
    )
    reports = backtester.run()

    logger.info("回测汇总（信号级 · 等额资金 · 次日开盘进场）：")
    logger.info(
        f"{'策略':<26}{'笔数':>6}{'胜率%':>8}{'平均%':>8}{'中位%':>8}"
        f"{'最好%':>9}{'最差%':>9}{'持有(日)':>9}"
    )
    for rep in reports.values():
        logger.info(
            f"{rep.strategy:<26}{rep.n:>6}{rep.win_rate:>8.1f}{rep.avg_ret:>8.2f}"
            f"{rep.med_ret:>8.2f}{rep.best:>9.2f}{rep.worst:>9.2f}{rep.avg_hold:>9.1f}"
        )
        if rep.exit_reasons:
            logger.info(f"    离场分布：{rep.exit_reasons}")
    logger.info(f"全市场等权基准（同窗口）：{backtester.benchmark_pct:+.2f}%")

    # 报告落盘
    try:
        out_dir = Path(settings.selections_dir).parent / "backtest"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"backtest_{datetime.now():%Y%m%d_%H%M%S}.json"
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "window": {
                "signal_start": backtester.signal_dates[0] if backtester.signal_dates else None,
                "signal_end": backtester.signal_dates[-1] if backtester.signal_dates else None,
                "step": backtester.step,
                "fee_rate": backtester.fee_rate,
            },
            "benchmark_pct": round(backtester.benchmark_pct, 3),
            "reports": {name: rep.to_dict() for name, rep in reports.items()},
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"回测报告已保存：{out}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"回测报告落盘失败（不影响结果输出）：{exc}")


if __name__ == "__main__":
    main()
