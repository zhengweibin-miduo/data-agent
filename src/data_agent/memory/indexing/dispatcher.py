"""记忆索引 outbox 调度。"""

from loguru import logger

from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.memory.indexing.elasticsearch import (
    MemoryElasticsearchIndex,
)
from data_agent.memory.indexing.qdrant import MemoryQdrantIndex
from data_agent.memory.mysql.index_outbox import (
    MemoryIndexOutboxRepository,
)
from data_agent.models.memory import (
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryOutboxItem,
    MemoryProjection,
    MemoryStatus,
)
from data_agent.settings import app_config


def _log_index_sync_deferred(task_id: str) -> None:
    """记录一个派生索引项目的安全退避结果。"""
    del task_id
    logger.warning("记忆索引同步失败，当前项目已进入退避并等待自动重试")


def _log_index_sync_superseded(task_id: str) -> None:
    """记录一次因权威内容再次变更而未确认的同步。"""
    del task_id
    logger.warning("权威内容在派生索引写入期间已变更，本次同步不确认并等待重新处理")


def _log_index_dead_letters(count: int) -> None:
    """记录已达死信阈值、需要人工介入的期望状态积压。"""
    logger.warning(
        "记忆索引期望状态已达最大失败次数并停止重试，待人工处理项数：{count}",
        count=count,
    )


def _writable_projection(
    item: MemoryOutboxItem,
    projection: MemoryProjection | None,
) -> MemoryProjection | None:
    """裁决单个期望状态应写入的投影，返回 None 表示应删除派生文档。

    UPSERT 的语义是"让派生索引收敛到权威状态"：权威行已被物理清理或不再处于
    ACTIVE 时同样收敛为删除，而不是写入失效内容或空确认，否则用户已删除的内容
    会在派生索引中永久残留。
    """
    # 步骤一：显式删除期望直接收敛为删除。
    if item.operation == MemoryIndexOperation.DELETE:
        return None
    # 步骤二：权威行缺失或非 ACTIVE 时，UPSERT 同样收敛为删除。
    if projection is None or projection.status != MemoryStatus.ACTIVE:
        return None
    # 步骤三：其余情况按权威投影写入派生索引。
    return projection


class MemoryIndexDispatcher:
    """独立确认 ES/Qdrant 投影期望状态。"""

    async def dispatch(self) -> int:
        """有界领取并处理一个 outbox 批次。

        领取、外部写入与确认分处三段事务：MySQL 行锁只在领取和确认的短事务内
        持有，任何 ES/Qdrant/TEI 调用都发生在事务之外。外部服务变慢因此不会
        阻塞写入同一批期望状态的用户事务。
        """
        # 步骤一：短事务内只领取一批期望状态并写入领取租约，提交后立即释放行锁。
        batch_size = app_config.memory.outbox_batch_size
        async with MySQLDatabase.session() as session:
            items = await MemoryIndexOutboxRepository(session).claim_outbox(batch_size)
        if not items:
            await self._report_dead_letters()
            return 0
        # 步骤二：逐项处理，投影读取也在逐项隔离边界之内。投影会解码历史 JSON，
        # 一条无法解码的权威行若在批次级读取，异常会逃出 dispatch 并中止整批：
        # 本批已被推进租约的行都不会退避、不会计入尝试次数，租约到期后同一错误
        # 无限重现，足以阻断全部记忆索引同步。
        processed = 0
        for item in items:
            if await self._synchronize(item):
                processed += 1
        # 步骤四：批次未被填满说明可领取队列已排空，此时才统计死信积压，
        # 避免在饱和运行时对 outbox 反复做全表计数。
        if len(items) < batch_size:
            await self._report_dead_letters()
        return processed

    async def _report_dead_letters(self) -> None:
        """统计并暴露已停止重试的期望状态积压。"""
        # 步骤一：死信行不再参与领取，只能通过日志暴露，避免静默积压。
        async with MySQLDatabase.session() as session:
            dead_letters = await MemoryIndexOutboxRepository(
                session
            ).dead_letter_count()
        if dead_letters:
            _log_index_dead_letters(dead_letters)

    async def _synchronize(self, item: MemoryOutboxItem) -> bool:
        """执行单项投影读取、外部写入，并在独立短事务中确认或退避。"""
        try:
            # 步骤一：读取权威投影并完成外部写入。投影解码失败与外部调用失败共用
            # 同一个退避出口，任一确定性错误都只影响本项，不会中止整批。
            async with MySQLDatabase.session() as session:
                projection = await MemoryIndexOutboxRepository(session).projection(
                    item.memory_uid
                )
            writable = _writable_projection(item, projection)
            await self._apply(item, writable)
        except Exception as error:
            # 步骤二：单目标失败只退避自身行，保留期望状态供后续重试。
            async with MySQLDatabase.session() as session:
                await MemoryIndexOutboxRepository(session).retry_outbox(
                    item,
                    type(error).__name__,
                    app_config.memory.outbox_max_backoff_seconds,
                )
            _log_index_sync_deferred(item.memory_uid)
            return False
        # 步骤三：按本次实际写入的内容确认；权威内容在外部写入期间再次变更时
        # 不确认，并按当前权威状态重建该目标的收敛请求——否则并发删除遇到较慢的
        # 外部调用时，迟到写入会把已删除内容重新写进派生索引且再无 outbox 请求
        # 可以纠正它，造成永久残留。
        async with MySQLDatabase.session() as session:
            repository = MemoryIndexOutboxRepository(session)
            acknowledged = await repository.acknowledge_outbox(
                item,
                content_hash=(writable.content_hash if writable is not None else None),
            )
            if not acknowledged:
                # 收敛请求是持久的：outbox 不设向 agent_memory 的外键，因此即使
                # 权威行已被 purge 物理清理，这条 DELETE 请求依然能登记，并由后续
                # 周期按普通 outbox 项重试——补偿不再是一次性远程调用。
                await repository.enqueue_convergence(item.memory_uid, item.target)
        if not acknowledged:
            _log_index_sync_superseded(item.memory_uid)
        return acknowledged

    async def _apply(
        self,
        item: MemoryOutboxItem,
        projection: MemoryProjection | None,
    ) -> None:
        """把单个期望状态写入对应派生索引；投影为 None 表示删除。"""
        # 步骤一：Elasticsearch 目标按裁决结果执行幂等删除或写入。
        if item.target == MemoryIndexTarget.ELASTICSEARCH:
            text_index = MemoryElasticsearchIndex(ElasticsearchClient.get_client())
            if projection is None:
                await text_index.delete(item.memory_uid)
                return
            await text_index.upsert(projection)
            return
        # 步骤二：Qdrant 目标仅在需要写入时才请求向量，删除路径不调用 TEI。
        vector_index = MemoryQdrantIndex(QdrantClient.get_client())
        if projection is None:
            await vector_index.delete(item.memory_uid)
            return
        embeddings = TEIEmbeddingClient.get_client()
        vector = await embeddings.aembed_documents([projection.memory_text])
        await vector_index.upsert(projection, vector[0])
