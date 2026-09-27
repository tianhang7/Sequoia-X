from contextlib import closing

import pandas as pd
import sqlite3
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class RpsBreakoutStrategy(BaseStrategy):
    """RPS 极强动量突破策略"""

    strategy_key: str = "rps"
    rps_period: int = 120
    rps_threshold: int = 90

    def run(self, as_of: str | None = None) -> list[str]:
        """RPS 极强动量突破选股。

        Args:
            as_of: 信号日；非 None 时只使用该日期及之前的K线
                （先在 SQL 文本日期上切片，避免读取后才过滤造成前视）。
        """
        try:
            with closing(sqlite3.connect(self.engine.db_path)) as conn:
                df = pd.read_sql("SELECT symbol, date, close, high FROM stock_daily", conn)
        except Exception as exc:
            logger.error(f"读取数据库失败: {exc}")
            return []

        if df.empty:
            return []

        if as_of is not None:
            # date 为 ISO 文本，字符串比较即时间比较
            df = df[df["date"] <= as_of]
            if df.empty:
                return []

        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values(['symbol', 'date'])

        # 纵向计算涨幅
        df['close_shift'] = df.groupby('symbol')['close'].shift(self.rps_period)
        df['pct_change'] = (df['close'] - df['close_shift']) / df['close_shift']

        latest_date = df['date'].max()
        latest_df = df[df['date'] == latest_date].copy()
        latest_df = latest_df.dropna(subset=['pct_change'])

        # 横向排位 (RPS)
        latest_df['rps'] = latest_df['pct_change'].rank(pct=True) * 100
        strong_stocks = latest_df[latest_df['rps'] >= self.rps_threshold].copy()

        # 计算滚动最高价（修复前视偏差：先 shift(1) 排除当日 high，
        # 否则「接近前 120 日高点」条件会把当日新高也算进去）
        df['high_prev'] = df.groupby('symbol')['high'].shift(1)
        roll_high = df.groupby('symbol')['high_prev'].rolling(
            window=self.rps_period, min_periods=self.rps_period // 2
        ).max().reset_index(level=0, drop=True)
        df['roll_high'] = roll_high

        latest_roll_high = df[df['date'] == latest_date][['symbol', 'roll_high']]
        strong_stocks = strong_stocks.merge(latest_roll_high, on='symbol')

        # 突破判定：收盘价位于「前 120 日最高价」10% 以内
        breakout_condition = strong_stocks['close'] >= strong_stocks['roll_high'] * 0.90
        selected = strong_stocks[breakout_condition]

        logger.info(f"RpsBreakoutStrategy 选出 {len(selected)} 只股票")
        return selected['symbol'].tolist()