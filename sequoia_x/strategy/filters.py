"""选股结果过滤器：把策略原始候选收敛为「可实际下单」的清单。

对应实盘 SOP 的阶段 2（开盘前强制过滤），维度如下：

  1. 非主板/双创板块（北交所 4/8 开头、B 股 9 开头等）→ 剔除
  2. ST / *ST / 退市风险（读本地名称缓存，缓存为空时跳过该规则）
  3. 停牌或数据滞后（最后一根K线日期不等于 as_of、或成交量为 0）
  4. 次新股（K 线根数不足 filter_min_bars）
  5. 低流动性（20 日均成交额 < filter_min_avg_amount）
  6. 信号日收盘涨停（次日大概率买不进/追高，filter_drop_limit_up 控制）

所有规则均为「硬剔除」，被剔除的代码按原因分组返回，便于日志与归档复盘。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine

# A 股主板 + 创业板 + 科创板代码前缀白名单；其余（北交所 4/8、B 股 9 等）剔除
_ALLOWED_PREFIXES = (
    "600", "601", "603", "605",
    "000", "001", "002", "003",
    "300", "301",
    "688", "689",
)

_REASON_LABELS = {
    "non_main": "非主板/双创",
    "st": "ST/退",
    "stale": "停牌/数据缺失",
    "new_stock": "次新",
    "illiquid": "低流动性",
    "limit_up": "涨停",
    "data_error": "数据异常",
}


def board_limit_pct(symbol: str, name: str | None = None) -> float:
    """返回该股票的涨跌停幅度（小数）。

    规则：ST/*ST ±5%；创业板(30)、科创板(68) ±20%；北交所 ±30%；其余主板 ±10%。
    注意：新股上市初期不设涨跌幅，本函数无法识别（靠次新股规则兜底）。
    """
    if name and "ST" in name.upper():
        return 0.05
    if symbol.startswith(("300", "301", "688", "689")):
        return 0.20
    if symbol.startswith(("4", "8")):
        return 0.30
    return 0.10


def limit_up_price(prev_close: float, limit: float) -> float:
    """按 A 股最小变动价位 0.01 元四舍五入计算涨停价。"""
    return round(prev_close * (1.0 + limit), 2)


def is_limit_up(symbol: str, prev_close: float, close: float, name: str | None = None) -> bool:
    """close 是否收于涨停价（半个最小变动单位容差，容忍浮点误差）。"""
    if prev_close <= 0:
        return False
    target = limit_up_price(prev_close, board_limit_pct(symbol, name))
    return abs(close - target) < 0.005


@dataclass
class FilterResult:
    """过滤结果：保留清单 + 按原因分组的剔除清单。"""

    kept: list[str]
    dropped: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total_dropped(self) -> int:
        return sum(len(v) for v in self.dropped.values())

    def summary(self) -> str:
        """人类可读的剔除统计，如「ST/退 2、低流动性 27、涨停 40」。"""
        parts = [
            f"{_REASON_LABELS.get(reason, reason)} {len(syms)}"
            for reason, syms in self.dropped.items()
            if syms
        ]
        return "、".join(parts) if parts else "无"


def filter_symbols(
    symbols: list[str],
    engine: DataEngine,
    as_of: str | None,
    settings: Settings,
) -> FilterResult:
    """按配置对候选代码列表执行全套过滤。

    Args:
        symbols: 策略产出的原始候选代码。
        engine: 数据引擎，提供行情与名称缓存。
        as_of: 信号日（ISO 日期）；最后一根K线不等于该日的股票视为停牌/滞后。
        settings: 配置，决定各过滤规则的开关与阈值。

    Returns:
        FilterResult：kept 为保留清单，dropped 为按原因分组的剔除清单。
    """
    if not settings.enable_symbol_filters or not symbols:
        return FilterResult(list(symbols), {})

    dropped: dict[str, list[str]] = {}

    def _drop(reason: str, symbol: str) -> None:
        dropped.setdefault(reason, []).append(symbol)

    # 规则 1：板块白名单
    on_board = []
    for s in symbols:
        if s.startswith(_ALLOWED_PREFIXES):
            on_board.append(s)
        else:
            _drop("non_main", s)

    # 规则 2：ST / 退（名称缓存缺失时无法判断，跳过）
    try:
        names = engine.get_stock_names(on_board)
    except Exception:  # noqa: BLE001 - 名称只影响过滤精度，不阻断
        names = {}
    watchable: list[str] = []
    for s in on_board:
        nm = names.get(s) or ""
        if nm and ("ST" in nm.upper() or "退" in nm):
            _drop("st", s)
        else:
            watchable.append(s)

    # 规则 3~6：基于行情逐只判断
    kept: list[str] = []
    for s in watchable:
        try:
            df = engine.get_ohlcv(s)
            if as_of is not None:
                # 回测时库里会有 as_of 之后的数据，必须先切片再判断，
                # 否则所有股票都会被误判为「数据滞后」
                df = df[df["date"].astype(str) <= as_of]
        except Exception:  # noqa: BLE001
            _drop("data_error", s)
            continue

        if df.empty:
            _drop("stale", s)
            continue
        if as_of is not None and str(df["date"].iloc[-1]) != as_of:
            _drop("stale", s)
            continue

        last = df.iloc[-1]
        if not last["volume"] or float(last["volume"]) <= 0:
            _drop("stale", s)
            continue

        if len(df) < settings.filter_min_bars:
            _drop("new_stock", s)
            continue

        avg_amount_20d = float(df["turnover"].tail(20).mean() or 0.0)
        if avg_amount_20d < settings.filter_min_avg_amount:
            _drop("illiquid", s)
            continue

        if settings.filter_drop_limit_up and len(df) >= 2:
            prev_close = float(df["close"].iloc[-2])
            if is_limit_up(s, prev_close, float(last["close"]), names.get(s)):
                _drop("limit_up", s)
                continue

        kept.append(s)

    return FilterResult(kept, dropped)