"""arq worker 发现设置。"""

from collections.abc import Callable
from typing import Any

from arq import cron, func
from arq.connections import RedisSettings

from data_agent.ddl_metadata.worker.job_runner import run_ddl_job
from data_agent.ddl_metadata.worker.lifecycle import shutdown, startup
from data_agent.ddl_metadata.worker.maintenance import (
    cleanup_checkpoints,
    dispatch_memory_index_outbox,
    dispatch_pending,
    expire_memories,
    expire_waiting,
    extract_conversation_memory,
    purge_user_memories,
    reap_stalled_jobs,
)
from data_agent.logging import logging_boundary
from data_agent.settings import app_config


def _observed(function: Callable[..., Any]) -> Callable[..., Any]:
    """在 arq 注册 seam 外部织入零配置日志边界。"""
    return logging_boundary()(function)


class WorkerSettings:
    """arq 可发现的 worker 设置。"""

    functions = [
        func(
            _observed(run_ddl_job),
            keep_result=0,
            timeout=app_config.redis.worker_job_timeout_seconds,
            max_tries=3,
        )
    ]
    cron_jobs = [
        cron(
            _observed(dispatch_pending),
            second={0, 10, 20, 30, 40, 50},
            run_at_startup=True,
        ),
        cron(_observed(expire_waiting), minute=None, second=0),
        cron(_observed(expire_memories), minute=None, second=1),
        cron(_observed(reap_stalled_jobs), minute=None, second=3),
        cron(
            _observed(cleanup_checkpoints),
            second={5, 15, 25, 35, 45, 55},
        ),
        cron(
            _observed(dispatch_memory_index_outbox),
            second={2, 12, 22, 32, 42, 52},
            run_at_startup=True,
        ),
        cron(
            _observed(extract_conversation_memory),
            second={4, 14, 24, 34, 44, 54},
            run_at_startup=True,
        ),
        cron(
            _observed(purge_user_memories),
            second={7, 17, 27, 37, 47, 57},
        ),
    ]
    on_startup = _observed(startup)
    on_shutdown = _observed(shutdown)
    redis_settings = RedisSettings.from_dsn(app_config.redis.url)
    max_jobs = app_config.redis.worker_concurrency
    job_timeout = app_config.redis.worker_job_timeout_seconds
    retry_jobs = True
    keep_result = 0
