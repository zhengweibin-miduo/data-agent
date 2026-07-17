"""应用日志配置检查。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from loguru import logger

from data_agent.logging import setup_logging
from data_agent.settings import (
    ConsoleLoggingSettings,
    FileLoggingSettings,
    LoggingSettings,
)


def test_setup_logging() -> None:
    """验证重复配置、默认上下文与 UTF-8 文件输出。"""
    with TemporaryDirectory() as directory:
        log_dir = Path(directory) / "logs"
        config = LoggingSettings(
            console=ConsoleLoggingSettings(enable=False, level="INFO"),
            file=FileLoggingSettings(
                enable=True,
                level="INFO",
                path=log_dir,
                rotation="10 MB",
                retention="7 days",
            ),
        )

        try:
            setup_logging(config)
            setup_logging(config)
            logger.info("setup-check")
            logger.bind(trace_id="trace-1").info("trace-check")

            output = (log_dir / "data-agent.log").read_text(encoding="utf-8")
            assert output.count("setup-check") == 1
            assert "trace_id=- | setup-check" in output
            assert "trace_id=trace-1 | trace-check" in output
        finally:
            logger.remove()
