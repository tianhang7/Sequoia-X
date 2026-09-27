"""Telegram 通知模块：将选股结果推送至 Telegram 频道或群组。"""

import re
from datetime import date

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine, fetch_stock_names_from_baostock

logger = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


class TelegramNotifier:
    """Telegram 推送器。

    当配置了 telegram_bot_token 和 telegram_chat_id 时，
    将选股结果以 MarkdownV2 / HTML 格式推送至 Telegram。
    股票名称优先读 DataEngine 的本地名称缓存表，缺失时降级为只显示代码。
    正文超长时按股票边界自动分片，分多条发送。
    """

    # 正文上限：Telegram 文档为 4096 字符（按实体解析后的可见文本计）
    MAX_RENDERED_LENGTH: int = 4096
    # 实测含 HTML 标签 7332 字符仍被接受，此处再留一道硬上限规避未公开限制
    MAX_RAW_LENGTH: int = 8192

    def __init__(self, settings: Settings, engine: DataEngine | None = None) -> None:
        self.settings = settings
        self.engine = engine
        self.bot_token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.api_base = settings.telegram_api_base.rstrip("/")

    @property
    def is_enabled(self) -> bool:
        """是否已配置并启用 Telegram 推送。"""
        return bool(self.bot_token and self.chat_id)

    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        """将纯数字代码转为雪球格式：6开头→SH，4/8开头→BJ，其余→SZ。"""
        if code.startswith("6"):
            return f"SH{code}"
        elif code.startswith(("4", "8")):
            return f"BJ{code}"
        return f"SZ{code}"

    def _get_stock_names(self, symbols: list[str]) -> dict[str, str]:
        """获取股票名称映射 {code: name}。

        优先读 DataEngine 的本地名称缓存表（推送阶段不发任何网络请求）；
        未注入 engine 时回退为 baostock 一次性全量拉取。
        名称只影响展示，任何异常都降级为空字典（仅展示代码），绝不阻断推送。
        """
        try:
            if self.engine is not None:
                return self.engine.get_stock_names(symbols)
            wanted = set(symbols)
            return {
                code: name
                for code, name in fetch_stock_names_from_baostock().items()
                if code in wanted
            }
        except Exception as e:
            logger.warning(f"获取股票名称失败，将仅使用代码展示: {e}")
            return {}

    def _build_header(self, strategy_name: str, total: int, page: int, pages: int) -> str:
        """构建消息表头；分片时在选股数量后标注页码，便于阅读。"""
        today = date.today().strftime("%Y-%m-%d")
        page_text = f"（第 {page}/{pages} 页）" if pages > 1 else ""
        return (
            f"📈 <b>Sequoia-X 选股播报 | {strategy_name}</b>\n\n"
            f"📅 <b>日期：</b> {today}\n"
            f"🎯 <b>策略：</b> {strategy_name}\n"
            f"🔢 <b>选股数量：</b> {total}{page_text}\n\n"
            f"<b>选股列表：</b>\n"
        )

    def _format_links(self, symbols: list[str], names: dict[str, str]) -> list[str]:
        """把代码列表格式化为可点击的 HTML 链接列表。"""
        links: list[str] = []
        for code in symbols:
            xq_code = self._to_xueqiu_code(code)
            name = names.get(code, code)
            links.append(f'<a href="https://xueqiu.com/S/{xq_code}">{name}({code})</a>')
        return links

    @staticmethod
    def _rendered_length(text: str) -> int:
        """计算实体解析后的可见文本长度（Telegram 的 4096 上限按此计）。"""
        return len(_TAG_RE.sub("", text))

    def _build_message(self, symbols: list[str], strategy_name: str) -> str:
        """构建未分片的完整 HTML 正文（诊断/兼容用；实际发送走 build_messages）。"""
        names = self._get_stock_names(symbols)
        links = self._format_links(symbols, names)
        content = " • ".join(links) if links else "（无选股结果）"
        return self._build_header(strategy_name, len(symbols), 1, 1) + content

    def build_messages(self, symbols: list[str], strategy_name: str) -> list[str]:
        """构建消息正文，超长时自动分片。

        在「单只股票」边界切分，保证 ``<a>`` 标签完整；每片同时满足
        MAX_RENDERED_LENGTH（可见文本）与 MAX_RAW_LENGTH（含标签原始长度）。
        """
        names = self._get_stock_names(symbols)
        links = self._format_links(symbols, names)
        if not links:
            return [self._build_header(strategy_name, 0, 1, 1) + "（无选股结果）"]

        # 表头长度随页码位数变化，用「最大页码」的表头长度做保守预算；
        # 分片数变化会影响预算，故迭代至稳定（最多 5 轮，收敛很快）
        pages = 1
        messages: list[str] = []
        for _ in range(5):
            messages = self._pack(links, strategy_name, len(symbols), pages)
            if len(messages) == pages:
                break
            pages = len(messages)
        return messages

    def _pack(self, links: list[str], strategy_name: str, total: int, pages: int) -> list[str]:
        """贪心装箱：按股票边界把链接装进若干条消息。

        预算用「最大页码」的表头长度，实际拼装时页头只会更短，
        因此每条消息都必然落在两个上限之内。
        """
        header = self._build_header(strategy_name, total, pages, pages)
        header_raw = len(header)
        header_rendered = self._rendered_length(header)
        sep_len = len(" • ")

        messages: list[str] = []
        current: list[str] = []
        raw_len = 0
        rendered_len = 0

        for link in links:
            link_raw = len(link)
            link_rendered = self._rendered_length(link)
            extra_raw = link_raw + (sep_len if current else 0)
            extra_rendered = link_rendered + (sep_len if current else 0)

            over_limit = (
                header_raw + raw_len + extra_raw > self.MAX_RAW_LENGTH
                or header_rendered + rendered_len + extra_rendered > self.MAX_RENDERED_LENGTH
            )
            if current and over_limit:
                messages.append(
                    self._build_header(strategy_name, total, len(messages) + 1, pages)
                    + " • ".join(current)
                )
                current = [link]
                raw_len = link_raw
                rendered_len = link_rendered
            else:
                current.append(link)
                raw_len += extra_raw
                rendered_len += extra_rendered

        if current:
            messages.append(
                self._build_header(strategy_name, total, len(messages) + 1, pages)
                + " • ".join(current)
            )
        return messages

    def send(
        self,
        symbols: list[str],
        strategy_name: str,
    ) -> bool:
        """发送选股结果到 Telegram。

        Args:
            symbols: 股票代码列表。
            strategy_name: 策略名称。

        Returns:
            bool: 推送是否成功。
        """
        if not self.is_enabled:
            return False

        url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        messages = self.build_messages(symbols, strategy_name)
        total_pages = len(messages)
        if total_pages > 1:
            logger.info(
                f"Telegram 消息超长，已按 {self.MAX_RENDERED_LENGTH} 字符上限"
                f"拆分为 {total_pages} 条发送"
            )

        all_ok = True
        for page, text in enumerate(messages, start=1):
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            suffix = f"第 {page}/{total_pages} 条，" if total_pages > 1 else ""

            try:
                resp = requests.post(
                    url,
                    json=payload,
                    timeout=10,
                )
                data = resp.json()
                if resp.status_code == 200 and data.get("ok"):
                    logger.info(
                        f"Telegram 推送成功 [{strategy_name}] {suffix}共 {len(symbols)} 只股票"
                    )
                else:
                    all_ok = False
                    logger.error(
                        f"Telegram 推送失败 [{strategy_name}] {suffix}"
                        f"HTTP={resp.status_code} 响应={resp.text}"
                    )
            except requests.RequestException as exc:
                all_ok = False
                logger.error(f"Telegram 推送请求异常 [{strategy_name}] {suffix}{exc}")

        return all_ok

    def _post_alert(self, text: str, title: str) -> bool:
        """发送单条告警文本；失败返回 False。"""
        url = f"{self.api_base}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                logger.info(f"Telegram 告警推送成功：{title}")
                return True
            logger.error(
                f"Telegram 告警推送失败：{title} HTTP={resp.status_code} 响应={resp.text}"
            )
            return False
        except requests.RequestException as exc:
            logger.error(f"Telegram 告警推送请求异常：{title} {exc}")
            return False

    def send_alert(self, title: str, body_html: str, emoji: str = "⚠️") -> bool:
        """推送任意 HTML 正文的告警消息（卖出提醒、数据不新鲜等）。

        正文超长时按「空行块」贪心打包为多条消息，保证每条都在
        4096 字符上限内。

        Returns:
            bool: 全部消息是否发送成功。
        """
        if not self.is_enabled:
            return False

        blocks = [b for b in body_html.split("\n\n") if b.strip()]
        if not blocks:
            blocks = [body_html]

        # 先贪心分页，拿到总页数后重建带页码的表头再发送
        pages: list[list[str]] = [[]]
        raw_len = 0
        for block in blocks:
            extra = len(block) + (2 if pages[-1] else 0)
            if pages[-1] and raw_len + extra > self.MAX_RENDERED_LENGTH - 64:
                pages.append([block])
                raw_len = len(block)
            else:
                pages[-1].append(block)
                raw_len += extra

        total = len(pages)
        all_ok = True
        for i, blocks_on_page in enumerate(pages, start=1):
            page_hint = f"（第 {i}/{total} 页）" if total > 1 else ""
            text = f"{emoji} <b>{title}</b>{page_hint}\n\n" + "\n\n".join(blocks_on_page)
            if not self._post_alert(text, title):
                all_ok = False
        return all_ok
