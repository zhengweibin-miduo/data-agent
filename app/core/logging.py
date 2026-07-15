"""应用日志配置。"""

import sys

from loguru import logger

from app.conf.app_config import LoggingConfig, app_config

CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level:<8}</level> | "
    "<cyan>{name}:{function}:{line}</cyan> | "
    "trace_id={extra[trace_id]} | <level>{message}</level>"
)
FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
    "pid={process.id} | {name}:{function}:{line} | "
    "trace_id={extra[trace_id]} | {message}"
)


def setup_logging(config: LoggingConfig = app_config.logging) -> None:
    """根据应用配置重建 Loguru sinks。"""
    logger.remove()
    logger.configure(extra={"trace_id": "-"})

    if config.console.enable:
        logger.add(
            sys.stderr,
            level=config.console.level,
            format=CONSOLE_FORMAT,
            colorize=True,
            diagnose=False,
        )

    if config.file.enable:
        config.file.path.mkdir(parents=True, exist_ok=True)
        logger.add(
            config.file.path / "data-agent.log",
            level=config.file.level,
            format=FILE_FORMAT,
            rotation=config.file.rotation,
            retention=config.file.retention,
            encoding="utf-8",
            diagnose=False,
        )
