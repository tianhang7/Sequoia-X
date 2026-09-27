"""日志模块：基于 rich 库提供带颜色的结构化终端日志输出。"""

import logging
import sys

from rich.logging import RichHandler

_FORMAT = "%(name)s - %(message)s"

_UTF8_READY = False
_FILE_HANDLER: logging.Handler | None = None


def configure_file_logging(path: str, level: int = logging.INFO) -> logging.Handler | None:
    """把日志同时写入文件（UTF-8），返回已挂载的 FileHandler。

    幂等：对同一路径重复调用复用现有 handler；路径为空时关闭文件日志
    （移除既有 handler）。

    注意：必须在首次 ``get_logger`` 之前调用效果最好；之后调用会把 handler
    补挂到已创建的 logger 上，但不会重复挂载。
    """
    global _FILE_HANDLER

    # 关闭既有文件日志
    if not path:
        if _FILE_HANDLER is not None:
            for lg in _live_loggers():
                if _FILE_HANDLER in lg.handlers:
                    lg.removeHandler(_FILE_HANDLER)
            _close_quietly(_FILE_HANDLER)
            _FILE_HANDLER = None
        return None

    # 已配置同一路径 → 复用
    if _FILE_HANDLER is not None:
        existing = getattr(_FILE_HANDLER, "baseFilename", None)
        if existing and str(existing) == str(path):
            return _FILE_HANDLER
        configure_file_logging("")  # 递归清理旧 handler

    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.name = "sequoia-file"
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    )
    for lg in _live_loggers():
        lg.addHandler(handler)
    _FILE_HANDLER = handler
    return handler


def _live_loggers() -> list[logging.Logger]:
    """返回本模块创建过的全部 logger（含尚未被请求的延迟创建场景）。"""
    out: list[logging.Logger] = []
    for name in getattr(logging.Logger.manager, "loggerDict", {}):
        lg = logging.getLogger(name)
        if any(isinstance(h, RichHandler) or h.name == "sequoia-file" for h in lg.handlers):
            out.append(lg)
    return out


def _close_quietly(handler: logging.Handler) -> None:
    try:
        handler.close()
    except Exception:  # noqa: BLE001 - 关闭失败无须影响主流程
        pass


def _ensure_utf8_output() -> None:
    """把「被重定向的」stdout/stderr 升级为 UTF-8，避免中文日志抛 UnicodeEncodeError。

    背景：rich 的 Console 默认写入 sys.stdout。在 Windows 上把输出重定向到
    管道/文件时，Python 会退回到系统 ANSI 代码页（如 cp1252、cp936）编码，
    此时任何中文都会抛 ``UnicodeEncodeError``；logging 会把它降级成
    ``--- Logging error ---`` 堆栈并丢弃原消息，导致进度、失败股票列表等
    关键信息在 ``python main.py > log.txt`` 场景下完全不可见。

    仅在非交互式（重定向）流上生效，避免覆盖真实终端自身的编码设置。
    """
    global _UTF8_READY
    if _UTF8_READY:
        return
    _UTF8_READY = True

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding.replace("_", "-") in ("utf-8", "utf8"):
            continue
        isatty = getattr(stream, "isatty", None)
        try:
            if isatty is None or not isatty():
                reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 流已被包装/关闭，保持原样即可，不影响日志功能
            continue


def get_logger(name: str) -> logging.Logger:
    """
    工厂函数，返回配置了 RichHandler 的 Logger 实例。

    支持 DEBUG/INFO/WARNING/ERROR 四级日志，由 rich 自动以不同颜色渲染。
    每条日志包含时间戳、模块名和日志级别。
    同名 logger 不重复添加 handler（幂等性）。

    Args:
        name: logger 名称，通常传入 __name__。

    Returns:
        logging.Logger: 配置好的 Logger 实例。
    """
    _ensure_utf8_output()

    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    handler = RichHandler(
        rich_tracebacks=True,
        show_path=False,
        log_time_format="[%Y-%m-%d %H:%M:%S]",
    )
    handler.setFormatter(logging.Formatter(_FORMAT))

    logger.addHandler(handler)
    if _FILE_HANDLER is not None and _FILE_HANDLER not in logger.handlers:
        logger.addHandler(_FILE_HANDLER)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    return logger
