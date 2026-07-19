"""DDL 元数据 worker 周期维护任务。"""

from typing import Any, cast

from arq.connections import ArqRedis
from loguru import logger
from redis.exceptions import RedisError

from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.memory.indexing.dispatcher import MemoryIndexDispatcher
from data_agent.infrastructure.checkpoint_store import CheckpointStore


async def dispatch_pending(ctx: dict[Any, Any]) -> None:
    """启动及周期性排空 dispatch outbox。"""
    jobs = cast(DDLJobStore, ctx["jobs"])
    queue = cast(ArqRedis, ctx["redis"])
    await jobs.dispatch(queue)


async def expire_waiting(ctx: dict[Any, Any]) -> None:
    """周期性拒绝过期等待并删除对应检查点。"""
    jobs = cast(DDLJobStore, ctx["jobs"])
    await jobs.expire_waiting()
    await cleanup_checkpoints(ctx)


async def cleanup_checkpoints(ctx: dict[Any, Any]) -> None:
    """重试删除所有已进入终态的 LangGraph 线程。"""
    jobs = cast(DDLJobStore, ctx["jobs"])
    job_ids = await jobs.pending_checkpoint_cleanup()
    if not job_ids:
        return
    checkpointer = CheckpointStore.get_client()
    for job_id in job_ids:
        try:
            await checkpointer.adelete_thread(job_id)
        except RedisError as error:
            logger.bind(
                trace_id=job_id,
                component="ddl_metadata.worker",
                event_name="ddl_metadata.checkpoint.cleanup_deferred",
                operation="cleanup_checkpoint",
                outcome="deferred",
                error_type=type(error).__name__,
                retryable=True,
            ).warning("终态检查点清理延后")
            continue
        await jobs.acknowledge_checkpoint_cleanup(job_id)


async def dispatch_memory_index_outbox(ctx: dict[Any, Any]) -> None:
    """周期性同步可重建 ES/Qdrant 记忆投影。"""
    del ctx
    await MemoryIndexDispatcher().dispatch()
