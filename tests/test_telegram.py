"""Telegram 通知单元测试。"""

import re
from unittest.mock import MagicMock, patch

import pytest

from sequoia_x.core.config import Settings
from sequoia_x.notify.telegram import TelegramNotifier


def test_telegram_notifier_disabled():
    settings = Settings(
        telegram_bot_token=None,
        telegram_chat_id=None,
    )
    notifier = TelegramNotifier(settings)
    assert not notifier.is_enabled
    assert not notifier.send(["000001"], "TestStrategy")


def test_telegram_notifier_send_success():
    settings = Settings(
        telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        telegram_chat_id="-1001234567890",
    )
    notifier = TelegramNotifier(settings)
    assert notifier.is_enabled

    with patch("requests.post") as mock_post, patch.object(notifier, "_get_stock_names", return_value={"000001": "平安银行"}):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": {}}
        mock_post.return_value = mock_resp

        success = notifier.send(["000001"], "TestStrategy")
        assert success is True

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        payload = call_kwargs["json"]
        assert payload["chat_id"] == "-1001234567890"
        assert payload["parse_mode"] == "HTML"
        assert "平安银行(000001)" in payload["text"]
        assert "TestStrategy" in payload["text"]


def test_telegram_notifier_send_failure():
    settings = Settings(
        telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        telegram_chat_id="-1001234567890",
    )
    notifier = TelegramNotifier(settings)

    with patch("requests.post") as mock_post, patch.object(notifier, "_get_stock_names", return_value={}):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"ok": False, "description": "Chat not found"}
        mock_resp.text = '{"ok":false,"description":"Chat not found"}'
        mock_post.return_value = mock_resp

        success = notifier.send(["600519"], "TurtleTrade")
        assert success is False


def _make_notifier() -> TelegramNotifier:
    """构造一个已启用的 TelegramNotifier（不联网）。"""
    settings = Settings(
        telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        telegram_chat_id="-1001234567890",
    )
    return TelegramNotifier(settings)


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"ok": True, "result": {}}
    return resp


def _fail_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 400
    resp.json.return_value = {"ok": False, "description": "Bad Request: message is too long"}
    resp.text = '{"ok":false,"description":"Bad Request: message is too long"}'
    return resp


def _assert_within_limits(text: str) -> None:
    """校验单条消息同时满足可见文本与原始长度上限，且 HTML 标签完整。"""
    assert len(text) <= TelegramNotifier.MAX_RAW_LENGTH
    assert TelegramNotifier._rendered_length(text) <= TelegramNotifier.MAX_RENDERED_LENGTH
    assert text.count("<a ") == text.count("</a>")


def test_telegram_short_message_is_not_split():
    """短消息不应被拆分，且与未分片的完整正文完全一致。"""
    notifier = _make_notifier()
    with patch.object(notifier, "_get_stock_names", return_value={"000001": "平安银行"}):
        messages = notifier.build_messages(["000001", "600519"], "TestStrategy")
        assert messages == [notifier._build_message(["000001", "600519"], "TestStrategy")]
    assert "平安银行(000001)" in messages[0]
    assert "第 1/" not in messages[0]


def test_telegram_empty_symbols_single_message():
    notifier = _make_notifier()
    with patch.object(notifier, "_get_stock_names", return_value={}):
        messages = notifier.build_messages([], "TestStrategy")
    assert len(messages) == 1
    assert "（无选股结果）" in messages[0]


def test_telegram_long_message_is_split_into_pages():
    """300 只股票应按分片边界切开，且每片都在上限内、股票不重不漏。"""
    notifier = _make_notifier()
    symbols = [f"{i:06d}" for i in range(1, 301)]
    names = {code: f"股票{code}" for code in symbols if int(code) % 2 == 0}

    with patch.object(notifier, "_get_stock_names", return_value=names):
        messages = notifier.build_messages(symbols, "RpsBreakout")

    assert len(messages) > 1
    for text in messages:
        _assert_within_limits(text)

    # 页眉带页码，且所有股票恰好出现一次
    for page, text in enumerate(messages, start=1):
        assert f"（第 {page}/{len(messages)} 页）" in text
        assert "<b>选股数量：</b> 300" in text

    pushed = [code for text in messages for code in re.findall(r"\((\d{6})\)</a>", text)]
    assert pushed == symbols
    assert "股票000002(000002)" in "".join(messages)


def test_telegram_send_pushes_every_page_and_returns_true():
    notifier = _make_notifier()
    symbols = [f"{i:06d}" for i in range(1, 301)]

    with patch("requests.post") as mock_post, patch.object(
        notifier, "_get_stock_names", return_value={}
    ):
        expected_pages = len(notifier.build_messages(symbols, "RpsBreakout"))
        assert expected_pages > 1

        mock_post.return_value = _ok_response()
        assert notifier.send(symbols, "RpsBreakout") is True
        assert mock_post.call_count == expected_pages
        payloads = [call.kwargs["json"] for call in mock_post.call_args_list]

    # 每个分片各发一条，顺序与 build_messages 一致
    for payload in payloads:
        assert payload["chat_id"] == "-1001234567890"
        assert payload["parse_mode"] == "HTML"
        _assert_within_limits(payload["text"])


def test_telegram_send_returns_false_when_a_page_fails():
    """分片发送时某一片失败：仍会尝试剩余分片，但整体返回 False。"""
    notifier = _make_notifier()
    symbols = [f"{i:06d}" for i in range(1, 301)]
    attempts: list[int] = []

    def _post(*args, **kwargs):
        attempts.append(len(attempts) + 1)
        return _ok_response() if len(attempts) == 1 else _fail_response()

    with patch("requests.post", side_effect=_post), patch.object(
        notifier, "_get_stock_names", return_value={}
    ):
        expected_pages = len(notifier.build_messages(symbols, "RpsBreakout"))
        assert notifier.send(symbols, "RpsBreakout") is False

    assert expected_pages > 1
    assert len(attempts) == expected_pages


def test_telegram_rendered_limit_is_enforced(monkeypatch):
    """可见文本上限才是 Telegram 的硬约束，缩小它应能让分片变多。"""
    monkeypatch.setattr(TelegramNotifier, "MAX_RENDERED_LENGTH", 300)
    notifier = _make_notifier()
    symbols = [f"{i:06d}" for i in range(1, 61)]

    with patch.object(notifier, "_get_stock_names", return_value={}):
        messages = notifier.build_messages(symbols, "TurtleTrade")

    assert len(messages) > 1
    for text in messages:
        assert TelegramNotifier._rendered_length(text) <= 300
    pushed = [code for text in messages for code in re.findall(r"\((\d{6})\)</a>", text)]
    assert pushed == symbols


def test_telegram_tiny_limit_keeps_one_symbol_per_page(monkeypatch):
    """上限小于表头时不能让装箱死循环：每页至少放一只股票。"""
    monkeypatch.setattr(TelegramNotifier, "MAX_RENDERED_LENGTH", 10)
    notifier = _make_notifier()
    symbols = [f"{i:06d}" for i in range(1, 4)]

    with patch.object(notifier, "_get_stock_names", return_value={}):
        messages = notifier.build_messages(symbols, "TurtleTrade")

    assert len(messages) == len(symbols)
    for code, text in zip(symbols, messages):
        assert text.count("<a ") == 1
        assert f"({code})</a>" in text
