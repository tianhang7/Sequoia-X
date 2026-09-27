"""日志落盘（configure_file_logging）测试。

注意：本文件可能早于 test_logger.py 执行，测试结束必须清理全局
_FILE_HANDLER 状态，避免污染「get_logger 只有一个 handler」属性。
"""

from pathlib import Path

from sequoia_x.core import logger as logger_mod


def test_configure_file_logging_writes_and_cleans_up(tmp_path):
    log_path = str(tmp_path / "run.log")
    name = "sequoia.test_file_logging"

    handler = logger_mod.configure_file_logging(log_path)
    try:
        assert handler is not None
        log = logger_mod.get_logger(name)
        # Rich 控制台 + 文件 两个 handler
        assert len(log.handlers) == 2
        log.info("hello-file-日志")
        for h in log.handlers:
            h.flush()
        content = Path(log_path).read_text(encoding="utf-8")
        assert "hello-file-日志" in content

        # 重复调用同一路径 → 复用，不叠加 handler
        handler2 = logger_mod.configure_file_logging(log_path)
        assert handler2 is handler
        assert len(logger_mod.get_logger(name).handlers) == 2
    finally:
        # 清理全局状态，避免影响 test_logger 的属性测试
        logger_mod.configure_file_logging("")

    assert logger_mod._FILE_HANDLER is None
    assert len(logger_mod.get_logger(name).handlers) == 1
