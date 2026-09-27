"""回测子包：验证各策略历史信号质量（胜率/盈亏/持仓天数）。"""

from sequoia_x.backtest.engine import Backtester, StrategyReport, Trade

__all__ = ["Backtester", "StrategyReport", "Trade"]