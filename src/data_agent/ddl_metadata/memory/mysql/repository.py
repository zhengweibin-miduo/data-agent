"""Mem0 风格权威记忆、历史、关联与索引 outbox 仓储。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import RowMapping, and_, func, or_, select, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.ddl_metadata.memory.mysql.index_outbox import (
    MemoryIndexOutboxRepository,
)
from data_agent.ddl_metadata.memory.mysql.tables import (
    agent_memory,
    agent_memory_event,
    agent_memory_link,
)
from data_agent.ddl_metadata.models.memory import (
    MEMORY_CONTENT_ADAPTER,
    MemoryActorType,
    MemoryCandidate,
    MemoryContent,
    MemoryDetail,
    MemoryEvent,
    MemoryEventType,
    MemoryHistoryPage,
    MemoryIndexOperation,
    MemoryKind,
    MemoryLink,
    MemoryLinkType,
    MemoryStatus,
    MemoryTrust,
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


def _parse_detail(
    row: RowMapping, links: list[MemoryLink] | None = None
) -> MemoryDetail:
    """把权威数据库行转换为公开详情。"""
    return MemoryDetail(
        uid=str(row["uid"]),
        source=str(row["source"]),
        kind=MemoryKind(str(row["kind"])),
        scope_key=str(row["scope_key"]),
        schema_fingerprint=str(row["schema_fingerprint"]),
        memory_text=str(row["memory_text"]),
        content=_decode_content(row["content"]),
        content_hash=str(row["content_hash"]),
        trust=MemoryTrust(str(row["trust"])),
        status=MemoryStatus(str(row["status"])),
        content_version=str(row["content_version"]),
        projection_version=str(row["projection_version"]),
        created_job_id=str(row["created_job_id"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
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
        candidate_uids = {candidate.uid for candidate in candidates}
        existing_rows = (
            await self._session.execute(
                select(agent_memory.c.uid, agent_memory.c.status).where(
                    agent_memory.c.uid.in_(candidate_uids)
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
        accepted_candidates = [
            candidate for candidate in candidates if candidate.uid not in deleted_uids
        ]
        for candidate in accepted_candidates:
            if candidate.kind not in {
                MemoryKind.SEMANTIC_DECISION,
                MemoryKind.METRIC_DEFINITION,
            }:
                continue
            older = (
                await self._session.scalars(
                    select(agent_memory.c.uid).where(
                        agent_memory.c.source == candidate.source,
                        agent_memory.c.kind == candidate.kind.value,
                        agent_memory.c.scope_key == candidate.scope_key,
                        agent_memory.c.status == MemoryStatus.ACTIVE.value,
                        agent_memory.c.uid != candidate.uid,
                    )
                )
            ).all()
            candidate.supersedes_uids = sorted(
                {*candidate.supersedes_uids, *(str(uid) for uid in older)}
            )

        if accepted_candidates:
            statement = insert(agent_memory).values(
                [
                    {
                        "uid": candidate.uid,
                        "source": candidate.source,
                        "kind": candidate.kind.value,
                        "scope_key": candidate.scope_key,
                        "schema_fingerprint": candidate.schema_fingerprint,
                        "memory_text": candidate.memory_text,
                        "content": candidate.content.model_dump(mode="json"),
                        "content_hash": candidate.content_hash,
                        "trust": candidate.trust.value,
                        "status": MemoryStatus.ACTIVE.value,
                        "content_version": candidate.content_version,
                        "projection_version": candidate.projection_version,
                        "created_job_id": candidate.created_job_id,
                        "deleted_at": None,
                    }
                    for candidate in accepted_candidates
                ]
            )
            await self._session.execute(
                statement.on_duplicate_key_update(
                    memory_text=statement.inserted.memory_text,
                    content=statement.inserted.content,
                    content_hash=statement.inserted.content_hash,
                    trust=statement.inserted.trust,
                    content_version=statement.inserted.content_version,
                    projection_version=statement.inserted.projection_version,
                )
            )
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
                            "event_type": MemoryEventType.ADD.value,
                            "old_content": None,
                            "new_content": candidate.content.model_dump(mode="json"),
                            "job_id": candidate.created_job_id,
                            "actor_type": MemoryActorType.WORKFLOW.value,
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
            superseded_rows = (
                await self._session.execute(
                    select(
                        agent_memory.c.id,
                        agent_memory.c.uid,
                        agent_memory.c.content,
                    ).where(
                        agent_memory.c.uid.in_(superseded),
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
                    "actor_type": MemoryActorType.WORKFLOW.value,
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
                    agent_memory.c.uid.in_(superseded),
                    agent_memory.c.status == MemoryStatus.ACTIVE.value,
                )
                .values(status=MemoryStatus.DELETED.value, deleted_at=func.now())
            )
            await self._outbox.set_desired_state(
                superseded,
                MemoryIndexOperation.DELETE,
            )
        await self._outbox.set_desired_state(
            {candidate.uid for candidate in accepted_candidates},
            MemoryIndexOperation.UPSERT,
        )
        await self._outbox.set_desired_state(
            deleted_uids,
            MemoryIndexOperation.DELETE,
        )

    async def find_compatible_scopes(
        self,
        source: str,
        scope_fingerprints: dict[str, str],
        kind: MemoryKind,
        content_version: str,
        *,
        per_scope_limit: int = 20,
    ) -> dict[str, list[StoredMemory]]:
        """批量读取一组精确作用域的活动权威记忆。"""
        if not scope_fingerprints:
            return {}
        conditions = [
            and_(
                agent_memory.c.scope_key == scope,
                agent_memory.c.schema_fingerprint == fingerprint,
            )
            for scope, fingerprint in scope_fingerprints.items()
        ]
        rows = (
            await self._session.execute(
                select(agent_memory)
                .where(
                    agent_memory.c.source == source,
                    agent_memory.c.kind == kind.value,
                    agent_memory.c.status == MemoryStatus.ACTIVE.value,
                    agent_memory.c.content_version == content_version,
                    or_(*conditions),
                )
                .order_by(agent_memory.c.updated_at.desc(), agent_memory.c.id.desc())
                .limit(len(conditions) * per_scope_limit)
            )
        ).mappings()
        result = {scope: [] for scope in scope_fingerprints}
        for row in rows:
            scope = str(row["scope_key"])
            if len(result[scope]) < per_scope_limit:
                result[scope].append(StoredMemory(int(row["id"]), _parse_detail(row)))
        return result

    async def pending_user_updates(
        self,
        source: str,
        scope_keys: set[str],
        *,
        limit: int = 500,
    ) -> dict[str, MemoryContent]:
        """读取每个活动作用域最新的待重处理用户 UPDATE 内容。"""
        if not scope_keys:
            return {}
        rows = (
            await self._session.execute(
                select(
                    agent_memory.c.scope_key,
                    agent_memory_event.c.new_content,
                )
                .select_from(
                    agent_memory_event.join(
                        agent_memory,
                        agent_memory.c.id == agent_memory_event.c.memory_id,
                    )
                )
                .where(
                    agent_memory.c.source == source,
                    agent_memory.c.scope_key.in_(scope_keys),
                    agent_memory.c.status == MemoryStatus.ACTIVE.value,
                    agent_memory_event.c.event_type == MemoryEventType.UPDATE.value,
                    agent_memory_event.c.actor_type == MemoryActorType.USER.value,
                )
                .order_by(agent_memory_event.c.id.desc())
                .limit(limit)
            )
        ).mappings()
        result: dict[str, MemoryContent] = {}
        for row in rows:
            scope = str(row["scope_key"])
            if scope not in result and row["new_content"] is not None:
                result[scope] = _decode_content(row["new_content"])
        return result

    async def latest_user_update(
        self,
        memory_id: int,
    ) -> MemoryContent | None:
        """读取指定权威记忆最新的待重处理用户修正。"""
        value = await self._session.scalar(
            select(agent_memory_event.c.new_content)
            .where(
                agent_memory_event.c.memory_id == memory_id,
                agent_memory_event.c.event_type == MemoryEventType.UPDATE.value,
                agent_memory_event.c.actor_type == MemoryActorType.USER.value,
            )
            .order_by(agent_memory_event.c.id.desc())
            .limit(1)
        )
        return _decode_content(value) if value is not None else None

    async def find_active_by_fingerprint(
        self,
        source: str,
        schema_fingerprint: str,
        kinds: set[MemoryKind],
        content_version: str,
        *,
        limit: int = 500,
    ) -> list[StoredMemory]:
        """读取完整模式指纹下的兼容活动记忆。"""
        rows = (
            await self._session.execute(
                select(agent_memory)
                .where(
                    agent_memory.c.source == source,
                    agent_memory.c.schema_fingerprint == schema_fingerprint,
                    agent_memory.c.kind.in_([kind.value for kind in kinds]),
                    agent_memory.c.status == MemoryStatus.ACTIVE.value,
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
        result = {memory_id: set() for memory_id in memory_ids}
        for memory_id, uid in rows:
            result[int(memory_id)].add(str(uid))
        return result

    async def get_by_uid(self, uid: str) -> StoredMemory | None:
        """读取一条权威记忆及有界关联。"""
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
                    )
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

    async def get_many_active(self, uids: list[str]) -> list[StoredMemory]:
        """批量回查活动权威内容，保持输入 UID 的稳定顺序。"""
        if not uids:
            return []
        rows = (
            await self._session.execute(
                select(agent_memory).where(
                    agent_memory.c.uid.in_(uids),
                    agent_memory.c.status == MemoryStatus.ACTIVE.value,
                )
            )
        ).mappings()
        by_uid = {
            str(row["uid"]): StoredMemory(int(row["id"]), _parse_detail(row))
            for row in rows
        }
        return [by_uid[uid] for uid in uids if uid in by_uid]

    async def find_exact_query(
        self,
        source: str,
        query: str,
        kinds: set[MemoryKind] | None,
        *,
        limit: int,
    ) -> list[str]:
        """以 scope key 或完整投影文本执行安全的 MySQL 精确基线检索。"""
        filters = [
            agent_memory.c.source == source,
            agent_memory.c.status == MemoryStatus.ACTIVE.value,
            agent_memory.c.content_version == app_config.memory.content_version,
            or_(
                agent_memory.c.scope_key == query,
                agent_memory.c.memory_text == query,
            ),
        ]
        if kinds:
            filters.append(agent_memory.c.kind.in_([kind.value for kind in kinds]))
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

    async def history(
        self,
        uid: str,
        *,
        offset: int,
        limit: int,
    ) -> MemoryHistoryPage | None:
        """读取有界只追加历史。"""
        memory = await self.get_by_uid(uid)
        if memory is None:
            return None
        rows = list(
            (
                await self._session.execute(
                    select(agent_memory_event)
                    .where(agent_memory_event.c.memory_id == memory.id)
                    .order_by(agent_memory_event.c.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).mappings()
        )
        events = [
            MemoryEvent(
                id=int(row["id"]),
                memory_uid=uid,
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

    async def append_user_update(
        self,
        memory: StoredMemory,
        content: MemoryContent,
    ) -> int:
        """只追加待重新处理的用户修正，不改写活动权威事实。"""
        result = await self._session.execute(
            insert(agent_memory_event).values(
                memory_id=memory.id,
                event_type=MemoryEventType.UPDATE.value,
                old_content=memory.content.model_dump(mode="json"),
                new_content=content.model_dump(mode="json"),
                job_id=None,
                actor_type=MemoryActorType.USER.value,
            )
        )
        cursor = result if isinstance(result, CursorResult) else None
        primary_key = cursor.inserted_primary_key if cursor is not None else None
        if primary_key is None or primary_key[0] is None:
            raise RuntimeError("记忆更新事件未返回主键")
        return int(primary_key[0])

    async def soft_delete(self, memory: StoredMemory) -> None:
        """软删除权威记忆并写历史及双目标 DELETE outbox。"""
        if memory.detail.status == MemoryStatus.DELETED:
            return
        await self._session.execute(
            update(agent_memory)
            .where(agent_memory.c.id == memory.id)
            .values(status=MemoryStatus.DELETED.value, deleted_at=func.now())
        )
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
        await self._outbox.set_desired_state(
            {memory.detail.uid},
            MemoryIndexOperation.DELETE,
        )
