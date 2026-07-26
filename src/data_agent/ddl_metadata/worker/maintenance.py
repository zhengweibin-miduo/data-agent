"""DDL 元数据 worker 周期维护任务。"""

from typing import Any, cast

from arq.connections import ArqRedis
from loguru import logger
from redis.exceptions import RedisError

from data_agent.conversation.extraction import ConversationMemoryExtractor
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.infrastructure.checkpoint_store import CheckpointStore
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.memory.indexing.dispatcher import MemoryIndexDispatcher
from data_agent.memory.mysql.repository import MemoryRepository


def _log_checkpoint_cleanup_deferred(job_id: str) -> None:
    """记录一个检查点将在后续周期继续清理。"""
    del job_id
    logger.warning("终态检查点清理失败，清理记录已保留并将在下个周期重试")


async def dispatch_pending(ctx: dict[Any, Any]) -> None:
    """启动及周期性排空 dispatch outbox。"""
    jobs = cast(DDLJobStore, ctx["jobs"])
    queue = cast(ArqRedis, ctx["redis"])
    await jobs.dispatch(queue)


async def expire_waiting(ctx: dict[Any, Any]) -> None:
    """周期性拒绝过期等待并删除对应检查点。"""
    # 步骤一：先让权威任务状态原子进入过期终态并写入 checkpoint 清理 outbox。
    jobs = cast(DDLJobStore, ctx["jobs"])
    await jobs.expire_waiting()
    # 步骤二：再尝试消费清理 outbox；失败会保留请求供后续周期重放。
    await cleanup_checkpoints(ctx)


async def reap_stalled_jobs(ctx: dict[Any, Any]) -> None:
    """周期性回收被重试预算耗尽后遗留的停滞任务。"""
    # 步骤一：按活动索引发现停滞任务并重新激活或判定失败。
    jobs = cast(DDLJobStore, ctx["jobs"])
    reaped = await jobs.reap_stalled()
    # 步骤二：被重新登记的激活请求由 dispatch outbox 在同一周期内尽快排空。
    if reaped:
        queue = cast(ArqRedis, ctx["redis"])
        await jobs.dispatch(queue)


async def cleanup_checkpoints(ctx: dict[Any, Any]) -> None:
    """重试删除所有已进入终态的 LangGraph 线程。"""
    # 步骤一：读取终态转换写入的清理 outbox；没有待处理项时立即结束。
    jobs = cast(DDLJobStore, ctx["jobs"])
    job_ids = await jobs.pending_checkpoint_cleanup()
    if not job_ids:
        return
    # 步骤二：逐项删除 checkpoint；失败不确认，确保 worker 崩溃或 Redis
    # 短暂失败时维护任务仍能重放，而不会静默遗留或提前丢失 checkpoint。
    checkpointer = CheckpointStore.get_client()
    for job_id in job_ids:
        try:
            await checkpointer.adelete_thread(job_id)
        except RedisError:
            _log_checkpoint_cleanup_deferred(job_id)
            continue
        # 步骤三：只有线程删除成功后才确认 outbox，避免提前丢失唯一清理请求。
        await jobs.acknowledge_checkpoint_cleanup(job_id)


async def dispatch_memory_index_outbox(ctx: dict[Any, Any]) -> None:
    """周期性同步可重建 ES/Qdrant 记忆投影。"""
    del ctx
    await MemoryIndexDispatcher().dispatch()


async def extract_conversation_memory(ctx: dict[Any, Any]) -> None:
    """周期性提炼完成对话轮次的用户长期记忆。"""
    extractor = cast(
        ConversationMemoryExtractor,
        ctx["conversation_extractor"],
    )
    await extractor.dispatch()


async def purge_user_memories(ctx: dict[Any, Any]) -> None:
    """物理清理派生索引已确认删除的用户记忆。"""
    del ctx
    async with MySQLDatabase.session() as session:
        await MemoryRepository(session).purge_ready_user_memories()


async def expire_memories(ctx: dict[Any, Any]) -> None:
    """周期性失效到期记忆并投递索引删除。"""
    del ctx
    async with MySQLDatabase.session() as session:
        await MemoryRepository(session).expire_due()
