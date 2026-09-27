"""海龟交易策略：20日新高突破 + 成交额过亿 + 动量阳线过滤。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class TurtleTradeStrategy(BaseStrategy):
    """海龟交易策略（A股防诱多改良版）。

    选股条件（向量化，严禁 iterrows）：
    1. 突破新高：今日 close > 前20个交易日 high 的最大值
    2. 流动性：今日 turnover > 100,000,000
    3. 防诱多过滤：今日必须是实体阳线（今日 close > 今日 open），且必须真涨（今日 close > 昨日 close）
    """

    strategy_key: str = "turtle"
    _MIN_BARS: int = 21  # 至少需要 21 根 K 线（20日窗口 + 当日）

    def _get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        """通过 baostock 查询候选股票的流通市值（不复权收盘价 × 流通股本）。

        流通股本 = 成交量 / (换手率% / 100)
        流通市值 = 流通股本 × 不复权收盘价
        """
        import contextlib
        import io
        from datetime import date

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        market_caps: dict[str, float] = {}

        # baostock 会直接 print 报错文案，重定向以保持日志干净
        with contextlib.redirect_stdout(io.StringIO()):
            lg = bs.login()
        if lg.error_code != "0":
            # 登录失败（服务宕机）立即放弃：继续逐只 query 会每只都
            # 阻塞到 socket 超时（104 只 × 10s ≈ 17 分钟）
            return market_caps
        try:
            for symbol in symbols:
                bs_code = self.engine._to_baostock_code(symbol)
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "close,volume,turn",
                    start_date=today_str,
                    end_date=today_str,
                    frequency="d",
                    adjustflag="3",  # 不复权，真实价格
                )
                if rs.error_code != "0":
                    # 服务异常时立即终止，避免剩余标的逐只等到超时
                    break
                while rs.next():
                    row = rs.get_row_data()
                    try:
                        close = float(row[0])
                        volume = float(row[1])
                        turn = float(row[2])
                        if turn > 0:
                            circulating_shares = volume / (turn / 100)
                            market_caps[symbol] = circulating_shares * close
                    except (ValueError, ZeroDivisionError):
                        continue
        finally:
            bs.logout()

        return market_caps

    def run(self, as_of: str | None = None) -> list[str]:
        """
        遍历全市场，返回满足海龟突破条件的股票代码列表。

        Args:
            as_of: 信号日；非 None 时只使用该日期及之前的K线，
                且不联网查询市值（历史流通市值不可得），改用本地成交额降级排序。

        Returns:
            满足条件的股票代码列表。
        """
        symbols = self.engine.get_local_symbols()
        candidates: list[str] = []
        avg_amounts: dict[str, float] = {}  # 降级排序用：20日均成交额

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if as_of is not None:
                    df = df[df["date"] <= as_of]
                if len(df) < self._MIN_BARS:
                    continue

                # 向量化：前20日 high 的滚动最大值（不含当日，shift(1) 后取 rolling(20)）
                df["high_20"] = df["high"].shift(1).rolling(20).max()

                last = df.iloc[-1]
                prev = df.iloc[-2]  # 获取昨日数据，用于对比

                if pd.isna(last["high_20"]):
                    continue

                # 核心条件 1：突破前 20 天最高点
                breakout = last["close"] > last["high_20"]
                # 核心条件 2：流动性过亿
                liquid = last["turnover"] > 100_000_000

                # 【新增防守条件】拒绝郑州煤电式的高开低走大阴线！
                is_yang = last["close"] > last["open"]   # 实体必须是阳线（红柱）
                is_up = last["close"] > prev["close"]    # 必须是真涨，不能是假阳线

                if breakout and liquid and is_yang and is_up:
                    candidates.append(symbol)
                    avg_amounts[symbol] = float(df["turnover"].tail(20).mean() or 0.0)

            except Exception as exc:
                logger.warning(f"[{symbol}] TurtleTradeStrategy 计算失败：{exc}")
                continue

        # 按流通市值从大到小排序；市值不可得时降级为「20日均成交额」降序
        if candidates:
            market_caps: dict[str, float] = {}
            if as_of is None:
                # 仅实时模式联网取市值；回测传历史日期时不查（当日市值会失真）
                try:
                    market_caps = self._get_market_caps(candidates)
                except Exception as exc:
                    logger.warning(f"TurtleTradeStrategy 获取流通市值失败：{exc}")

            if market_caps:
                candidates.sort(key=lambda s: market_caps.get(s, 0), reverse=True)
            else:
                candidates.sort(key=lambda s: avg_amounts.get(s, 0), reverse=True)
                logger.warning(
                    "TurtleTradeStrategy 流通市值不可用，已降级按 20 日均成交额降序排序"
                )

        logger.info(f"TurtleTradeStrategy 选出 {len(candidates)} 只股票")
        return candidates