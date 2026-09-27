"""持仓管理：登记实盘持仓，每日评估止损/趋势离场/时间止损，产出卖出提醒。

系统本身不自动交易，本模块只负责两件事：

  1. **登记买入**（CLI：``python main.py --position-add 600519 --qty 100 --price 1500``），
     登记时写死止损价（缺省 = 买入价 × (1 - position_stop_loss)）。
  2. **每日评估**：收盘后按优先级「硬止损 > 跌破MA20 > 时间止损」检查全部持仓，
     产出 :class:`ExitSignal` 清单，由 main.py 推送卖出提醒。

离场由人工执行，之后用 ``--position-close`` 登记平仓结果，纳入复盘统计。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine, _connect

logger = get_logger(__name__)


@dataclass
class ExitSignal:
    """一条卖出提醒信号。

    价格口径说明：``price_basis`` 为 ``"raw"`` 时现价/止损/盈亏均为不复权原始价
    （可直接下单）；为 ``"hfq"`` 时是后复权价（仅表达相对关系，下单需换算）。
    """

    symbol: str
    strategy: str
    qty: int
    buy_date: str
    buy_price: float
    stop_price: float
    current_price: float
    ret_pct: float
    reason: str  # 硬止损 / 跌破MA20 / 时间止损
    detail: str
    price_basis: str = "hfq"  # "raw"（可下单）/ "hfq"（后复权降级表达）

    def render_plain(self) -> str:
        """控制台/日志用的单行文本。"""
        basis = "" if self.price_basis == "raw" else "（后复权）"
        return (
            f"{self.symbol} [{self.strategy or '-'}] {self.reason} | "
            f"买 {self.buy_date} @{self.buy_price:.3f}×{self.qty}股 | "
            f"现价 {self.current_price:.3f}{basis} ({self.ret_pct:+.1f}%) | {self.detail}"
        )

    def render_html(self) -> str:
        """Telegram HTML 用的多行文本。"""
        basis = "" if self.price_basis == "raw" else "（后复权）"
        return (
            f"🔴 <b>{self.symbol}</b> {self.reason}\n"
            f"策略：{self.strategy or '-'}　持股：{self.buy_date} 起 {self.qty} 股\n"
            f"买入 {self.buy_price:.3f} → 现价 {self.current_price:.3f}{basis}"
            f"（{self.ret_pct:+.1f}%），止损价 {self.stop_price:.3f}\n"
            f"{self.detail}"
        )

class PositionManager:
    """持仓表 position 的读写 + 每日离场评估。"""

    def __init__(self, engine: DataEngine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    # ── 登记 ──

    def add(
        self,
        symbol: str,
        buy_date: str,
        buy_price: float,
        qty: int,
        strategy: str = "",
        stop_price: float | None = None,
    ) -> int:
        """登记一笔买入，返回持仓 id。

        Raises:
            ValueError: 参数非法，或该股票已有未平仓持仓。
        """
        if buy_price <= 0:
            raise ValueError("买入价必须为正数")
        if qty <= 0:
            raise ValueError("数量必须为正整数")
        pd.Timestamp(buy_date)  # 非法日期抛异常

        stop = (
            stop_price
            if stop_price is not None
            else buy_price * (1.0 - self.settings.position_stop_loss)
        )
        if stop <= 0 or stop >= buy_price:
            raise ValueError(f"止损价 {stop} 必须在 (0, 买入价) 之间")

        with _connect(self.engine.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM position WHERE symbol = ? AND status = 'open'",
                (symbol,),
            ).fetchone()
            if row:
                raise ValueError(f"{symbol} 已有未平仓持仓（id={row[0]}），请先平仓")
            cur = conn.execute(
                "INSERT INTO position (symbol, strategy, buy_date, buy_price, qty, stop_price) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, strategy, buy_date, buy_price, qty, round(stop, 4)),
            )
            conn.commit()
            pos_id = int(cur.lastrowid or 0)
        logger.info(
            f"登记买入 {symbol} {buy_date} @{buy_price} ×{qty} 股，"
            f"止损价 {round(stop, 4)}（id={pos_id}）"
        )
        return pos_id

    def close(
        self,
        symbol: str,
        close_price: float,
        reason: str = "手动平仓",
        close_date: str | None = None,
    ) -> float:
        """平掉 symbol 的未平仓持仓，返回实现盈亏比例（%）。

        Raises:
            ValueError: 无未平仓持仓或价格非法。
        """
        if close_price <= 0:
            raise ValueError("平仓价必须为正数")
        close_date = close_date or pd.Timestamp.today().strftime("%Y-%m-%d")

        with _connect(self.engine.db_path) as conn:
            row = conn.execute(
                "SELECT id, buy_price FROM position WHERE symbol = ? AND status = 'open'",
                (symbol,),
            ).fetchone()
            if not row:
                raise ValueError(f"{symbol} 没有未平仓持仓")
            conn.execute(
                "UPDATE position SET status='closed', close_date=?, close_price=?, "
                "close_reason=? WHERE id=?",
                (close_date, close_price, reason, row[0]),
            )
            conn.commit()

        ret_pct = (close_price / float(row[1]) - 1.0) * 100.0
        logger.info(f"登记平仓 {symbol} @{close_price}（{reason}），盈亏 {ret_pct:+.1f}%")
        return ret_pct

    # ── 查询 ──

    def list_open(self) -> list[dict]:
        """返回全部未平仓持仓（dict 列表，按登记时间升序）。"""
        with _connect(self.engine.db_path) as conn:
            rows = conn.execute(
                "SELECT id, symbol, strategy, buy_date, buy_price, qty, stop_price "
                "FROM position WHERE status='open' ORDER BY id"
            ).fetchall()
        keys = ["id", "symbol", "strategy", "buy_date", "buy_price", "qty", "stop_price"]
        return [dict(zip(keys, r)) for r in rows]

    def closed_stats(self) -> dict[str, float]:
        """已平仓持仓的复盘统计：笔数、胜率、平均盈亏%。"""
        with _connect(self.engine.db_path) as conn:
            rows = conn.execute(
                "SELECT buy_price, close_price FROM position "
                "WHERE status='closed' AND close_price IS NOT NULL"
            ).fetchall()
        if not rows:
            return {"count": 0, "win_rate": 0.0, "avg_ret": 0.0}
        rets = [(c / b - 1.0) * 100.0 for b, c in rows if b]
        if not rets:
            return {"count": 0, "win_rate": 0.0, "avg_ret": 0.0}
        wins = sum(1 for r in rets if r > 0)
        return {
            "count": float(len(rets)),
            "win_rate": wins / len(rets) * 100.0,
            "avg_ret": sum(rets) / len(rets),
        }


    # ── 每日评估 ──

    def evaluate(self, as_of: str | None = None) -> list[ExitSignal]:
        """按优先级评估全部持仓：硬止损 > 跌破MA20 > 时间止损。

        Args:
            as_of: 评估日（ISO 日期）。最后一根K线未到 as_of 的股票（停牌/
                同步失败）直接跳过，避免用旧价格产生误报。

        Returns:
            触发离场条件的 ExitSignal 列表（可能为空）。
        """
        signals: list[ExitSignal] = []
        stop_loss = self.settings.position_stop_loss
        time_days = self.settings.position_time_stop_days

        for pos in self.list_open():
            symbol = pos["symbol"]
            try:
                df = self.engine.get_ohlcv(symbol)
                if as_of:
                    df = df[df["date"].astype(str) <= as_of]
                if df.empty:
                    continue
                if as_of and str(df["date"].iloc[-1]) != as_of:
                    # 数据滞后（停牌/同步失败）→ 跳过，不用旧价误报
                    continue

                buy_date = pos["buy_date"]
                buy_price = float(pos["buy_price"])
                stop = float(pos["stop_price"] or buy_price * (1.0 - stop_loss))

                since = df[df["date"].astype(str) >= buy_date]
                if since.empty:
                    # 买入日之后还没有K线（如当日刚登记）
                    continue

                last = df.iloc[-1]
                latest_date = str(last["date"])
                close_hfq = float(last["close"])

                # ── 价格口径切换（可执行价）──
                # 登记的 buy_price/stop 都是原始成交价；库内 OHLC 是后复权。
                # 同日 raw_close 存在 → 用 scale = raw/后复权 把后复权序列映射回
                # 原始价坐标系再比较；缺失 → 降级后复权并打标 price_basis="hfq"。
                basis = "hfq"
                raw = self.engine.get_raw_close(symbol)
                if (raw is not None and raw[0] == latest_date
                        and raw[1] and raw[1] > 0 and close_hfq > 0):
                    scale = float(raw[1]) / close_hfq
                else:
                    scale = 0.0
                if scale > 0:
                    basis = "raw"
                    current = float(raw[1])
                    low_min = float(since["low"].min()) * scale
                else:
                    current = close_hfq
                    low_min = float(since["low"].min())
                ret_pct = (current / buy_price - 1.0) * 100.0
                held_bars = len(since)

                reason: str | None = None
                detail = ""

                # 1. 硬止损：买入以来盘中最低价触及止损线
                if low_min <= stop:
                    reason = "硬止损"
                    detail = f"期间最低 {low_min:.2f} ≤ 止损价 {stop:.2f}，应已离场"

                # 2. 趋势离场：收盘跌破 20 日均线
                # （detail 文案避免使用裸 "<" ">"，Telegram HTML 解析会报错）
                if reason is None and len(df) >= 20:
                    ma20 = float(df["close"].rolling(20).mean().iloc[-1])
                    if basis == "raw":
                        ma20 *= scale
                    if current < ma20:
                        reason = "跌破MA20"
                        detail = f"收盘 {current:.2f} 低于 MA20 {ma20:.2f}，趋势转弱"

                # 3. 时间止损：持有 N 个交易日仍不盈利
                if reason is None and held_bars - 1 >= time_days and current <= buy_price:
                    reason = "时间止损"
                    detail = f"已持有 {held_bars - 1} 个交易日仍无盈利，资金效率过低"

                if reason:
                    signals.append(
                        ExitSignal(
                            symbol=symbol,
                            strategy=str(pos["strategy"] or ""),
                            qty=int(pos["qty"]),
                            buy_date=buy_date,
                            buy_price=buy_price,
                            stop_price=stop,
                            current_price=current,
                            ret_pct=ret_pct,
                            reason=reason,
                            detail=detail,
                            price_basis=basis,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - 单只失败不阻断整体评估
                logger.warning(f"[{symbol}] 持仓评估失败：{exc}")

        if signals:
            logger.warning(f"持仓评估触发 {len(signals)} 条卖出提醒")
        return signals

