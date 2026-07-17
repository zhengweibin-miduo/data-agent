"""长期 LLM 记忆及关系仓储。"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select, tuple_, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.models import (
    MEMORY_CONTENT_ADAPTER,
    MemoryCandidate,
    MemoryContent,
    MemoryDetail,
    MemoryKind,
    MemoryListItem,
    MemoryPage,
    MemoryPayload,
    MemoryRelation,
    MemoryRelationType,
    MemoryRowStatus,
)
from data_agent.ddl_metadata.persistence.tables import llm_memory, llm_memory_relation

_DETAIL_RELATION_LIMIT = 500


@dataclass(frozen=True)
class StoredMemory:
    """仓储内部使用的已解析记忆。"""

    id: int
    item: MemoryListItem
    content: MemoryContent
    payload: MemoryPayload


def _decode_json(value: object) -> object:
    """兼容驱动返回 JSON 对象或 JSON 字符串。"""
    return json.loads(value) if isinstance(value, str) else value


def _summary(content: MemoryContent) -> str:
    """生成不暴露提示词或回答正文的有界摘要。"""
    if content.kind == MemoryKind.SEMANTIC_DECISION:
        decision = content.table or content.column
        if decision is None:
            raise ValueError("语义决策缺少对象")
        return f"{content.kind.value}: {decision.description[:160]}"
    if content.kind == MemoryKind.METRIC_DEFINITION:
        return f"{content.kind.value}: {content.metric.name}"
    if content.kind == MemoryKind.METRIC_QUESTION:
        return f"{content.kind.value}: {content.question.question_id}"
    return f"{content.kind.value}: {content.answer.question_id}"


def parse_stored_memory(row: RowMapping) -> StoredMemory:
    """将数据库行解析回当前版本的严格契约。"""
    content = MEMORY_CONTENT_ADAPTER.validate_python(_decode_json(row["content"]))
    payload = MemoryPayload.model_validate(_decode_json(row["payload"]))
    item = MemoryListItem(
        uid=str(row["uid"]),
        source=str(row["source"]),
        kind=MemoryKind(str(row["kind"])),
        scope_key=str(row["scope_key"]),
        schema_fingerprint=str(row["schema_fingerprint"]),
        row_status=MemoryRowStatus(str(row["row_status"])),
        pinned=bool(row["pinned"]),
        summary=_summary(content),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
    return StoredMemory(
        id=int(row["id"]),
        item=item,
        content=content,
        payload=payload,
    )


def _encode_cursor(updated_at: datetime, identifier: int) -> str:
    """编码无状态分页游标。"""
    payload = json.dumps(
        [updated_at.isoformat(), identifier],
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    """校验并解码分页游标。"""
    try:
        padding = "=" * (-len(cursor) % 4)
        updated_at, identifier = json.loads(base64.urlsafe_b64decode(cursor + padding))
        return datetime.fromisoformat(updated_at), int(identifier)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise DDLMetadataError(
            "invalid_cursor",
            "memory_list",
            "记忆分页游标无效",
        ) from error


class MemoryRepository:
    """在调用方事务中管理规范内容、载荷和类型化关系。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由调用方管理事务边界的 Session。"""
        self._session = session

    async def upsert_candidates(
        self,
        candidates: list[MemoryCandidate],
    ) -> None:
        """幂等写入候选、关系，并归档被替代的旧记忆。"""
        if not candidates:
            return
        for candidate in candidates:
            if candidate.kind not in {
                MemoryKind.SEMANTIC_DECISION,
                MemoryKind.METRIC_DEFINITION,
            }:
                continue
            older_uids = (
                await self._session.scalars(
                    select(llm_memory.c.uid).where(
                        llm_memory.c.source == candidate.source,
                        llm_memory.c.kind == candidate.kind.value,
                        llm_memory.c.scope_key == candidate.scope_key,
                        llm_memory.c.row_status == MemoryRowStatus.NORMAL.value,
                        llm_memory.c.uid != candidate.uid,
                    )
                )
            ).all()
            candidate.supersedes_uids = sorted(
                {*candidate.supersedes_uids, *older_uids}
            )
        statement = insert(llm_memory).values(
            [
                {
                    "uid": candidate.uid,
                    "source": candidate.source,
                    "kind": candidate.kind.value,
                    "scope_key": candidate.scope_key,
                    "schema_fingerprint": candidate.schema_fingerprint,
                    "row_status": MemoryRowStatus.NORMAL.value,
                    "pinned": candidate.pinned,
                    "content": candidate.content.model_dump(mode="json"),
                    "payload": candidate.payload.model_dump(mode="json"),
                    "content_version": candidate.content_version,
                }
                for candidate in candidates
            ]
        )
        await self._session.execute(
            statement.on_duplicate_key_update(
                content=statement.inserted.content,
                payload=statement.inserted.payload,
                content_version=statement.inserted.content_version,
                row_status=MemoryRowStatus.NORMAL.value,
            )
        )
        all_uids = {
            uid
            for candidate in candidates
            for uid in (
                candidate.uid,
                *candidate.reference_uids,
                *candidate.comment_uids,
                *candidate.supersedes_uids,
            )
        }
        uid_rows = (
            await self._session.execute(
                select(llm_memory.c.uid, llm_memory.c.id).where(
                    llm_memory.c.uid.in_(all_uids)
                )
            )
        ).all()
        uid_to_id = {str(row[0]): int(row[1]) for row in uid_rows}
        missing = all_uids - set(uid_to_id)
        if missing:
            raise DDLMetadataError(
                "memory_relation_target_missing",
                "persist_snapshot",
                "记忆关系引用不存在的目标",
                details={"uids": ",".join(sorted(missing))},
            )

        relations: list[dict[str, object]] = []
        for candidate in candidates:
            memory_id = uid_to_id[candidate.uid]
            relations.extend(
                {
                    "memory_id": memory_id,
                    "related_memory_id": uid_to_id[uid],
                    "relation_type": relation_type.value,
                }
                for relation_type, uids in (
                    (
                        MemoryRelationType.REFERENCE,
                        candidate.reference_uids,
                    ),
                    (MemoryRelationType.COMMENT, candidate.comment_uids),
                    (
                        MemoryRelationType.SUPERSEDES,
                        candidate.supersedes_uids,
                    ),
                )
                for uid in uids
            )
        if relations:
            relation_statement = insert(llm_memory_relation).values(relations)
            await self._session.execute(
                relation_statement.on_duplicate_key_update(
                    relation_type=relation_statement.inserted.relation_type
                )
            )
        superseded = {
            uid for candidate in candidates for uid in candidate.supersedes_uids
        }
        if superseded:
            await self._session.execute(
                update(llm_memory)
                .where(llm_memory.c.uid.in_(superseded))
                .values(
                    row_status=MemoryRowStatus.ARCHIVED.value,
                    pinned=False,
                )
            )

    async def find_compatible(
        self,
        source: str,
        scope_key: str,
        schema_fingerprint: str,
        kind: MemoryKind,
        payload_version: str,
        content_version: str,
        *,
        limit: int = 20,
    ) -> list[StoredMemory]:
        """精确检索当前版本的 NORMAL 记忆。"""
        rows = (
            await self._session.execute(
                select(llm_memory)
                .where(
                    llm_memory.c.source == source,
                    llm_memory.c.scope_key == scope_key,
                    llm_memory.c.schema_fingerprint == schema_fingerprint,
                    llm_memory.c.kind == kind.value,
                    llm_memory.c.row_status == MemoryRowStatus.NORMAL.value,
                    llm_memory.c.content_version == content_version,
                )
                .order_by(
                    llm_memory.c.pinned.desc(),
                    llm_memory.c.updated_at.desc(),
                    llm_memory.c.id.desc(),
                )
                .limit(limit)
            )
        ).mappings()
        compatible: list[StoredMemory] = []
        for row in rows:
            try:
                memory = parse_stored_memory(row)
            except (ValueError, TypeError):
                continue
            if memory.payload.version == payload_version:
                compatible.append(memory)
        return compatible

    async def find_compatible_scopes(
        self,
        source: str,
        scope_fingerprints: dict[str, str],
        kind: MemoryKind,
        payload_version: str,
        content_version: str,
        *,
        per_scope_limit: int = 20,
    ) -> dict[str, list[StoredMemory]]:
        """单次查询一组精确作用域，避免逐对象读取。"""
        if not scope_fingerprints:
            return {}
        pairs = list(scope_fingerprints.items())
        rows = (
            await self._session.execute(
                select(llm_memory)
                .where(
                    llm_memory.c.source == source,
                    llm_memory.c.kind == kind.value,
                    llm_memory.c.row_status == MemoryRowStatus.NORMAL.value,
                    llm_memory.c.content_version == content_version,
                    tuple_(
                        llm_memory.c.scope_key,
                        llm_memory.c.schema_fingerprint,
                    ).in_(pairs),
                )
                .order_by(
                    llm_memory.c.pinned.desc(),
                    llm_memory.c.updated_at.desc(),
                    llm_memory.c.id.desc(),
                )
                .limit(len(pairs) * per_scope_limit)
            )
        ).mappings()
        result: dict[str, list[StoredMemory]] = {
            scope: [] for scope in scope_fingerprints
        }
        for row in rows:
            scope = str(row["scope_key"])
            if len(result[scope]) >= per_scope_limit:
                continue
            try:
                memory = parse_stored_memory(row)
            except (ValueError, TypeError):
                continue
            if memory.payload.version == payload_version:
                result[scope].append(memory)
        return result

    async def find_active_by_fingerprint(
        self,
        source: str,
        schema_fingerprint: str,
        kinds: set[MemoryKind],
        payload_version: str,
        content_version: str,
        *,
        limit: int = 500,
    ) -> list[StoredMemory]:
        """批量读取完整模式指纹下的兼容活动记忆。"""
        rows = (
            await self._session.execute(
                select(llm_memory)
                .where(
                    llm_memory.c.source == source,
                    llm_memory.c.schema_fingerprint == schema_fingerprint,
                    llm_memory.c.kind.in_([kind.value for kind in kinds]),
                    llm_memory.c.row_status == MemoryRowStatus.NORMAL.value,
                    llm_memory.c.content_version == content_version,
                )
                .order_by(
                    llm_memory.c.pinned.desc(),
                    llm_memory.c.updated_at.desc(),
                    llm_memory.c.id.desc(),
                )
                .limit(limit)
            )
        ).mappings()
        result: list[StoredMemory] = []
        for row in rows:
            try:
                memory = parse_stored_memory(row)
            except (ValueError, TypeError):
                continue
            if memory.payload.version == payload_version:
                result.append(memory)
        return result

    async def related_uids(
        self,
        memory_ids: set[int],
        relation_type: MemoryRelationType,
    ) -> dict[int, set[str]]:
        """批量读取一组记忆的指定出向关系目标 UID。"""
        if not memory_ids:
            return {}
        related_memory = llm_memory.alias("related_memory")
        rows = (
            await self._session.execute(
                select(
                    llm_memory_relation.c.memory_id,
                    related_memory.c.uid,
                )
                .select_from(
                    llm_memory_relation.join(
                        related_memory,
                        related_memory.c.id == llm_memory_relation.c.related_memory_id,
                    )
                )
                .where(
                    llm_memory_relation.c.memory_id.in_(memory_ids),
                    llm_memory_relation.c.relation_type == relation_type.value,
                )
            )
        ).all()
        result: dict[int, set[str]] = {memory_id: set() for memory_id in memory_ids}
        for memory_id, uid in rows:
            result[int(memory_id)].add(str(uid))
        return result

    async def list_page(
        self,
        source: str,
        *,
        kind: MemoryKind | None = None,
        row_status: MemoryRowStatus = MemoryRowStatus.NORMAL,
        pinned: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> MemoryPage:
        """返回按更新时间和主键稳定排序的有界列表。"""
        filters = [
            llm_memory.c.source == source,
            llm_memory.c.row_status == row_status.value,
        ]
        if kind is not None:
            filters.append(llm_memory.c.kind == kind.value)
        if pinned is not None:
            filters.append(llm_memory.c.pinned == pinned)
        if cursor is not None:
            updated_at, identifier = _decode_cursor(cursor)
            filters.append(
                or_(
                    llm_memory.c.updated_at < updated_at,
                    and_(
                        llm_memory.c.updated_at == updated_at,
                        llm_memory.c.id < identifier,
                    ),
                )
            )
        rows = list(
            (
                await self._session.execute(
                    select(llm_memory)
                    .where(*filters)
                    .order_by(
                        llm_memory.c.updated_at.desc(),
                        llm_memory.c.id.desc(),
                    )
                    .limit(limit + 1)
                )
            ).mappings()
        )
        parsed = [parse_stored_memory(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and parsed:
            last = parsed[-1]
            next_cursor = _encode_cursor(last.item.updated_at, last.id)
        return MemoryPage(
            items=[memory.item for memory in parsed],
            next_cursor=next_cursor,
        )

    async def get_by_uid(self, uid: str) -> StoredMemory | None:
        """读取并解析一条记忆。"""
        row = (
            (
                await self._session.execute(
                    select(llm_memory).where(llm_memory.c.uid == uid)
                )
            )
            .mappings()
            .one_or_none()
        )
        return parse_stored_memory(row) if row is not None else None

    async def get_detail(self, uid: str) -> MemoryDetail | None:
        """读取记忆详情及双向关系。"""
        memory = await self.get_by_uid(uid)
        if memory is None:
            return None
        source_memory = llm_memory.alias("source_memory")
        target_memory = llm_memory.alias("target_memory")
        rows = (
            await self._session.execute(
                select(
                    source_memory.c.uid.label("source_uid"),
                    target_memory.c.uid.label("target_uid"),
                    llm_memory_relation.c.relation_type,
                )
                .select_from(
                    llm_memory_relation.join(
                        source_memory,
                        source_memory.c.id == llm_memory_relation.c.memory_id,
                    ).join(
                        target_memory,
                        target_memory.c.id == llm_memory_relation.c.related_memory_id,
                    )
                )
                .where(
                    or_(
                        llm_memory_relation.c.memory_id == memory.id,
                        llm_memory_relation.c.related_memory_id == memory.id,
                    )
                )
                .order_by(
                    llm_memory_relation.c.relation_type,
                    llm_memory_relation.c.memory_id,
                    llm_memory_relation.c.related_memory_id,
                )
                .limit(_DETAIL_RELATION_LIMIT)
            )
        ).mappings()
        relations = [
            MemoryRelation(
                relation_type=MemoryRelationType(str(row["relation_type"])),
                memory_uid=str(row["source_uid"]),
                related_memory_uid=str(row["target_uid"]),
            )
            for row in rows
        ]
        return MemoryDetail(
            **memory.item.model_dump(),
            content=memory.content,
            payload=memory.payload,
            relations=relations,
        )

    async def patch(
        self,
        uid: str,
        *,
        pinned: bool | None = None,
        archive: bool = False,
    ) -> MemoryDetail:
        """幂等 pin/unpin 或归档记忆。"""
        memory = await self.get_by_uid(uid)
        if memory is None:
            raise DDLMetadataError(
                "memory_not_found",
                "memory_patch",
                "记忆不存在",
                http_status=404,
            )
        if pinned is not None and memory.item.row_status == MemoryRowStatus.ARCHIVED:
            raise DDLMetadataError(
                "archived_memory",
                "memory_patch",
                "归档记忆不能修改 pin 状态",
                http_status=409,
            )
        values: dict[str, object] = {}
        if pinned is not None:
            values["pinned"] = pinned
        if archive:
            values.update(
                row_status=MemoryRowStatus.ARCHIVED.value,
                pinned=False,
            )
        if values:
            await self._session.execute(
                update(llm_memory).where(llm_memory.c.id == memory.id).values(**values)
            )
            await self._session.flush()
        detail = await self.get_detail(uid)
        if detail is None:
            raise AssertionError("更新后的记忆必须存在")
        return detail

    async def rebuild_rows(
        self,
        limit: int,
        source: str | None = None,
        after_id: int = 0,
    ) -> list[RowMapping]:
        """读取一个有界载荷重建批次。"""
        statement = select(llm_memory).where(llm_memory.c.id > after_id)
        if source is not None:
            statement = statement.where(llm_memory.c.source == source)
        rows = (
            await self._session.execute(
                statement.order_by(llm_memory.c.id).limit(limit)
            )
        ).mappings()
        return list(rows)

    async def update_payload(
        self,
        identifier: int,
        payload: MemoryPayload,
    ) -> None:
        """仅更新可重建载荷。"""
        await self._session.execute(
            update(llm_memory)
            .where(llm_memory.c.id == identifier)
            .values(payload=payload.model_dump(mode="json"))
        )
