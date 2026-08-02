"""记忆派生索引期望状态仓储。"""

from uuid import uuid4

from sqlalchemy import (
    RowMapping,
    delete,
    exists,
    func,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from memory.domain.payloads import content_object_ids
from memory.mysql.tables import (
    agent_memory,
    memory_index_outbox,
)
from models.memory import (
    MEMORY_CONTENT_ADAPTER,
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryLifecyclePolicy,
    MemoryOutboxItem,
    MemoryProjection,
    MemoryStatus,
    MemoryTrust,
)
from settings import app_config


class MemoryIndexOutboxRepository:
    """在调用方事务中管理派生索引期望状态与投影读取。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由调用方管理事务边界的 Session。"""
        self._session = session

    async def set_desired_state(
        self,
        memory_uids: set[str],
        operation: MemoryIndexOperation,
    ) -> None:
        """覆盖每个索引目标的期望状态。"""
        if not memory_uids:
            return
        # 步骤一：为每个权威 UID 展开两个派生目标的最新期望状态。
        values = [
            {
                "memory_uid": uid,
                "target": target.value,
                "operation": operation.value,
                "projection_version": app_config.memory.projection_version,
                "attempts": 0,
                "available_at": func.now(),
                "lease_token": None,
                "last_error_type": None,
            }
            for uid in memory_uids
            for target in MemoryIndexTarget
        ]
        # 步骤二：按 UID 与目标幂等覆盖状态，并重置重试进度。覆盖必须同时作废领取
        # 代次令牌：否则 attempts 被重置为 0 后，仍持有旧令牌的迟到 worker 还能命中
        # 这一行并把新期望直接写到死信上限，使最新内容再也不被领取。
        statement = insert(memory_index_outbox).values(values)
        await self._session.execute(
            statement.on_duplicate_key_update(
                operation=statement.inserted.operation,
                projection_version=statement.inserted.projection_version,
                attempts=0,
                available_at=func.now(),
                lease_token=None,
                last_error_type=None,
            )
        )

    async def pending_targets(
        self,
        uids: set[str],
    ) -> dict[str, set[MemoryIndexTarget]]:
        """批量读取尚未确认的派生索引目标。"""
        if not uids:
            return {}
        # 步骤一：批量读取指定 UID 尚未完成的目标行。
        rows = (
            await self._session.execute(
                select(
                    memory_index_outbox.c.memory_uid,
                    memory_index_outbox.c.target,
                ).where(memory_index_outbox.c.memory_uid.in_(uids))
            )
        ).all()
        # 步骤二：按权威 UID 聚合待确认目标，缺少记录表示该 UID 已无待办。
        result: dict[str, set[MemoryIndexTarget]] = {}
        for uid, target in rows:
            result.setdefault(str(uid), set()).add(MemoryIndexTarget(str(target)))
        return result

    async def claim_outbox(self, limit: int) -> list[MemoryOutboxItem]:
        """通过行锁有界领取可执行索引期望状态并写入领取租约。"""
        # 步骤一：按可用时间跳锁领取有界任务，使并发 dispatcher 不重复处理；
        # 已达死信阈值的行保留在表中但不再参与领取，避免确定性故障无限重试。
        rows = (
            (
                await self._session.execute(
                    select(memory_index_outbox)
                    .where(
                        memory_index_outbox.c.available_at <= func.now(),
                        memory_index_outbox.c.attempts
                        < app_config.memory.outbox_max_attempts,
                    )
                    .order_by(memory_index_outbox.c.updated_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return []
        # 步骤二：为本批生成不可复用的领取代次令牌。仅靠 (uid, target, operation,
        # projection_version) 甚至再加 attempts 都无法区分"同一四元组被重新写入并被
        # 另一个 worker 重新领取"的新一代期望：迟到 worker 的结算会命中新行，既可能
        # 把新内容推进死信，也会缩短新领取者的租约造成重复处理。
        lease_token = uuid4().hex
        items = [
            MemoryOutboxItem(
                memory_uid=str(row["memory_uid"]),
                target=MemoryIndexTarget(str(row["target"])),
                operation=MemoryIndexOperation(str(row["operation"])),
                projection_version=str(row["projection_version"]),
                attempts=int(row["attempts"]),
                lease_token=lease_token,
            )
            for row in rows
        ]
        # 步骤三：把租约与代次令牌一并写入已领取行，使调用方能在提交后释放行锁再
        # 执行外部写入；租约到期而未确认的行会自动重新可领取，无需额外恢复通道。
        await self._session.execute(
            update(memory_index_outbox)
            .where(
                tuple_(
                    memory_index_outbox.c.memory_uid,
                    memory_index_outbox.c.target,
                ).in_([(item.memory_uid, item.target.value) for item in items])
            )
            .values(
                available_at=func.timestampadd(
                    text("SECOND"),
                    app_config.memory.outbox_claim_lease_seconds,
                    func.now(),
                ),
                lease_token=lease_token,
            )
        )
        return items

    async def renew_claim(self, item: MemoryOutboxItem) -> bool:
        """在开始处理该项前把它的领取租约续到当前时刻之后。

        领取时整批共用同一个到期时间，而处理是逐项顺序进行的：批次靠前的项目一旦
        累计耗时超过租约，尚未开始处理的尾部行就会重新变为可领取，后续 cron 会与本
        dispatcher 并发对同一行调用 TEI/ES/Qdrant，造成重复调用与负载放大。逐项续租
        使每一行的租约覆盖它自己的处理窗口。

        续租按领取代次令牌匹配：令牌已变说明该行已被覆盖或被其他 worker 重新领取，
        本 dispatcher 必须放弃它，不得再执行外部写入。

        Args:
            item: 即将处理的期望状态。

        Returns:
            是否仍持有该行的领取代次。
        """
        # 步骤一：按代次令牌续租；令牌不匹配时不更新任何行。
        result = await self._session.execute(
            update(memory_index_outbox)
            .where(
                memory_index_outbox.c.memory_uid == item.memory_uid,
                memory_index_outbox.c.target == item.target.value,
                memory_index_outbox.c.lease_token == item.lease_token,
            )
            .values(
                available_at=func.timestampadd(
                    text("SECOND"),
                    app_config.memory.outbox_claim_lease_seconds,
                    func.now(),
                )
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def dead_letter_count(self) -> int:
        """统计已达死信阈值、不再参与领取的期望状态行数。"""
        # 步骤一：只做计数，供调度器暴露需要人工介入的积压规模。
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(memory_index_outbox)
                    .where(
                        memory_index_outbox.c.attempts
                        >= app_config.memory.outbox_max_attempts
                    )
                )
            ).scalar_one()
        )

    async def acknowledge_outbox(
        self,
        item: MemoryOutboxItem,
        *,
        content_hash: str | None,
    ) -> bool:
        """仅在派生索引已与权威内容一致时确认 outbox 行。

        外部写入发生在事务之外，期间权威内容可能再次变更并写入新的期望状态。
        因为新旧期望的 (uid, target, operation, projection_version) 完全相同，
        只按期望条件确认会删除这份新期望，使刚写入的陈旧内容永久留在派生索引。
        因此确认额外要求权威行仍与本次实际写入的内容一致：不一致时保留期望状态，
        该行会在领取租约到期后（或写入方重置可用时间后）被重新处理。

        Args:
            item: 已处理的期望状态。
            content_hash: 本次写入派生索引的权威内容哈希；删除派生文档时为 None。

        Returns:
            是否确认了该期望状态。
        """
        # 步骤一：按本次实际执行的动作构造权威一致性条件。写入路径要求权威行仍为
        # 同一内容且仍处于 ACTIVE；删除路径要求权威行确实不再是可检索的 ACTIVE 行。
        active_row = select(agent_memory.c.uid).where(
            agent_memory.c.uid == item.memory_uid,
            agent_memory.c.status == MemoryStatus.ACTIVE.value,
        )
        if content_hash is None:
            consistent = ~exists(active_row)
        else:
            consistent = exists(
                active_row.where(agent_memory.c.content_hash == content_hash)
            )
        # 步骤二：期望条件与权威一致性同时满足才删除，避免迟到 worker 既删除后来
        # 覆盖的新状态，又留下与权威内容不一致的派生文档。
        result = await self._session.execute(
            delete(memory_index_outbox).where(
                memory_index_outbox.c.memory_uid == item.memory_uid,
                memory_index_outbox.c.target == item.target.value,
                memory_index_outbox.c.operation == item.operation.value,
                memory_index_outbox.c.projection_version == item.projection_version,
                memory_index_outbox.c.lease_token == item.lease_token,
                consistent,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def enqueue_convergence(
        self,
        memory_uid: str,
        target: MemoryIndexTarget,
    ) -> None:
        """确认失败后按当前权威状态为单个目标重建收敛请求。

        确认失败意味着本次写入已经与权威内容不一致：可能是内容再次变更，也可能是
        并发删除的 DELETE 期望已被另一个 worker 确认并删除了 outbox 行。后者若不
        重建请求，迟到写入的陈旧内容就会永久留在派生索引里，没有任何后续请求能
        纠正它。

        已存在 outbox 行时只纠正操作与投影版本，不重置 `attempts` 与
        `available_at`，避免覆盖那一行自己的退避进度与死信预算。

        Args:
            memory_uid: 需要重新收敛的权威记忆 UID。
            target: 需要重新收敛的派生索引目标。

        权威行已被物理清理时同样登记一条 DELETE 收敛请求：outbox 刻意不设向
        `agent_memory` 的外键，因此这条请求可以独立存在，由后续周期按普通 outbox
        项重试直至成功。这样补偿不再是一次性远程调用——瞬时索引故障或进程退出后
        仍有持久待办可重放，用户已删除的内容不会永久留在派生索引里。
        """
        # 步骤一：按当前权威状态派生目标操作；权威行已不存在时同样收敛为删除。
        status = (
            await self._session.execute(
                select(agent_memory.c.status).where(agent_memory.c.uid == memory_uid)
            )
        ).scalar_one_or_none()
        operation = (
            MemoryIndexOperation.UPSERT
            if status is not None and str(status) == MemoryStatus.ACTIVE.value
            else MemoryIndexOperation.DELETE
        )
        statement = insert(memory_index_outbox).values(
            {
                "memory_uid": memory_uid,
                "target": target.value,
                "operation": operation.value,
                "projection_version": app_config.memory.projection_version,
                "attempts": 0,
                "available_at": func.now(),
                "last_error_type": None,
            }
        )
        # 步骤二：行已被删除时新建收敛请求；行仍在时只纠正操作，保留其退避进度。
        await self._session.execute(
            statement.on_duplicate_key_update(
                operation=statement.inserted.operation,
                projection_version=statement.inserted.projection_version,
            )
        )

    async def retry_outbox(
        self,
        item: MemoryOutboxItem,
        error_type: str,
        max_backoff_seconds: int,
    ) -> None:
        """记录安全异常类型并指数退避。"""
        # 步骤一：根据已尝试次数计算有上限的指数退避时间。
        attempts = item.attempts + 1
        delay = min(2 ** min(attempts, 20), max_backoff_seconds)
        # 步骤二：只更新仍属于本次领取的那一代期望状态。代次令牌在每次领取时重新
        # 生成，`set_desired_state` 的覆盖与其他 worker 的重新领取都会让它改变，因此
        # 迟到 worker 的失败回写不会命中新一代——否则它既可能把新内容的期望直接推到
        # 死信上限使其再不被领取，也会把新领取者的租约缩短成一次退避间隔而造成重复处理。
        await self._session.execute(
            update(memory_index_outbox)
            .where(
                memory_index_outbox.c.memory_uid == item.memory_uid,
                memory_index_outbox.c.target == item.target.value,
                memory_index_outbox.c.operation == item.operation.value,
                memory_index_outbox.c.projection_version == item.projection_version,
                memory_index_outbox.c.lease_token == item.lease_token,
            )
            .values(
                attempts=attempts,
                available_at=func.timestampadd(
                    text("SECOND"),
                    delay,
                    func.now(),
                ),
                last_error_type=error_type[:128],
            )
        )

    async def projection(self, uid: str) -> MemoryProjection | None:
        """从权威内容构造共享索引投影。"""
        # 步骤一：按 UID 读取 MySQL 权威行，禁止从派生索引反向构造事实。
        row = (
            (
                await self._session.execute(
                    select(agent_memory).where(agent_memory.c.uid == uid)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        # 步骤二：严格解码类型化内容并构造两个索引共享的有界投影。
        content = MEMORY_CONTENT_ADAPTER.validate_python(row["content"])
        return MemoryProjection(
            memory_uid=str(row["uid"]),
            source=str(row["source"]),
            user_id=(str(row["user_id"]) if row["user_id"] is not None else None),
            created_conversation_uid=(
                str(row["created_conversation_uid"])
                if row["created_conversation_uid"] is not None
                else None
            ),
            created_message_uid=(
                str(row["created_message_uid"])
                if row["created_message_uid"] is not None
                else None
            ),
            category=str(row["category"]),
            memory_key=str(row["memory_key"]),
            content_schema=str(row["content_schema"]),
            schema_fingerprint=(
                str(row["schema_fingerprint"])
                if row["schema_fingerprint"] is not None
                else None
            ),
            memory_text=str(row["memory_text"]),
            content_hash=str(row["content_hash"]),
            object_ids=content_object_ids(content),
            trust=MemoryTrust(str(row["trust"])),
            status=MemoryStatus(str(row["status"])),
            importance_score=float(row["importance_score"]),
            lifecycle_policy=MemoryLifecyclePolicy(str(row["lifecycle_policy"])),
            expires_at=row["expires_at"],
            record_version=int(row["record_version"]),
            content_version=str(row["content_version"]),
            projection_version=str(row["projection_version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def scan_active(
        self,
        *,
        after_id: int,
        limit: int,
    ) -> list[RowMapping]:
        """按 MySQL 主键游标扫描活动记忆。"""
        # 步骤一：按排他主键游标读取有界 ACTIVE 行，供重建任务稳定续扫。
        return list(
            (
                await self._session.execute(
                    select(agent_memory.c.id, agent_memory.c.uid)
                    .where(
                        agent_memory.c.id > after_id,
                        agent_memory.c.status == MemoryStatus.ACTIVE.value,
                    )
                    .order_by(agent_memory.c.id)
                    .limit(limit)
                )
            ).mappings()
        )

    async def enqueue_rebuild(self, uids: set[str]) -> None:
        """为活动 UID 重新生成双目标 UPSERT 期望状态。"""
        if not uids:
            return
        # 步骤一：锁定复核仍处于 ACTIVE 的权威行。扫描阶段不持锁，期间可能有行被
        # 软删除或墓碑化；只有在本事务内锁定成功的 ACTIVE 行才允许重建 UPSERT
        # 期望，否则会把并发删除已提交的 DELETE 期望覆盖成 UPSERT。
        active_uids = {
            str(uid)
            for uid in (
                await self._session.execute(
                    select(agent_memory.c.uid)
                    .where(
                        agent_memory.c.uid.in_(uids),
                        agent_memory.c.status == MemoryStatus.ACTIVE.value,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        }
        if not active_uids:
            return
        # 步骤二：只推进已锁定 ACTIVE 行的投影版本。
        await self._session.execute(
            update(agent_memory)
            .where(agent_memory.c.uid.in_(active_uids))
            .values(projection_version=app_config.memory.projection_version)
        )
        # 步骤三：只为已锁定 ACTIVE 行重建两个目标的 UPSERT 期望。
        await self.set_desired_state(active_uids, MemoryIndexOperation.UPSERT)
