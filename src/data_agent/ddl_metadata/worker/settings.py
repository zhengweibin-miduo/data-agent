"""arq worker 发现设置。"""

from arq import cron, func
from arq.connections import RedisSettings

from data_agent.ddl_metadata.worker.job_runner import run_ddl_job
from data_agent.ddl_metadata.worker.lifecycle import shutdown, startup
from data_agent.ddl_metadata.worker.maintenance import (
    cleanup_checkpoints,
    dispatch_memory_index_outbox,
    dispatch_pending,
    expire_waiting,
    extract_conversation_memory,
    purge_user_memories,
)
from data_agent.settings import app_config


class WorkerSettings:
    """arq 可发现的 worker 设置。"""

    functions = [
        func(
            run_ddl_job,
            keep_result=0,
            timeout=app_config.redis.worker_job_timeout_seconds,
            max_tries=3,
        )
    ]
    cron_jobs = [
        cron(
            dispatch_pending,
            second={0, 10, 20, 30, 40, 50},
            run_at_startup=True,
        ),
        cron(expire_waiting, minute=None, second=0),
        cron(
            cleanup_checkpoints,
            second={5, 15, 25, 35, 45, 55},
        ),
        cron(
            dispatch_memory_index_outbox,
            second={2, 12, 22, 32, 42, 52},
            run_at_startup=True,
        ),
        cron(
            extract_conversation_memory,
            second={4, 14, 24, 34, 44, 54},
            run_at_startup=True,
        ),
        cron(
            purge_user_memories,
            second={7, 17, 27, 37, 47, 57},
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(app_config.redis.url)
    max_jobs = app_config.redis.worker_concurrency
    job_timeout = app_config.redis.worker_job_timeout_seconds
    retry_jobs = True
    keep_result = 0
