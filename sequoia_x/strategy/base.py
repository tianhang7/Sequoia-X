"""策略基类模块：定义所有选股策略的抽象接口。"""

from abc import ABC, abstractmethod

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


class BaseStrategy(ABC):
    """选股策略抽象基类。

    所有具体策略必须继承此类并实现 run() 方法。

    Attributes:
        strategy_key: 策略短标识，供 CLI/回测（--bt-strategies）以简称
            引用策略，例如 'ma_volume'、'turtle'。子类应覆盖此属性。
    """

    strategy_key: str = "default"

    def __init__(self, engine: DataEngine, settings: Settings) -> None:
        """
        初始化策略。

        Args:
            engine: DataEngine 实例，用于读取行情数据。
            settings: Settings 实例，用于读取配置。
        """
        self.engine = engine
        self.settings = settings

    @abstractmethod
    def run(self, as_of: str | None = None) -> list[str]:
        """
        执行选股逻辑，返回选中的股票代码列表。

        Args:
            as_of: 信号日（ISO 日期，如 '2026-09-25'）。为 None 时使用库内
                全部数据（日常模式）；回测时必须传入历史日期，且策略实现
                必须保证不使用该日期之后的任何数据（防止前视偏差）。

        Returns:
            满足策略条件的股票代码列表，如 ['000001', '600519']。
            无选股结果时返回空列表。
        """
        ...
