"""轻量回测引擎：验证各策略历史信号的质量（胜率/盈亏/持仓天数）。

定位与边界（务必阅读）：

- **信号级**回测：每个信号日的每只选股都视为一笔独立交易（等额资金），
  不做组合资金管理/仓位约束，衡量的是「信号质量」而非净值曲线。
- **入场**：信号日**次日开盘价**；次日涨停开盘视为买不进，放弃该笔。
- **离场优先级**：硬止损（盘中触发，按止损价成交；跳空低开则按开盘价）
  > 收盘跌破 MA20（次日开盘卖出）> 时间止损（持有 N 个交易日仍不盈利，
  次日开盘卖出）> 回测窗口结束（按最后收盘价强平）。
- **费用**：按 fee_rate（默认往返 0.1%）从收益中直接扣除，不建模滑点。
- 事件型策略（PrivatePlacementStrategy）依赖当日公告，不参与回测。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.filters import board_limit_pct, filter_symbols, limit_up_price

logger = get_logger(__name__)


@dataclass
class Trade:
    """一笔回测成交。"""

    strategy: str
    symbol: str
    signal_date: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    hold_bars: int
    ret_pct: float  # 已扣费
    reason: str

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "signal_date": self.signal_date,
            "entry_date": self.entry_date,
            "entry_price": round(self.entry_price, 4),
            "exit_date": self.exit_date,
            "exit_price": round(self.exit_price, 4),
            "hold_bars": self.hold_bars,
            "ret_pct": round(self.ret_pct, 3),
            "reason": self.reason,
        }


@dataclass
class StrategyReport:
    """单策略回测汇总。"""

    strategy: str
    trades: list[Trade] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.ret_pct > 0) / len(self.trades) * 100.0

    @property
    def avg_ret(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.ret_pct for t in self.trades) / len(self.trades)

    @property
    def med_ret(self) -> float:
        if not self.trades:
            return 0.0
        vals = sorted(t.ret_pct for t in self.trades)
        mid = len(vals) // 2
        if len(vals) % 2:
            return vals[mid]
        return (vals[mid - 1] + vals[mid]) / 2

    @property
    def best(self) -> float:
        return max((t.ret_pct for t in self.trades), default=0.0)

    @property
    def worst(self) -> float:
        return min((t.ret_pct for t in self.trades), default=0.0)

    @property
    def avg_hold(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.hold_bars for t in self.trades) / len(self.trades)

    @property
    def total_ret(self) -> float:
        """等额资金下各笔收益之和（% × 单笔资金 / 单笔资金），仅作参考。"""
        return sum(t.ret_pct for t in self.trades)

    @property
    def exit_reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.trades:
            out[t.reason] = out.get(t.reason, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "n": self.n,
            "win_rate": round(self.win_rate, 2),
            "avg_ret": round(self.avg_ret, 3),
            "med_ret": round(self.med_ret, 3),
            "best_ret": round(self.best, 3),
            "worst_ret": round(self.worst, 3),
            "avg_hold_bars": round(self.avg_hold, 1),
            "total_ret": round(self.total_ret, 3),
            "exit_reasons": self.exit_reasons,
            "trades": [t.to_dict() for t in self.trades],
        }


class _MemoryEngine:
    """内存行情缓存：向策略/过滤器提供与 DataEngine 相同的读取接口。

    一次性把全部K线载入内存（丢弃冗余 symbol 列省内存），回测期间
    策略/过滤器的 get_ohlcv 不再触发 SQLite 查询。
    """

    def __init__(self, engine: DataEngine, cutoff: str | None = None) -> None:
        self.db_path = engine.db_path  # RpsBreakout 直接读库，需要真实路径
        self._engine = engine
        self._cutoff = cutoff
        self._frames: dict[str, pd.DataFrame] = {}

    def load(self) -> int:
        symbols = self._engine.get_local_symbols()
        for symbol in symbols:
            df = self._engine.get_ohlcv(symbol)
            if df.empty:
                continue
            if self._cutoff:
                df = df[df["date"].astype(str) <= self._cutoff]
                if df.empty:
                    continue
            df = df.drop(columns=["symbol"], errors="ignore")
            # 预计算 MA20，供离场模拟使用
            df["ma20"] = df["close"].rolling(20).mean()
            self._frames[symbol] = df
        logger.info(f"回测行情缓存完成：{len(self._frames)} 只股票（cutoff={self._cutoff}）")
        return len(self._frames)

    @property
    def frames(self) -> dict[str, pd.DataFrame]:
        return self._frames

    def get_local_symbols(self) -> list[str]:
        return list(self._frames.keys())

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        return self._frames.get(symbol, pd.DataFrame())

    def get_stock_names(self, symbols: list[str]) -> dict[str, str]:
        try:
            return self._engine.get_stock_names(symbols)
        except Exception:  # noqa: BLE001
            return {}

class Backtester:
    """信号级回测器：在历史信号日上运行策略 → 过滤 → 模拟成交 → 汇总。"""

    def __init__(
        self,
        settings: Settings,
        engine: DataEngine,
        strategies: list[BaseStrategy],
        start: str | None = None,
        end: str | None = None,
        days: int = 60,
        step: int = 10,
        fee_rate: float = 0.001,
        stop_loss: float | None = None,
        time_stop_days: int | None = None,
    ) -> None:
        """
        Args:
            start/end: 回测窗口（ISO 日期）。start 为 None 时取 end 前
                `days` 个交易日作为信号窗口。
            step: 信号日抽样步长（每 N 个交易日取一天做信号日），控制耗时。
            fee_rate: 往返费用率（从单笔收益中扣除）。
            stop_loss: 硬止损比例，默认取 settings.position_stop_loss。
            time_stop_days: 时间止损天数，默认取 settings.position_time_stop_days。
        """
        self.settings = settings
        self.engine = engine
        self.strategies = [
            s for s in strategies if type(s).__name__ != "PrivatePlacementStrategy"
        ]
        self.start = start
        self.end = end
        self.days = days
        self.step = max(1, step)
        self.fee_rate = fee_rate
        self.stop_loss = stop_loss if stop_loss is not None else settings.position_stop_loss
        self.time_days = (
            time_stop_days if time_stop_days is not None else settings.position_time_stop_days
        )
        self.benchmark_pct: float = 0.0
        self.signal_dates: list[str] = []

    def run(self) -> dict[str, StrategyReport]:
        """执行回测，返回 {策略名: StrategyReport}。"""
        from datetime import date as _date

        end_ref = _date.fromisoformat(self.end) if self.end else _date.today()
        mem = _MemoryEngine(self.engine, cutoff=end_ref.isoformat())
        mem.load()
        if not mem.frames:
            raise RuntimeError("回测失败：行情缓存为空，请先执行 --backfill 回填数据")

        # 全市场交易日轴（升序）
        all_dates: set[str] = set()
        for df in mem.frames.values():
            all_dates.update(df["date"].astype(str).tolist())
        timeline = sorted(all_dates)
        if not timeline:
            raise RuntimeError("回测失败：无可用K线数据")

        end_iso = timeline[-1] if not self.end else min(self.end, timeline[-1])
        window = [d for d in timeline if d <= end_iso]
        if self.start:
            window = [d for d in window if d >= self.start]
        else:
            window = window[-self.days :]
        if not window:
            raise RuntimeError(
                f"回测失败：窗口为空（start={self.start} end={end_iso}）"
            )

        self.signal_dates = window[:: self.step]
        logger.info(
            f"回测开始：窗口 {window[0]} ~ {window[-1]}，"
            f"信号日 {len(self.signal_dates)} 个（step={self.step}），"
            f"策略 {[type(s).__name__ for s in self.strategies]}"
        )

        reports: dict[str, StrategyReport] = {
            type(s).__name__: StrategyReport(type(s).__name__) for s in self.strategies
        }

        for idx, d in enumerate(self.signal_dates, start=1):
            logger.info(f"[回测 {idx}/{len(self.signal_dates)}] 信号日 {d}")
            for strategy in self.strategies:
                name = type(strategy).__name__
                try:
                    raw = strategy.run(as_of=d)
                    picked = filter_symbols(raw, mem, d, self.settings).kept
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[回测 {d}] {name} 执行失败：{exc}")
                    continue
                if len(picked) != len(raw):
                    logger.info(
                        f"[回测 {d}] {name} 原始 {len(raw)} → 过滤后 {len(picked)}"
                    )
                for symbol in picked:
                    trade = self._simulate_one(mem.get_ohlcv(symbol), name, symbol, d)
                    if trade is not None:
                        reports[name].trades.append(trade)

        self.benchmark_pct = self._benchmark(mem, window[0], window[-1])
        for rep in reports.values():
            logger.info(
                f"[回测] {rep.strategy}: {rep.n} 笔，胜率 {rep.win_rate:.1f}%，"
                f"平均 {rep.avg_ret:+.2f}%，中位 {rep.med_ret:+.2f}%，"
                f"平均持有 {rep.avg_hold:.1f} 日"
            )
        logger.info(f"[回测] 全市场等权基准（同窗口）：{self.benchmark_pct:+.2f}%")
        return reports

    def _benchmark(self, mem: _MemoryEngine, start: str, end: str) -> float:
        """全市场等权买入持有基准：窗口首收盘 → 末收盘的平均涨幅（%）。"""
        rets: list[float] = []
        for df in mem.frames.values():
            dates = df["date"].astype(str)
            sub = df[(dates >= start) & (dates <= end)]
            if len(sub) < 2:
                continue
            first = float(sub["close"].iloc[0])
            last = float(sub["close"].iloc[-1])
            if first > 0:
                rets.append((last / first - 1.0) * 100.0)
        if not rets:
            return 0.0
        return sum(rets) / len(rets)


    def _simulate_one(
        self,
        df: pd.DataFrame,
        strategy: str,
        symbol: str,
        signal_date: str,
    ) -> Trade | None:
        """模拟一笔「信号日次日开盘进场 → 规则离场」的交易。

        Returns:
            Trade；无入场条件（无次日K线/涨停开盘）时返回 None。
        """
        if df is None or df.empty or len(df) < 2:
            return None

        dates = df["date"].astype(str).tolist()
        # 信号日之后的第一根K线（= 次一交易日）
        i = bisect.bisect_right(dates, signal_date)
        if i >= len(dates):
            return None

        entry_open = float(df["open"].iloc[i])
        if entry_open <= 0:
            return None

        # 涨停开盘买不进：开盘价触及前收涨停价 → 放弃
        prev_close = float(df["close"].iloc[i - 1]) if i > 0 else entry_open
        if prev_close > 0:
            limit = limit_up_price(prev_close, board_limit_pct(symbol))
            if entry_open >= limit - 0.005:
                return None

        entry_price = entry_open
        stop = entry_price * (1.0 - self.stop_loss)
        entry_date = dates[i]

        opens = df["open"]
        closes = df["close"]
        lows = df["low"]
        ma20s = df["ma20"]

        pending_reason: str | None = None  # 收盘触发 → 次日开盘执行

        for j in range(i, len(df)):
            # 1) 前一日收盘触发的离场：本日开盘卖出
            if pending_reason is not None:
                fill = float(opens.iloc[j])
                if fill <= 0:
                    fill = float(closes.iloc[j])
                return self._make_trade(
                    strategy, symbol, signal_date, entry_date, entry_price,
                    dates[j], fill, j - i, pending_reason,
                )

            open_j = float(opens.iloc[j])
            low_j = float(lows.iloc[j])
            close_j = float(closes.iloc[j])

            # 2) 硬止损：盘中触及止损价（跳空低开则按开盘价成交）
            if low_j <= stop and open_j > 0:
                fill = stop if open_j > stop else open_j
                return self._make_trade(
                    strategy, symbol, signal_date, entry_date, entry_price,
                    dates[j], fill, j - i, "硬止损",
                )

            # 3) 收盘跌破 MA20 → 次日开盘卖出
            ma_j = ma20s.iloc[j]
            held = j - i
            if pd.notna(ma_j) and close_j < float(ma_j):
                pending_reason = "跌破MA20"
            # 4) 时间止损：持有 N 个交易日仍不盈利 → 次日开盘卖出
            elif held >= self.time_days and close_j <= entry_price:
                pending_reason = "时间止损"

        # 窗口结束：按最后收盘价强平（保留最后的 pending 原因）
        j = len(df) - 1
        return self._make_trade(
            strategy, symbol, signal_date, entry_date, entry_price,
            dates[j], float(closes.iloc[j]), j - i,
            pending_reason or "窗口结束",
        )

    def _make_trade(
        self,
        strategy: str,
        symbol: str,
        signal_date: str,
        entry_date: str,
        entry_price: float,
        exit_date: str,
        exit_price: float,
        hold_bars: int,
        reason: str,
    ) -> Trade | None:
        if entry_price <= 0 or exit_price <= 0:
            return None
        net_ret = ((exit_price / entry_price) - 1.0) * 100.0 - self.fee_rate * 100.0
        return Trade(
            strategy=strategy,
            symbol=symbol,
            signal_date=signal_date,
            entry_date=entry_date,
            entry_price=entry_price,
            exit_date=exit_date,
            exit_price=exit_price,
            hold_bars=hold_bars,
            ret_pct=net_ret,
            reason=reason,
        )

