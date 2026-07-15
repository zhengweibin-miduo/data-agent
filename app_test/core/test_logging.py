"""应用日志配置检查。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from loguru import logger

from app.conf.app_config import LogConsoleConfig, LogFileConfig, LoggingConfig
from app.core.logging import setup_logging


def test_setup_logging() -> None:
    with TemporaryDirectory() as directory:
        log_dir = Path(directory) / "logs"
        config = LoggingConfig(
            console=LogConsoleConfig(enable=False, level="INFO"),
            file=LogFileConfig(
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


if __name__ == "__main__":
    test_setup_logging()
