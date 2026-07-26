"""Mem0 风格权威记忆、历史、关联与索引 outbox 仓储。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import RowMapping, and_, func, or_, select, true, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.identifiers import memory_uid
from data_agent.memory.domain.lifecycle import (
    decide_memory,
    semantically_equivalent,
)
from data_agent.memory.domain.payloads import (
    build_memory_text,
    canonical_content_json,
    memory_content_hash,
)
from data_agent.memory.domain.policies import (
    category_policy,
    validate_candidate_policy,
)
from data_agent.memory.mysql.index_outbox import (
    MemoryIndexOutboxRepository,
)
from data_agent.memory.mysql.tables import (
    agent_memory,
    agent_memory_event,
    agent_memory_link,
    memory_index_outbox,
)
from data_agent.models.memory import (
    MEMORY_CONTENT_ADAPTER,
    MemoryActorType,
    MemoryCandidate,
    MemoryContent,
    MemoryDecision,
    MemoryDetail,
    MemoryEvent,
    MemoryEventType,
    MemoryHistoryPage,
    MemoryIndexOperation,
    MemoryLifecyclePolicy,
    MemoryLink,
    MemoryLinkType,
    MemoryStatus,
    MemoryTrust,
    MetricDefinitionContent,
)
from data_agent.settings import app_config


@dataclass(frozen=True)
class StoredMemory:
    """带内部主键的权威记忆。"""

    id: int
    detail: MemoryDetail

    @property
    def content(self) -> MemoryContent:
        """返回类型化权威内容。"""
        return self.detail.content


def _decode_content(value: object) -> MemoryContent:
    """解析数据库 JSON 为严格领域内容。"""
    return MEMORY_CONTENT_ADAPTER.validate_python(value)


def _active_key(candidate: MemoryCandidate) -> str:
    """生成数据库唯一活动槽键。"""
    identity = "\x1f".join(
        (
            candidate.source,
            candidate.user_id or "",
            candidate.category,
            candidate.memory_key,
        )
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _versioned_uid(base_uid: str, record_version: int) -> str:
    """为重新生效的退役内容生成不可冲突的版本标识。"""
    identity = "\x1f".join((base_uid, str(record_version)))
    return hashlib.sha256(identity.encode()).hexdigest()


def _merge_metric_content(
    active_content: MemoryContent,
    candidate_content: MemoryContent,
) -> MetricDefinitionContent | None:
    """按指标槽合并历史问答证据并保留候选指标定义。"""
    if not isinstance(active_content, MetricDefinitionContent):
        return None
    if not isinstance(candidate_content, MetricDefinitionContent):
        return None
    questions = {
        question.question_id: question for question in active_content.questions
    }
    questions.update(
        {question.question_id: question for question in candidate_content.questions}
    )
    answers = {answer.question_id: answer for answer in active_content.answers}
    answers.update({answer.question_id: answer for answer in candidate_content.answers})
    return candidate_content.model_copy(
        update={
            "questions": list(questions.values()),
            "answers": list(answers.values()),
        }
    )


def _apply_merged_content(
    candidate: MemoryCandidate, content: MetricDefinitionContent
) -> None:
    """把合并后的内容同步回候选的内容寻址字段和投影文本。"""
    content_json = canonical_content_json(content)
    candidate.content = content
    candidate.content_hash = memory_content_hash(content)
    candidate.memory_text = build_memory_text(content)
    candidate.uid = memory_uid(
        candidate.source,
        candidate.category,
        candidate.memory_key,
        candidate.schema_fingerprint or "",
        content_json,
    )


def _event_type_for_decision(decision: MemoryDecision | None) -> MemoryEventType:
    """映射新版本的审计事件类型。"""
    if decision == MemoryDecision.UPDATE:
        return MemoryEventType.UPDATE
    if decision == MemoryDecision.MERGE:
        return MemoryEventType.MERGE
    return MemoryEventType.ADD


def _parse_detail(
    row: RowMapping, links: list[MemoryLink] | None = None
) -> MemoryDetail:
    """把权威数据库行转换为公开详情。"""
    return MemoryDetail(
        uid=str(row["uid"]),
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
        content=_decode_content(row["content"]),
        content_hash=str(row["content_hash"]),
        trust=MemoryTrust(str(row["trust"])),
        status=MemoryStatus(str(row["status"])),
        importance_score=float(row["importance_score"]),
        lifecycle_policy=MemoryLifecyclePolicy(str(row["lifecycle_policy"])),
        expires_at=row["expires_at"],
        record_version=int(row["record_version"]),
        access_count=int(row["access_count"]),
        last_accessed_at=row["last_accessed_at"],
        content_version=str(row["content_version"]),
        projection_version=str(row["projection_version"]),
        created_job_id=(
            str(row["created_job_id"]) if row["created_job_id"] is not None else None
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
        purge_requested_at=row["purge_requested_at"],
        links=links or [],
    )


class MemoryRepository:
    """在调用方事务中管理唯一权威记忆及可重建投影期望。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由调用方管理事务边界的 Session。"""
        self._session = session
        self._outbox = MemoryIndexOutboxRepository(session)

    async def upsert_candidates(self, candidates: list[MemoryCandidate]) -> None:
        """幂等写入已接受事实、历史、关联和双目标 outbox。"""
        if not candidates:
            return
        # 步骤一：统一执行类别策略并校验批次活动槽，
        # 避免同一事务内的候选彼此争用唯一 ACTIVE 位置。
        for candidate in candidates:
            validate_candidate_policy(candidate)
        active_keys = [_active_key(candidate) for candidate in candidates]
        if len(active_keys) != len(set(active_keys)):
            raise ValueError("同一批次不能包含重复的活动记忆槽")

        # 步骤二：识别不可复活的 DELETED UID tombstone；
        # 重放相同内容只能审计为 NOOP，并继续维持删除投影。
        candidate_uids = {candidate.uid for candidate in candidates}
        existing_rows = (
            await self._session.execute(
                select(agent_memory.c.uid, agent_memory.c.status).where(
                    agent_memory.c.uid.in_(candidate_uids),
                    or_(
                        agent_memory.c.user_id.is_(None),
                        agent_memory.c.user_id.in_(
                            {
                                candidate.user_id
                                for candidate in candidates
                                if candidate.user_id is not None
                            }
                        ),
                    ),
                )
            )
        ).all()
        existing_status = {
            str(uid): MemoryStatus(str(status)) for uid, status in existing_rows
        }
        deleted_uids = {
            uid
            for uid, status in existing_status.items()
            if status == MemoryStatus.DELETED
        }
        for candidate in candidates:
            if candidate.uid in deleted_uids:
                candidate.decision = MemoryDecision.NOOP
                candidate.supersedes_uids = []
        accepted_candidates = [
            candidate for candidate in candidates if candidate.uid not in deleted_uids
        ]
        deleted_rows: dict[int, tuple[MemoryCandidate, RowMapping]] = {}
        noop_rows: list[tuple[MemoryCandidate, RowMapping]] = []
        record_versions: dict[str, int] = {}
        versioned_uids: dict[str, str] = {}
        scope_deleted_uids: set[str] = set()
        for candidate in accepted_candidates:
            # 步骤三：锁定同一逻辑事实的全部版本后再决策，
            # 确保生命周期比较和 ACTIVE 唯一槽基于同一权威快照。
            base_uid = candidate.uid
            active_key = _active_key(candidate)
            scope_rows = list(
                (
                    await self._session.execute(
                        select(
                            agent_memory.c.id,
                            agent_memory.c.uid,
                            agent_memory.c.active_key,
                            agent_memory.c.content,
                            agent_memory.c.content_hash,
                            agent_memory.c.record_version,
                            agent_memory.c.status,
                        )
                        .where(
                            agent_memory.c.source == candidate.source,
                            (
                                agent_memory.c.user_id.is_(None)
                                if candidate.user_id is None
                                else agent_memory.c.user_id == candidate.user_id
                            ),
                            agent_memory.c.category == candidate.category,
                            agent_memory.c.memory_key == candidate.memory_key,
                        )
                        .with_for_update()
                    )
                ).mappings()
            )
            active_rows = [
                row for row in scope_rows if str(row["active_key"]) == active_key
            ]
            deleted_same_content_uids = {
                str(row["uid"])
                for row in scope_rows
                if MemoryStatus(str(row["status"])) == MemoryStatus.DELETED
                and str(row["content_hash"]) == candidate.content_hash
            }
            if deleted_same_content_uids:
                scope_deleted_uids.update(deleted_same_content_uids)
                candidate.decision = MemoryDecision.NOOP
                candidate.supersedes_uids = []
                continue
            active_uids = {str(row["uid"]) for row in active_rows}
            same_content = any(
                str(row["content_hash"]) == candidate.content_hash
                for row in active_rows
            )
            same_meaning = any(
                semantically_equivalent(
                    _decode_content(row["content"]),
                    candidate.content,
                )
                for row in active_rows
            )
            if candidate.decision == MemoryDecision.DELETE:
                candidate.decision = decide_memory(
                    has_active=bool(active_rows),
                    delete_requested=True,
                )
            elif same_content or (
                candidate.decision not in {MemoryDecision.UPDATE, MemoryDecision.MERGE}
                and same_meaning
            ):
                candidate.decision = MemoryDecision.NOOP
            elif not active_rows:
                candidate.decision = MemoryDecision.ADD
            elif candidate.decision is None:
                candidate.decision = decide_memory(
                    has_active=True,
                    merge_requested=(
                        category_policy(candidate.category).conflict_strategy == "merge"
                    ),
                )
            if candidate.decision == MemoryDecision.DELETE:
                deleted_rows.update(
                    {int(row["id"]): (candidate, row) for row in active_rows}
                )
                candidate.supersedes_uids = []
                continue
            if candidate.decision == MemoryDecision.NOOP:
                if active_rows:
                    noop_rows.append((candidate, active_rows[0]))
                candidate.supersedes_uids = []
                continue
            if candidate.decision in {MemoryDecision.UPDATE, MemoryDecision.MERGE}:
                if candidate.decision == MemoryDecision.MERGE and active_rows:
                    merged_content = _merge_metric_content(
                        _decode_content(active_rows[0]["content"]),
                        candidate.content,
                    )
                    if merged_content is not None:
                        _apply_merged_content(candidate, merged_content)
                        merged_same_content_row = next(
                            (
                                row
                                for row in active_rows
                                if str(row["content_hash"]) == candidate.content_hash
                            ),
                            None,
                        )
                        if merged_same_content_row is not None:
                            candidate.decision = MemoryDecision.NOOP
                            candidate.supersedes_uids = []
                            noop_rows.append((candidate, merged_same_content_row))
                            continue
                candidate.supersedes_uids = sorted(
                    {*candidate.supersedes_uids, *active_uids}
                )
            else:
                candidate.supersedes_uids = []

            # 步骤四：历史内容再次成为当前事实时创建新 UID；
            # 先腾空旧活动槽，稍后再写入新权威版本。
            record_version = 1 + max(
                (int(row["record_version"]) for row in scope_rows),
                default=0,
            )
            if existing_status.get(base_uid) in {
                MemoryStatus.SUPERSEDED,
                MemoryStatus.EXPIRED,
            }:
                candidate.uid = _versioned_uid(base_uid, record_version)
                versioned_uids[base_uid] = candidate.uid
            record_versions[candidate.uid] = record_version
            if candidate.supersedes_uids:
                await self._session.execute(
                    update(agent_memory)
                    .where(agent_memory.c.uid.in_(candidate.supersedes_uids))
                    .values(active_key=None)
                )

        # 步骤五：改写同批候选对刚版本化事实的引用，
        # 所有关联必须改写到新 UID 后再做目标存在性校验。
        for candidate in accepted_candidates:
            candidate.derived_from_uids = [
                versioned_uids.get(uid, uid) for uid in candidate.derived_from_uids
            ]
            candidate.related_uids = [
                versioned_uids.get(uid, uid) for uid in candidate.related_uids
            ]

        accepted_candidates = [
            candidate
            for candidate in accepted_candidates
            if candidate.decision not in {MemoryDecision.DELETE, MemoryDecision.NOOP}
        ]

        # 步骤六：为 NOOP 和 DELETE 写入权威历史，并让 DELETE 撤销活动槽及双索引期望。
        if noop_rows:
            await self._session.execute(
                insert(agent_memory_event).values(
                    [
                        {
                            "memory_id": int(row["id"]),
                            "event_type": MemoryEventType.NOOP.value,
                            "old_content": row["content"],
                            "new_content": row["content"],
                            "job_id": candidate.created_job_id,
                            "actor_type": candidate.actor_type.value,
                        }
                        for candidate, row in noop_rows
                    ]
                )
            )

        if deleted_rows:
            await self._session.execute(
                update(agent_memory)
                .where(
                    agent_memory.c.id.in_(deleted_rows),
                    agent_memory.c.status == MemoryStatus.ACTIVE.value,
                )
                .values(
                    status=MemoryStatus.DELETED.value,
                    active_key=None,
                    deleted_at=func.now(),
                )
            )
            await self._session.execute(
                insert(agent_memory_event).values(
                    [
                        {
                            "memory_id": identifier,
                            "event_type": MemoryEventType.DELETE.value,
                            "old_content": row["content"],
                            "new_content": None,
                            "job_id": None,
                            "actor_type": candidate.actor_type.value,
                        }
                        for identifier, (candidate, row) in deleted_rows.items()
                    ]
                )
            )
            await self._outbox.set_desired_state(
                {str(row["uid"]) for _, row in deleted_rows.values()},
                MemoryIndexOperation.DELETE,
            )

        # 步骤七：仅将产生新版本的候选写入权威表，避免原地改写既有内容。
        if accepted_candidates:
            statement = insert(agent_memory).values(
                [
                    {
                        "uid": candidate.uid,
                        "source": candidate.source,
                        "user_id": candidate.user_id,
                        "category": candidate.category,
                        "memory_key": candidate.memory_key,
                        "active_key": _active_key(candidate),
                        "content_schema": candidate.content_schema,
                        "schema_fingerprint": candidate.schema_fingerprint,
                        "memory_text": candidate.memory_text,
                        "content": candidate.content.model_dump(mode="json"),
                        "content_hash": candidate.content_hash,
                        "trust": candidate.trust.value,
                        "status": MemoryStatus.ACTIVE.value,
                        "importance_score": candidate.importance_score,
                        "lifecycle_policy": candidate.lifecycle_policy.value,
                        "expires_at": candidate.expires_at,
                        "record_version": record_versions[candidate.uid],
                        "access_count": 0,
                        "last_accessed_at": None,
                        "content_version": candidate.content_version,
                        "projection_version": candidate.projection_version,
                        "created_job_id": candidate.created_job_id,
                        "created_conversation_uid": (
                            candidate.created_conversation_uid
                        ),
                        "created_message_uid": candidate.created_message_uid,
                        "deleted_at": None,
                        "purge_requested_at": None,
                    }
                    for candidate in accepted_candidates
                ]
            )
            await self._session.execute(statement)

        # 步骤八：从权威表解析全部关联端点，缺失目标时回滚以避免悬空关系。
        all_uids = {
            uid
            for candidate in accepted_candidates
            for uid in (
                candidate.uid,
                *candidate.derived_from_uids,
                *candidate.related_uids,
                *candidate.supersedes_uids,
            )
        }
        uid_rows = (
            await self._session.execute(
                select(agent_memory.c.uid, agent_memory.c.id).where(
                    agent_memory.c.uid.in_(all_uids)
                )
            )
        ).all()
        uid_to_id = {str(uid): int(identifier) for uid, identifier in uid_rows}
        missing = all_uids - set(uid_to_id)
        if missing:
            raise ValueError(f"记忆关联目标不存在: {','.join(sorted(missing))}")

        # 步骤九：先为新权威版本追加事件，再建立三类记忆关系。
        new_candidates = [
            candidate
            for candidate in accepted_candidates
            if candidate.uid not in existing_status
        ]
        if new_candidates:
            await self._session.execute(
                insert(agent_memory_event).values(
                    [
                        {
                            "memory_id": uid_to_id[candidate.uid],
                            "event_type": _event_type_for_decision(
                                candidate.decision
                            ).value,
                            "old_content": None,
                            "new_content": candidate.content.model_dump(mode="json"),
                            "job_id": candidate.created_job_id,
                            "actor_type": candidate.actor_type.value,
                        }
                        for candidate in new_candidates
                    ]
                )
            )

        links = [
            {
                "memory_id": uid_to_id[candidate.uid],
                "linked_memory_id": uid_to_id[linked_uid],
                "link_type": link_type.value,
            }
            for candidate in accepted_candidates
            for link_type, linked_uids in (
                (MemoryLinkType.DERIVED_FROM, candidate.derived_from_uids),
                (MemoryLinkType.RELATED, candidate.related_uids),
                (MemoryLinkType.SUPERSEDES, candidate.supersedes_uids),
            )
            for linked_uid in linked_uids
        ]
        if links:
            link_statement = insert(agent_memory_link).values(links)
            await self._session.execute(
                link_statement.on_duplicate_key_update(
                    link_type=link_statement.inserted.link_type
                )
            )

        # 步骤十：替代关系落库后再把已腾空活动槽的旧版本标记为 SUPERSEDED，
        # 并要求两个派生索引删除旧 UID。
        superseded = {
            uid
            for candidate in accepted_candidates
            for uid in candidate.supersedes_uids
        }
        if superseded:
            replacement_by_uid = {
                old_uid: candidate
                for candidate in accepted_candidates
                for old_uid in candidate.supersedes_uids
            }
            superseded_filters = [
                and_(
                    agent_memory.c.uid == uid,
                    (
                        agent_memory.c.user_id.is_(None)
                        if candidate.user_id is None
                        else agent_memory.c.user_id == candidate.user_id
                    ),
                )
                for uid, candidate in replacement_by_uid.items()
            ]
            superseded_rows = (
                await self._session.execute(
                    select(
                        agent_memory.c.id,
                        agent_memory.c.uid,
                        agent_memory.c.content,
                    ).where(
                        or_(*superseded_filters),
                        agent_memory.c.status == MemoryStatus.ACTIVE.value,
                    )
                )
            ).mappings()
            history_values = [
                {
                    "memory_id": int(row["id"]),
                    "event_type": MemoryEventType.UPDATE.value,
                    "old_content": row["content"],
                    "new_content": replacement_by_uid[
                        str(row["uid"])
                    ].content.model_dump(mode="json"),
                    "job_id": replacement_by_uid[str(row["uid"])].created_job_id,
                    "actor_type": replacement_by_uid[str(row["uid"])].actor_type.value,
                }
                for row in superseded_rows
            ]
            if history_values:
                await self._session.execute(
                    insert(agent_memory_event).values(history_values)
                )
            await self._session.execute(
                update(agent_memory)
                .where(
                    or_(*superseded_filters),
                    agent_memory.c.status == MemoryStatus.ACTIVE.value,
                )
                .values(
                    status=MemoryStatus.SUPERSEDED.value,
                    active_key=None,
                    deleted_at=None,
                )
            )
            await self._outbox.set_desired_state(
                superseded,
                MemoryIndexOperation.DELETE,
            )

        # 步骤十一：写入新版本 UPSERT，并维持重放 tombstone 与作用域 tombstone 的
        # DELETE 期望；旧替代版本的 DELETE 已在状态终结后写入同一调用方事务。
        await self._outbox.set_desired_state(
            {candidate.uid for candidate in accepted_candidates},
            MemoryIndexOperation.UPSERT,
        )
        await self._outbox.set_desired_state(
            deleted_uids | scope_deleted_uids,
            MemoryIndexOperation.DELETE,
        )

    async def find_compatible_scopes(
        self,
        source: str,
        scope_fingerprints: dict[str, str],
        category: str,
        content_version: str,
        *,
        per_scope_limit: int = 20,
    ) -> dict[str, list[StoredMemory]]:
        """批量读取一组精确作用域的活动权威记忆。"""
        if not scope_fingerprints:
            return {}
        # 步骤一：把每个逻辑作用域与其结构指纹绑定为精确匹配条件。
        conditions = [
            and_(
                agent_memory.c.memory_key == scope,
                agent_memory.c.schema_fingerprint == fingerprint,
            )
            for scope, fingerprint in scope_fingerprints.items()
        ]
        # 步骤二：按版本和有效期读取有界的 ACTIVE 权威行。
        rows = (
            await self._session.execute(
                select(agent_memory)
                .where(
                    agent_memory.c.source == source,
                    agent_memory.c.user_id.is_(None),
                    agent_memory.c.category == category,
                    agent_memory.c.status == MemoryStatus.ACTIVE.value,
                    or_(
                        agent_memory.c.expires_at.is_(None),
                        agent_memory.c.expires_at > func.now(),
                    ),
                    agent_memory.c.content_version == content_version,
                    or_(*conditions),
                )
                .order_by(agent_memory.c.updated_at.desc(), agent_memory.c.id.desc())
                .limit(len(conditions) * per_scope_limit)
            )
        ).mappings()
        # 步骤三：按作用域分组，并再次执行单作用域数量上限。
        result = {scope: [] for scope in scope_fingerprints}
        for row in rows:
            scope = str(row["memory_key"])
            if len(result[scope]) < per_scope_limit:
                result[scope].append(StoredMemory(int(row["id"]), _parse_detail(row)))
        return result

    async def find_active_by_fingerprint(
        self,
        source: str,
        schema_fingerprint: str,
        categories: set[str],
        content_version: str,
        *,
        limit: int = 500,
    ) -> list[StoredMemory]:
        """读取完整模式指纹下的兼容活动记忆。"""
        # 步骤一：按来源、完整指纹、类别、版本和有效期读取有界权威结果。
        rows = (
            await self._session.execute(
                select(agent_memory)
                .where(
                    agent_memory.c.source == source,
                    agent_memory.c.user_id.is_(None),
                    agent_memory.c.schema_fingerprint == schema_fingerprint,
                    agent_memory.c.category.in_(categories),
                    agent_memory.c.status == MemoryStatus.ACTIVE.value,
                    or_(
                        agent_memory.c.expires_at.is_(None),
                        agent_memory.c.expires_at > func.now(),
                    ),
                    agent_memory.c.content_version == content_version,
                )
                .order_by(agent_memory.c.updated_at.desc(), agent_memory.c.id.desc())
                .limit(limit)
            )
        ).mappings()
        return [StoredMemory(int(row["id"]), _parse_detail(row)) for row in rows]

    async def linked_uids(
        self,
        memory_ids: set[int],
        link_type: MemoryLinkType,
    ) -> dict[int, set[str]]:
        """批量读取一组记忆的指定出向关联目标 UID。"""
        if not memory_ids:
            return {}
        # 步骤一：连接关联表与目标权威行，批量读取指定类型的出向关系。
        linked = agent_memory.alias("linked")
        rows = (
            await self._session.execute(
                select(agent_memory_link.c.memory_id, linked.c.uid)
                .select_from(
                    agent_memory_link.join(
                        linked,
                        linked.c.id == agent_memory_link.c.linked_memory_id,
                    )
                )
                .where(
                    agent_memory_link.c.memory_id.in_(memory_ids),
                    agent_memory_link.c.link_type == link_type.value,
                )
            )
        ).all()
        # 步骤二：按源记忆主键聚合目标 UID，并为无关联输入保留空集合。
        result = {memory_id: set() for memory_id in memory_ids}
        for memory_id, uid in rows:
            result[int(memory_id)].add(str(uid))
        return result

    async def get_by_uid(
        self,
        uid: str,
        *,
        user_id: str | None = None,
        for_update: bool = False,
    ) -> StoredMemory | None:
        """读取一条权威记忆及有界关联。"""
        # 步骤一：在租户边界内读取权威行，并按调用方要求获取更新锁。
        statement = select(agent_memory).where(
            agent_memory.c.uid == uid,
            (
                agent_memory.c.user_id.is_(None)
                if user_id is None
                else agent_memory.c.user_id == user_id
            ),
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).mappings().one_or_none()
        # 步骤二：权威行不存在时立即返回，避免继续读取无归属关联。
        if row is None:
            return None
        # 步骤三：在同一租户边界内有界读取该记忆的双向关联。
        identifier = int(row["id"])
        source_memory = agent_memory.alias("source_memory")
        linked_memory = agent_memory.alias("linked_memory")
        link_rows = (
            await self._session.execute(
                select(
                    source_memory.c.uid.label("source_uid"),
                    linked_memory.c.uid.label("linked_uid"),
                    agent_memory_link.c.link_type,
                )
                .select_from(
                    agent_memory_link.join(
                        source_memory,
                        source_memory.c.id == agent_memory_link.c.memory_id,
                    ).join(
                        linked_memory,
                        linked_memory.c.id == agent_memory_link.c.linked_memory_id,
                    )
                )
                .where(
                    or_(
                        agent_memory_link.c.memory_id == identifier,
                        agent_memory_link.c.linked_memory_id == identifier,
                    ),
                    (
                        and_(
                            source_memory.c.user_id.is_(None),
                            linked_memory.c.user_id.is_(None),
                        )
                        if user_id is None
                        else and_(
                            source_memory.c.user_id == user_id,
                            linked_memory.c.user_id == user_id,
                        )
                    ),
                )
                .limit(500)
            )
        ).mappings()
        links = [
            MemoryLink(
                link_type=MemoryLinkType(str(link["link_type"])),
                memory_uid=str(link["source_uid"]),
                linked_memory_uid=str(link["linked_uid"]),
            )
            for link in link_rows
        ]
        return StoredMemory(identifier, _parse_detail(row, links))

    async def get_many_active(
        self,
        uids: list[str],
        *,
        user_id: str | None = None,
    ) -> list[StoredMemory]:
        """批量回查活动权威内容，保持输入 UID 的稳定顺序。"""
        if not uids:
            return []
        # 步骤一：在租户、状态和有效期边界内批量回查权威行。
        rows = (
            await self._session.execute(
                select(agent_memory).where(
                    agent_memory.c.uid.in_(uids),
                    (
                        agent_memory.c.user_id.is_(None)
                        if user_id is None
                        else agent_memory.c.user_id == user_id
                    ),
                    agent_memory.c.status == MemoryStatus.ACTIVE.value,
                    or_(
                        agent_memory.c.expires_at.is_(None),
                        agent_memory.c.expires_at > func.now(),
                    ),
                )
            )
        ).mappings()
        # 步骤二：按输入 UID 顺序回排结果，并自然排除缺失或失效记录。
        by_uid = {
            str(row["uid"]): StoredMemory(int(row["id"]), _parse_detail(row))
            for row in rows
        }
        return [by_uid[uid] for uid in uids if uid in by_uid]

    async def find_exact_query(
        self,
        source: str,
        query: str,
        categories: set[str] | None,
        *,
        user_id: str | None = None,
        limit: int,
    ) -> list[str]:
        """以 scope key 或完整投影文本执行安全的 MySQL 精确基线检索。"""
        # 步骤一：构造来源、租户、状态、版本和精确文本的权威过滤条件。
        filters = [
            agent_memory.c.source == source,
            (
                agent_memory.c.user_id.is_(None)
                if user_id is None
                else agent_memory.c.user_id == user_id
            ),
            agent_memory.c.status == MemoryStatus.ACTIVE.value,
            or_(
                agent_memory.c.expires_at.is_(None),
                agent_memory.c.expires_at > func.now(),
            ),
            agent_memory.c.content_version == app_config.memory.content_version,
            or_(
                agent_memory.c.memory_key == query,
                agent_memory.c.memory_text == query,
            ),
        ]
        if categories:
            filters.append(agent_memory.c.category.in_(categories))
        # 步骤二：按最新更新时间读取有界 UID，作为派生索引不可用时的基线。
        return [
            str(uid)
            for uid in (
                await self._session.scalars(
                    select(agent_memory.c.uid)
                    .where(*filters)
                    .order_by(agent_memory.c.updated_at.desc())
                    .limit(limit)
                )
            ).all()
        ]

    async def latest_event_id(
        self,
        uid: str,
        *,
        user_id: str | None = None,
    ) -> int:
        """读取逻辑事实作用域内最新一条历史事件的编号。

        写入方需要回报"刚提交的事件编号"。按 `history` 分页回读再取末项只在事件
        总数不超过一页时才正确：历史按事件 id 升序分页，越过一页后末项是旧事件。
        这里直接取作用域内的最大事件 id，与事件总量无关。

        Args:
            uid: 本次写入的权威记忆 UID。
            user_id: 租户边界；DDL 记忆为 None。

        Returns:
            最新事件编号；作用域内没有可见事件时为 0。
        """
        # 步骤一：先解析当前租户可见的权威记忆，确定逻辑事实作用域。
        memory = await self.get_by_uid(uid, user_id=user_id)
        if memory is None:
            return 0
        # 步骤二：在同一作用域内取最大事件 id，避免依赖分页窗口。
        latest = (
            await self._session.execute(
                select(func.max(agent_memory_event.c.id))
                .select_from(
                    agent_memory_event.join(
                        agent_memory,
                        agent_memory.c.id == agent_memory_event.c.memory_id,
                    )
                )
                .where(
                    agent_memory.c.source == memory.detail.source,
                    (
                        agent_memory.c.user_id.is_(None)
                        if user_id is None
                        else agent_memory.c.user_id == user_id
                    ),
                    agent_memory.c.category == memory.detail.category,
                    agent_memory.c.memory_key == memory.detail.memory_key,
                )
            )
        ).scalar_one_or_none()
        # 步骤三：作用域内无事件时返回 0，与既有响应契约保持一致。
        return int(latest) if latest is not None else 0

    async def history(
        self,
        uid: str,
        *,
        user_id: str | None = None,
        offset: int,
        limit: int,
    ) -> MemoryHistoryPage | None:
        """读取有界只追加历史。"""
        # 步骤一：先解析当前租户可见的权威记忆，以确定逻辑事实作用域。
        memory = await self.get_by_uid(uid, user_id=user_id)
        if memory is None:
            return None
        # 步骤二：按逻辑事实作用域读取有界追加事件，并多取一条判断后续页。
        rows = list(
            (
                await self._session.execute(
                    select(
                        agent_memory_event,
                        agent_memory.c.uid.label("event_memory_uid"),
                    )
                    .select_from(
                        agent_memory_event.join(
                            agent_memory,
                            agent_memory.c.id == agent_memory_event.c.memory_id,
                        )
                    )
                    .where(
                        agent_memory.c.source == memory.detail.source,
                        (
                            agent_memory.c.user_id.is_(None)
                            if user_id is None
                            else agent_memory.c.user_id == user_id
                        ),
                        agent_memory.c.category == memory.detail.category,
                        agent_memory.c.memory_key == memory.detail.memory_key,
                    )
                    .order_by(agent_memory_event.c.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).mappings()
        )
        # 步骤三：将数据库事件解码为公开历史，并返回稳定分页元数据。
        events = [
            MemoryEvent(
                id=int(row["id"]),
                memory_uid=str(row["event_memory_uid"]),
                event_type=MemoryEventType(str(row["event_type"])),
                old_content=(
                    _decode_content(row["old_content"])
                    if row["old_content"] is not None
                    else None
                ),
                new_content=(
                    _decode_content(row["new_content"])
                    if row["new_content"] is not None
                    else None
                ),
                job_id=(str(row["job_id"]) if row["job_id"] is not None else None),
                actor_type=MemoryActorType(str(row["actor_type"])),
                created_at=row["created_at"],
            )
            for row in rows[:limit]
        ]
        return MemoryHistoryPage(
            items=events,
            offset=offset,
            limit=limit,
            has_more=len(rows) > limit,
        )

    async def record_access(
        self,
        uids: set[str],
        *,
        source: str,
        user_id: str | None,
    ) -> None:
        """单调记录真正进入结果集的活动记忆访问。"""
        if not uids:
            return
        # 步骤一：仅在来源、租户和 ACTIVE 边界内原子递增访问计数。
        await self._session.execute(
            update(agent_memory)
            .where(
                agent_memory.c.uid.in_(uids),
                agent_memory.c.source == source,
                (
                    agent_memory.c.user_id.is_(None)
                    if user_id is None
                    else agent_memory.c.user_id == user_id
                ),
                agent_memory.c.status == MemoryStatus.ACTIVE.value,
            )
            .values(
                access_count=agent_memory.c.access_count + 1,
                last_accessed_at=func.now(),
            )
        )

    async def expire_due(self, limit: int = 100) -> int:
        """批量过期到期记忆并投递派生索引删除。"""
        # 步骤一：以跳锁批次领取到期 ACTIVE 行，让并发任务不重复处理同一版本。
        rows = list(
            (
                await self._session.execute(
                    select(
                        agent_memory.c.id,
                        agent_memory.c.uid,
                        agent_memory.c.content,
                    )
                    .where(
                        agent_memory.c.status == MemoryStatus.ACTIVE.value,
                        agent_memory.c.expires_at.is_not(None),
                        agent_memory.c.expires_at <= func.now(),
                    )
                    .order_by(agent_memory.c.expires_at, agent_memory.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).mappings()
        )
        if not rows:
            return 0
        # 步骤二：复用共享生命周期写入，原子完成过期、审计和索引删除期望。
        return await self._expire_rows(rows)

    async def expire_fingerprint_bound(
        self,
        source: str,
        valid_fingerprints: set[str],
        *,
        memory_keys: set[str] | None = None,
        limit: int = 500,
    ) -> int:
        """失效指定作用域内与当前 DDL 指纹集合不再匹配的活动记忆。"""
        if memory_keys is not None and not memory_keys:
            return 0
        # 步骤一：只在本次提交的 source/可选 memory_key 范围内构造失配条件，
        # 不能越界清理未提交的 DDL 事实。
        fingerprint_filter = true()
        if valid_fingerprints:
            fingerprint_filter = or_(
                agent_memory.c.schema_fingerprint.is_(None),
                ~agent_memory.c.schema_fingerprint.in_(valid_fingerprints),
            )
        # 步骤二：跳锁领取有界的指纹失配 ACTIVE 行。
        rows = list(
            (
                await self._session.execute(
                    select(
                        agent_memory.c.id,
                        agent_memory.c.uid,
                        agent_memory.c.content,
                    )
                    .where(
                        agent_memory.c.source == source,
                        agent_memory.c.status == MemoryStatus.ACTIVE.value,
                        agent_memory.c.lifecycle_policy
                        == MemoryLifecyclePolicy.FINGERPRINT_BOUND.value,
                        *(
                            (agent_memory.c.memory_key.in_(memory_keys),)
                            if memory_keys is not None
                            else ()
                        ),
                        fingerprint_filter,
                    )
                    .order_by(agent_memory.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).mappings()
        )
        if not rows:
            return 0
        # 步骤三：复用共享生命周期写入，原子完成过期、审计和索引删除期望。
        return await self._expire_rows(rows)

    async def _expire_rows(self, rows: list[RowMapping]) -> int:
        """应用共享过期状态、历史和索引删除。"""
        identifiers = {int(row["id"]) for row in rows}
        uids = {str(row["uid"]) for row in rows}
        # 步骤一：先撤销权威 ACTIVE 槽并标记为 EXPIRED。
        await self._session.execute(
            update(agent_memory)
            .where(
                agent_memory.c.id.in_(identifiers),
                agent_memory.c.status == MemoryStatus.ACTIVE.value,
            )
            .values(status=MemoryStatus.EXPIRED.value, active_key=None)
        )
        # 步骤二：为每个过期版本追加系统审计事件。
        await self._session.execute(
            insert(agent_memory_event).values(
                [
                    {
                        "memory_id": int(row["id"]),
                        "event_type": MemoryEventType.EXPIRE.value,
                        "old_content": row["content"],
                        "new_content": None,
                        "job_id": None,
                        "actor_type": MemoryActorType.SYSTEM.value,
                    }
                    for row in rows
                ]
            )
        )
        # 步骤三：覆盖双目标 DELETE 期望，使状态、审计和投影在调用方事务中共同提交。
        await self._outbox.set_desired_state(uids, MemoryIndexOperation.DELETE)
        return len(rows)

    async def soft_delete(self, memory: StoredMemory) -> None:
        """软删除权威记忆并写历史及双目标 DELETE outbox。"""
        # 步骤一：已删除记录直接返回，保持重复请求幂等。
        if memory.detail.status == MemoryStatus.DELETED:
            return
        # 步骤二：先让 MySQL 权威行不可召回并释放 ACTIVE 唯一槽。
        await self._session.execute(
            update(agent_memory)
            .where(
                agent_memory.c.id == memory.id,
                (
                    agent_memory.c.user_id.is_(None)
                    if memory.detail.user_id is None
                    else agent_memory.c.user_id == memory.detail.user_id
                ),
            )
            .values(
                status=MemoryStatus.DELETED.value,
                active_key=None,
                deleted_at=func.now(),
            )
        )
        # 步骤三：追加用户删除事件，保留被删除内容的审计证据。
        await self._session.execute(
            insert(agent_memory_event).values(
                memory_id=memory.id,
                event_type=MemoryEventType.DELETE.value,
                old_content=memory.content.model_dump(mode="json"),
                new_content=None,
                job_id=None,
                actor_type=MemoryActorType.USER.value,
            )
        )
        # 步骤四：覆盖双目标 DELETE 期望，异步收敛两个可重建索引。
        await self._outbox.set_desired_state(
            {memory.detail.uid},
            MemoryIndexOperation.DELETE,
        )

    async def tombstone_user(self, user_id: str) -> None:
        """立即隐藏用户记忆并投递双目标删除期望。"""
        # 步骤一：锁定用户全部尚未进入 purge 的版本，
        # 阻止并发更新在整用户删除期间重新建立 ACTIVE 行。
        rows = (
            await self._session.execute(
                select(
                    agent_memory.c.id,
                    agent_memory.c.uid,
                    agent_memory.c.content,
                    agent_memory.c.status,
                )
                .where(
                    agent_memory.c.user_id == user_id,
                    agent_memory.c.purge_requested_at.is_(None),
                )
                .with_for_update()
            )
        ).mappings()
        values = list(rows)
        if not values:
            return
        identifiers = {int(row["id"]) for row in values}
        uids = {str(row["uid"]) for row in values}
        # 步骤二：tombstone 立即切断召回并标记物理清理请求，
        # 但实体仍保留到两个派生目标均确认 DELETE。
        await self._session.execute(
            update(agent_memory)
            .where(
                agent_memory.c.id.in_(identifiers),
                agent_memory.c.user_id == user_id,
            )
            .values(
                status=MemoryStatus.DELETED.value,
                active_key=None,
                deleted_at=func.now(),
                purge_requested_at=func.now(),
            )
        )
        # 步骤三：仅为原 ACTIVE 版本新增删除事件；
        # 已终结版本也需进入 outbox/purge，但不重复伪造生命周期事件。
        active = [
            row for row in values if str(row["status"]) == MemoryStatus.ACTIVE.value
        ]
        if active:
            await self._session.execute(
                insert(agent_memory_event).values(
                    [
                        {
                            "memory_id": int(row["id"]),
                            "event_type": MemoryEventType.DELETE.value,
                            "old_content": row["content"],
                            "new_content": None,
                            "job_id": None,
                            "actor_type": MemoryActorType.SYSTEM.value,
                        }
                        for row in active
                    ]
                )
            )
        # 步骤四：为全部版本覆盖双目标 DELETE 期望，作为物理清理前置门禁。
        await self._outbox.set_desired_state(uids, MemoryIndexOperation.DELETE)

    async def purge_ready_user_memories(self, limit: int = 100) -> int:
        """两个派生目标均确认删除后物理清理用户级 tombstone。"""
        # 步骤一：只领取 outbox 已清空的 UID，确认两个目标均完成删除；
        # 跳锁领取使多个 purge worker 可安全并行。
        rows = list(
            (
                await self._session.execute(
                    select(agent_memory.c.id, agent_memory.c.uid)
                    .where(
                        agent_memory.c.purge_requested_at.is_not(None),
                        ~agent_memory.c.uid.in_(
                            select(memory_index_outbox.c.memory_uid)
                        ),
                    )
                    .order_by(agent_memory.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).mappings()
        )
        if not rows:
            return 0
        identifiers = {int(row["id"]) for row in rows}
        # 步骤二：先移除双向关联和追加事件，最后删除权威实体以满足外键顺序。
        await self._session.execute(
            agent_memory_link.delete().where(
                or_(
                    agent_memory_link.c.memory_id.in_(identifiers),
                    agent_memory_link.c.linked_memory_id.in_(identifiers),
                )
            )
        )
        await self._session.execute(
            agent_memory_event.delete().where(
                agent_memory_event.c.memory_id.in_(identifiers)
            )
        )
        await self._session.execute(
            agent_memory.delete().where(agent_memory.c.id.in_(identifiers))
        )
        return len(rows)
