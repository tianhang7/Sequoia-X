"""配置管理模块：通过 pydantic-settings 从环境变量或 .env 文件加载系统配置。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_path: str = "data/sequoia_v2.db"
    start_date: str = "2024-01-01"

    # Telegram 推送配置（默认且唯一的推送通道；未配置时跳过推送并记录告警）
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_api_base: str = "https://api.telegram.org"  # 支持自定义反代地址

    # ── 数据新鲜度检查 ──
    # strict: 库内最新K线不是最新交易日时，拒绝选股推送并告警退出（防止按过期信号下单）
    # warn:   仅记录告警并继续；off: 不检查
    data_freshness: str = "strict"

    # ── 选股结果过滤器（见 sequoia_x/strategy/filters.py）──
    enable_symbol_filters: bool = True
    filter_min_bars: int = 60  # K线不足 N 根视为次新股，剔除
    filter_min_avg_amount: float = 100_000_000.0  # 20日均成交额低于该值（元）剔除
    filter_drop_limit_up: bool = True  # 剔除信号日收盘涨停（次日大概率难买）的股票

    # ── 持仓/卖出提醒（见 sequoia_x/portfolio.py）──
    position_stop_loss: float = 0.07  # 硬止损：买入价 × (1 - 该比例)
    position_time_stop_days: int = 10  # 时间止损：持有 N 个交易日仍不盈利则提醒离场

    # ── 原始价同步（见 sequoia_x/data/engine.py）──
    enable_raw_prices: bool = True  # 日常增量/回填是否同步不复权收盘价（下单用）

    # ── 当日操作手册（见 sequoia_x/manual.py，落盘于 selections_dir 同级的 manuals 目录）──
    manual_take_profit: float = 0.20  # 止盈目标：信号日收盘 × (1 + 该比例)
    manual_push_top_n: int = 10  # 手册 Telegram 摘要买卖各取前 N 条；0 = 不推送手册摘要

    # ── 运行归档 ──
    log_file: str = "log.txt"  # 日志落盘路径；空字符串表示只输出到控制台
    selections_dir: str = "data/selections"  # 每日选股结果 JSON 归档目录

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # <--- 加上这一行！让 Pydantic 放行未定义的变量
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 Settings 单例。

    首次调用时从环境变量或 .env 文件加载配置。

    Returns:
        Settings: 全局唯一的配置实例。
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
