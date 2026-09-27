"""交易日历：判断「最新交易日」，供数据新鲜度检查与回测取样使用。

设计原则：
- 本地缓存优先：交易日历保存在数据库同目录的 ``trade_calendar.json``，
  日常运行零网络依赖（只有缓存过期时才尝试联网刷新）。
- 联网降级：刷新失败时退化为「周一~周五视为交易日」的工作日推断，
  绝不阻断主流程（春节等长假会产生一次误报，但缓存过期窗口为 7 天，
  且失败会持续重试，影响可控）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

_FETCH_MAX_AGE_DAYS = 7  # 缓存超过 N 天才尝试联网刷新


def _fetch_from_akshare() -> set[date]:
    """从新浪获取全部历史交易日（akshare tool_trade_date_hist_sina）。"""
    import pandas as pd

    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    if df is None or df.empty or "trade_date" not in df.columns:
        raise ValueError("交易日历返回空数据")
    return set(pd.to_datetime(df["trade_date"]).dt.date)


class TradingCalendar:
    """A股交易日历（本地缓存 + 联网刷新 + 工作日降级）。"""

    def __init__(
        self,
        db_path: str,
        fetcher: Callable[[], set[date]] | None = None,
    ) -> None:
        """
        Args:
            db_path: 数据库路径；缓存文件存放于其同级目录。
            fetcher: 自定义抓取函数（测试注入用），默认走 akshare。
        """
        self.cache_path = Path(db_path).parent / "trade_calendar.json"
        self._fetcher = fetcher or _fetch_from_akshare
        self._dates: set[date] = set()
        self._fetched_at: date | None = None
        self._load()

    # ── 缓存读写 ──

    def _load(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._dates = {date.fromisoformat(s) for s in raw["dates"]}
            self._fetched_at = date.fromisoformat(raw["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            self._dates = set()
            self._fetched_at = None

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "fetched_at": (self._fetched_at or date.today()).isoformat(),
                "dates": sorted(d.isoformat() for d in self._dates),
            }
            self.cache_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"交易日历缓存写入失败（不影响本次运行）：{exc}")

    def refresh(self) -> bool:
        """联网刷新交易日历。成功返回 True；失败保留旧缓存并返回 False。"""
        try:
            dates = self._fetcher()
        except Exception as exc:  # noqa: BLE001 - 任何网络/解析错误都只降级
            logger.warning(f"交易日历刷新失败（降级为工作日推断）：{exc}")
            return False
        if not dates:
            logger.warning("交易日历刷新得到空集合，保留旧缓存")
            return False
        self._dates = dates
        self._fetched_at = date.today()
        self._save()
        logger.info(f"交易日历已刷新：{min(dates)} ~ {max(dates)}，共 {len(dates)} 天")
        return True

    def _ensure(self, ref: date) -> None:
        """确保缓存覆盖 ref；缓存过期（>7天）时尝试刷新一次。"""
        stale = self._fetched_at is None or (
            date.today() - self._fetched_at
        ).days >= _FETCH_MAX_AGE_DAYS
        covers = bool(self._dates) and max(self._dates) >= ref
        if stale or not covers:
            self.refresh()

    # ── 查询接口 ──

    def latest_trade_date(self, ref: date | None = None) -> date:
        """返回不晚于 ref 的最近一个交易日。"""
        ref = ref or date.today()
        self._ensure(ref)
        if self._dates:
            candidates = [d for d in self._dates if d <= ref]
            if candidates:
                return max(candidates)
        # 降级：周一~周五视为交易日
        d = ref
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

    def is_trading_day(self, ref: date | None = None) -> bool:
        """ref 当天是否为交易日（缓存缺失时按工作日推断）。"""
        ref = ref or date.today()
        self._ensure(ref)
        if self._dates:
            return ref in self._dates
        return ref.weekday() < 5

    def trade_dates(self, start: date, end: date) -> list[date]:
        """返回 [start, end] 区间内的全部交易日（升序），供回测取样。"""
        self._ensure(end)
        if self._dates:
            return sorted(d for d in self._dates if start <= d <= end)
        # 降级：逐日按工作日推断
        out: list[date] = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out