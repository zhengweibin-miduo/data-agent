"""字段值索引的有界扫描、精确频次与差量发布状态机。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import column, delete, literal, or_, select, table, tuple_, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.data_sync.models import (
    DesiredSyncTable,
    decode_row_value,
    encode_row_value,
)
from data_agent.data_sync.tables import data_sync_task
from data_agent.ddl_metadata.persistence.tables import column_info
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.metadata_indexing.elasticsearch import (
    MetadataValueElasticsearchIndex,
    metadata_value_document_id,
)
from data_agent.metadata_indexing.models import (
    ClaimedMetadataIndexWork,
    MetadataValueProjection,
    MetadataValueRefreshPhase,
)
from data_agent.metadata_indexing.projections import (
    MetadataProjectionRepository,
    ProjectionNotReadyError,
    ValueProjectionPlan,
    _stable_value_text,
)
from data_agent.metadata_indexing.repository import (
    MetadataIndexOutboxRepository,
    metadata_desired_version,
)
from data_agent.metadata_indexing.tables import (
    metadata_index_outbox,
    metadata_value_frequency,
    metadata_value_publication,
)
from data_agent.settings import app_config

_ACTION_PAYLOAD_BYTE_LIMIT = 4 * 1024 * 1024


class ValueRefreshPersistenceError(RuntimeError):
    """字段值状态的本地事务失败，不消耗远程失败预算。"""


def _value_hash(value_text: str) -> str:
    return hashlib.sha256(value_text.encode()).hexdigest()


def _primary_key_types(plan: ValueProjectionPlan) -> list[str]:
    columns = {item.name: item for item in plan.desired.columns}
    return [columns[name].data_type for name in plan.desired.primary_key]


def _cursor_values(
    plan: ValueProjectionPlan,
    values: Sequence[object],
) -> dict[str, object]:
    """编码并绑定当前 schema 与有序主键定义的可恢复游标。"""
    return {
        "v": 1,
        "schema_fingerprint": plan.desired.schema_fingerprint,
        "columns": list(plan.desired.primary_key),
        "types": _primary_key_types(plan),
        "values": [encode_row_value(value) for value in values],
    }


def _decoded_cursor(
    plan: ValueProjectionPlan,
    cursor: Mapping[str, object] | None,
) -> tuple[object, ...] | None:
    """只恢复与当前扫描计划身份完全一致的游标。"""
    if cursor is None:
        return None
    expected = {
        "v": 1,
        "schema_fingerprint": plan.desired.schema_fingerprint,
        "columns": list(plan.desired.primary_key),
        "types": _primary_key_types(plan),
    }
    actual = {name: cursor.get(name) for name in expected}
    if actual != expected:
        raise ValueError("字段值扫描主键游标与当前 schema 或主键定义不匹配")
    values = cursor.get("values")
    if not isinstance(values, list) or len(values) != len(plan.desired.primary_key):
        raise ValueError("字段值扫描主键游标值数量不匹配")
    return tuple(decode_row_value(value) for value in values)  # type: ignore[arg-type]


def _current_column(
    plan: ValueProjectionPlan,
    progress_column_id: str | None,
) -> tuple[int, tuple[str, str, str]] | None:
    if not plan.columns:
        return None
    if progress_column_id is None:
        return 0, plan.columns[0]
    for index, item in enumerate(plan.columns):
        if item[0] == progress_column_id:
            return index, item
    return 0, plan.columns[0]


@dataclass(frozen=True)
class FrequencyMutationState:
    """一次 DW 事务开始时锁定的逻辑表频次状态。"""

    table_id: str
    phase: MetadataValueRefreshPhase
    frequency_version: str
    progress_column_id: str | None
    last_primary_key: tuple[object, ...] | None
    plan: ValueProjectionPlan


async def prepare_frequency_mutation(
    session: AsyncSession,
    desired: DesiredSyncTable,
) -> list[FrequencyMutationState]:
    """在 DW DML 前按稳定顺序锁定共享目标涉及的 VALUES 状态。"""
    peer_payloads = (
        await session.scalars(
            select(data_sync_task.c.desired_json).where(
                data_sync_task.c.target_table == desired.target_table
            )
        )
    ).all()
    peers = [DesiredSyncTable.model_validate(payload) for payload in peer_payloads]
    column_ids = {column.id for peer in [desired, *peers] for column in peer.columns}
    table_ids = sorted(
        {
            str(value)
            for value in await session.scalars(
                select(column_info.c.table_id).where(column_info.c.id.in_(column_ids))
            )
        }
    )
    states: list[FrequencyMutationState] = []
    for table_id in table_ids:
        row = (
            (
                await session.execute(
                    select(metadata_index_outbox)
                    .where(
                        metadata_index_outbox.c.target == "values",
                        metadata_index_outbox.c.object_id == table_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["frequency_version"] is None or row["phase"] is None:
            continue
        try:
            plan = await MetadataProjectionRepository(session).value_projection_plan(
                table_id
            )
        except ProjectionNotReadyError:
            # PENDING_SCHEMA 阶段尚无稳定 DW 快照可维护；后续首次 SCAN 会从
            # 物化后的全量行建立精确基线，不能让 CDC 事务被索引投影阻塞。
            continue
        if plan is None or plan.desired.target_table != desired.target_table:
            continue
        states.append(
            FrequencyMutationState(
                table_id=table_id,
                phase=MetadataValueRefreshPhase(row["phase"]),
                frequency_version=str(row["frequency_version"]),
                progress_column_id=row["progress_column_id"],
                last_primary_key=_decoded_cursor(plan, row["last_primary_key"]),
                plan=plan,
            )
        )
    return states


def _row_is_counted(
    state: FrequencyMutationState,
    column_id: str,
    row: Mapping[str, object],
) -> bool:
    """判断 SCAN 游标是否已经包含该行的该字段。"""
    if state.phase != MetadataValueRefreshPhase.SCAN:
        return True
    column_ids = [item[0] for item in state.plan.columns]
    if not column_ids:
        return False
    try:
        current_index = (
            0
            if state.progress_column_id is None
            else column_ids.index(state.progress_column_id)
        )
        column_index = column_ids.index(column_id)
    except ValueError:
        return False
    if column_index < current_index:
        return True
    if column_index > current_index or state.last_primary_key is None:
        return False
    primary_key = tuple(row[name] for name in state.plan.desired.primary_key)
    return primary_key <= state.last_primary_key


async def apply_frequency_row_changes(
    session: AsyncSession,
    states: Sequence[FrequencyMutationState],
    before_rows: Sequence[Mapping[str, object]],
    after_rows: Sequence[Mapping[str, object]],
) -> None:
    """在 DW DML 事务内按 before/after 镜像维护精确频次。"""
    repository = MetadataValueFrequencyRepository(session)
    for state in states:
        for column_id, name, data_type in state.plan.columns:
            delta: Counter[str] = Counter()
            for row in before_rows:
                if row.get(name) is not None and _row_is_counted(state, column_id, row):
                    delta[_stable_value_text(row[name], data_type)] -= 1
            for row in after_rows:
                if row.get(name) is not None and _row_is_counted(state, column_id, row):
                    delta[_stable_value_text(row[name], data_type)] += 1
            for value_text, amount in sorted(delta.items()):
                if amount:
                    await repository.apply_delta(
                        table_id=state.table_id,
                        column_id=column_id,
                        frequency_version=state.frequency_version,
                        value_text=value_text,
                        delta=amount,
                    )


class MetadataValueFrequencyRepository:
    """在调用方事务中维护精确频次与 publication 集合。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定调用方事务。"""
        self._session = session

    async def scan_rows(
        self,
        plan: ValueProjectionPlan,
        column_item: tuple[str, str, str],
        after_key: Mapping[str, object] | None,
    ) -> list[dict[str, object]]:
        """按 DW 主键读取一个有限批次并锁定这些行。"""
        _, value_name, _ = column_item
        names = list(dict.fromkeys([*plan.desired.primary_key, value_name]))
        target = table(
            plan.desired.target_table,
            *(column(name) for name in names),
            schema=app_config.data_sync.dw_database,
        )
        primary_keys = [target.c[name] for name in plan.desired.primary_key]
        statement = select(*(target.c[name] for name in names)).order_by(*primary_keys)
        decoded = _decoded_cursor(plan, after_key)
        if decoded is not None:
            if len(decoded) != len(primary_keys):
                raise ValueError("字段值扫描主键游标长度不匹配")
            statement = statement.where(
                tuple_(*primary_keys) > tuple_(*(literal(value) for value in decoded))
            )
        rows = await self._session.execute(
            statement.limit(app_config.metadata_index.value_scan_batch_size).with_for_update()
        )
        return [dict(row) for row in rows.mappings()]

    async def add_scan_values(
        self,
        *,
        table_id: str,
        frequency_version: str,
        column_item: tuple[str, str, str],
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        """把一个扫描批次的值精确累加到当前 generation。"""
        column_id, name, data_type = column_item
        counts: Counter[str] = Counter(
            _stable_value_text(row[name], data_type)
            for row in rows
            if row[name] is not None
        )
        for value_text, frequency in sorted(counts.items()):
            await self.apply_delta(
                table_id=table_id,
                column_id=column_id,
                frequency_version=frequency_version,
                value_text=value_text,
                delta=frequency,
            )

    async def apply_delta(
        self,
        *,
        table_id: str,
        column_id: str,
        frequency_version: str,
        value_text: str,
        delta: int,
    ) -> None:
        """锁定一个规范值并应用精确正负变化。"""
        value_hash = _value_hash(value_text)
        identity = (
            metadata_value_frequency.c.table_id == table_id,
            metadata_value_frequency.c.column_id == column_id,
            metadata_value_frequency.c.frequency_version == frequency_version,
            metadata_value_frequency.c.value_hash == value_hash,
        )
        row = (
            (
                await self._session.execute(
                    select(metadata_value_frequency)
                    .where(*identity)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            if delta < 0:
                raise RuntimeError("字段值精确频次出现未应用事件导致的负数")
            await self._session.execute(
                insert(metadata_value_frequency).values(
                    table_id=table_id,
                    column_id=column_id,
                    frequency_version=frequency_version,
                    value_hash=value_hash,
                    value_text=value_text,
                    frequency=delta,
                )
            )
            return
        if str(row["value_text"]) != value_text:
            raise RuntimeError("字段值哈希碰撞")
        frequency = int(row["frequency"]) + delta
        if frequency < 0:
            raise RuntimeError("字段值精确频次不能为负数")
        if frequency == 0:
            await self._session.execute(
                delete(metadata_value_frequency).where(*identity)
            )
        else:
            await self._session.execute(
                update(metadata_value_frequency)
                .where(*identity)
                .values(frequency=frequency)
            )

    async def materialize_top_n(
        self,
        item: ClaimedMetadataIndexWork,
        plan: ValueProjectionPlan,
        column_item: tuple[str, str, str],
    ) -> None:
        """物化一个字段当前版本的精确 Top-N membership。"""
        if item.frequency_version is None or item.index_generation is None:
            raise ValueError("字段值刷新缺少 generation")
        column_id, _, _ = column_item
        rows = (
            (
                await self._session.execute(
                    select(
                        metadata_value_frequency.c.value_hash,
                        metadata_value_frequency.c.value_text,
                        metadata_value_frequency.c.frequency,
                    )
                    .where(
                        metadata_value_frequency.c.table_id == item.object_id,
                        metadata_value_frequency.c.column_id == column_id,
                        metadata_value_frequency.c.frequency_version
                        == item.frequency_version,
                    )
                    .order_by(
                        metadata_value_frequency.c.frequency.desc(),
                        metadata_value_frequency.c.value_hash,
                    )
                    .limit(app_config.metadata_index.value_top_n)
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            projection = MetadataValueProjection(
                column_id=column_id,
                table_id=item.object_id,
                value_text=str(row["value_text"]),
                value_keyword=str(row["value_text"]),
                frequency=int(row["frequency"]),
                refresh_version=item.desired_version,
                schema_fingerprint=plan.desired.schema_fingerprint,
            )
            payload = projection.model_dump(mode="json")
            payload_hash = metadata_desired_version(payload)
            document_id = metadata_value_document_id(
                item.object_id,
                column_id,
                str(row["value_hash"]),
            )
            statement = insert(metadata_value_publication).values(
                table_id=item.object_id,
                index_generation=item.index_generation,
                document_id=document_id,
                column_id=column_id,
                value_hash=row["value_hash"],
                value_text=row["value_text"],
                schema_fingerprint=plan.desired.schema_fingerprint,
                desired_membership_version=item.desired_version,
                desired_frequency=row["frequency"],
                desired_payload_hash=payload_hash,
            )
            await self._session.execute(
                statement.on_duplicate_key_update(
                    column_id=statement.inserted.column_id,
                    value_hash=statement.inserted.value_hash,
                    value_text=statement.inserted.value_text,
                    schema_fingerprint=statement.inserted.schema_fingerprint,
                    desired_membership_version=statement.inserted.desired_membership_version,
                    desired_frequency=statement.inserted.desired_frequency,
                    desired_payload_hash=statement.inserted.desired_payload_hash,
                )
            )

    async def prepare_publish(
        self,
        item: ClaimedMetadataIndexWork,
    ) -> list[tuple[str, MetadataValueProjection]]:
        """持久化并返回一个有界 UPSERT 动作批次。"""
        rows = await self._publication_candidates(item, "upsert")
        actions: list[tuple[str, MetadataValueProjection]] = []
        payload_bytes = 0
        for row in rows:
            if (
                row["pending_action"] == "upsert"
                and row["action_version"] == item.desired_version
                and row["action_payload_json"] is not None
            ):
                projection = MetadataValueProjection.model_validate(
                    row["action_payload_json"]
                )
            else:
                projection = MetadataValueProjection(
                    column_id=str(row["column_id"]),
                    table_id=item.object_id,
                    value_text=str(row["value_text"]),
                    value_keyword=str(row["value_text"]),
                    frequency=int(str(row["desired_frequency"])),
                    refresh_version=item.desired_version,
                    schema_fingerprint=str(row["schema_fingerprint"]),
                )
            payload = projection.model_dump(mode="json")
            encoded_size = len(json.dumps(payload, ensure_ascii=False).encode())
            if actions and payload_bytes + encoded_size > _ACTION_PAYLOAD_BYTE_LIMIT:
                break
            payload_bytes += encoded_size
            actions.append((str(row["document_id"]), projection))
            await self._session.execute(
                update(metadata_value_publication)
                .where(
                    metadata_value_publication.c.table_id == item.object_id,
                    metadata_value_publication.c.index_generation
                    == item.index_generation,
                    metadata_value_publication.c.document_id == row["document_id"],
                )
                .values(
                    pending_action="upsert",
                    action_version=item.desired_version,
                    action_payload_hash=row["desired_payload_hash"],
                    action_payload_json=payload,
                )
            )
        return actions

    async def _publication_candidates(
        self,
        item: ClaimedMetadataIndexWork,
        action: str,
    ) -> list[Mapping[str, object]]:
        if item.index_generation is None:
            raise ValueError("字段值刷新缺少 index_generation")
        if action == "upsert":
            predicate = or_(
                metadata_value_publication.c.pending_action == "upsert",
                metadata_value_publication.c.published_payload_hash.is_distinct_from(
                    metadata_value_publication.c.desired_payload_hash
                ),
            )
            membership = (
                metadata_value_publication.c.desired_membership_version
                == item.desired_version
            )
        else:
            predicate = or_(
                metadata_value_publication.c.pending_action == "delete",
                metadata_value_publication.c.pending_action == "upsert",
                metadata_value_publication.c.published_payload_hash.is_not(None),
            )
            membership = or_(
                metadata_value_publication.c.desired_membership_version.is_(None),
                metadata_value_publication.c.desired_membership_version
                != item.desired_version,
            )
        rows = (
            (
                await self._session.execute(
                    select(
                        metadata_value_publication,
                        # projection schema fingerprint comes from the active plan and
                        # is persisted in the immutable action body on first prepare.
                    )
                    .where(
                        metadata_value_publication.c.table_id == item.object_id,
                        metadata_value_publication.c.index_generation
                        == item.index_generation,
                        membership,
                        predicate,
                    )
                    .order_by(metadata_value_publication.c.document_id)
                    .limit(app_config.metadata_index.value_bulk_batch_size)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    async def settle_publish(
        self,
        item: ClaimedMetadataIndexWork,
        document_ids: Sequence[str],
    ) -> None:
        """结算已成功发送的 UPSERT 动作。"""
        if not document_ids:
            return
        await self._session.execute(
            update(metadata_value_publication)
            .where(
                metadata_value_publication.c.table_id == item.object_id,
                metadata_value_publication.c.index_generation == item.index_generation,
                metadata_value_publication.c.document_id.in_(document_ids),
                metadata_value_publication.c.pending_action == "upsert",
                metadata_value_publication.c.action_version == item.desired_version,
            )
            .values(
                published_payload_hash=metadata_value_publication.c.action_payload_hash,
                pending_action=None,
                action_version=None,
                action_payload_hash=None,
                action_payload_json=None,
            )
        )

    async def prepare_cleanup(
        self,
        item: ClaimedMetadataIndexWork,
    ) -> list[str]:
        """持久化并返回一个有界 DELETE 动作批次。"""
        rows = await self._publication_candidates(item, "delete")
        document_ids = [str(row["document_id"]) for row in rows]
        if document_ids:
            await self._session.execute(
                update(metadata_value_publication)
                .where(
                    metadata_value_publication.c.table_id == item.object_id,
                    metadata_value_publication.c.index_generation
                    == item.index_generation,
                    metadata_value_publication.c.document_id.in_(document_ids),
                )
                .values(
                    pending_action="delete",
                    action_version=item.desired_version,
                    action_payload_hash=None,
                    action_payload_json=None,
                )
            )
        return document_ids

    async def settle_cleanup(
        self,
        item: ClaimedMetadataIndexWork,
        document_ids: Sequence[str],
    ) -> None:
        """结算已成功发送的 DELETE 动作。"""
        if not document_ids:
            return
        identity = (
            metadata_value_publication.c.table_id == item.object_id,
            metadata_value_publication.c.index_generation == item.index_generation,
            metadata_value_publication.c.document_id.in_(document_ids),
            metadata_value_publication.c.pending_action == "delete",
            metadata_value_publication.c.action_version == item.desired_version,
        )
        await self._session.execute(
            update(metadata_value_publication)
            .where(*identity)
            .values(
                published_payload_hash=None,
                pending_action=None,
                action_version=None,
            )
        )
        await self._session.execute(
            delete(metadata_value_publication).where(
                metadata_value_publication.c.table_id == item.object_id,
                metadata_value_publication.c.index_generation == item.index_generation,
                metadata_value_publication.c.document_id.in_(document_ids),
                metadata_value_publication.c.published_payload_hash.is_(None),
                or_(
                    metadata_value_publication.c.desired_membership_version.is_(None),
                    metadata_value_publication.c.desired_membership_version
                    != item.desired_version,
                ),
            )
        )


class MetadataValueRefresh:
    """每次调用只执行一个有界、可恢复的字段值工作单元。"""

    async def run_next_unit(self, item: ClaimedMetadataIndexWork) -> bool:
        """执行一个 phase 单元；返回是否已到 COMPLETE。"""
        if item.phase is None:
            raise ValueError("VALUES 工作缺少 phase")
        if item.frequency_version is None or item.index_generation is None:
            raise ValueError("VALUES 工作缺少 generation")
        if item.phase == MetadataValueRefreshPhase.SCAN:
            await self._scan(item)
        elif item.phase == MetadataValueRefreshPhase.SELECT_TOP_N:
            await self._select_top_n(item)
        elif item.phase == MetadataValueRefreshPhase.PUBLISH:
            await self._publish(item)
        elif item.phase == MetadataValueRefreshPhase.CLEANUP:
            await self._cleanup(item)
        return item.phase == MetadataValueRefreshPhase.COMPLETE

    async def _plan(
        self,
        session: AsyncSession,
        table_id: str,
    ) -> ValueProjectionPlan | None:
        return await MetadataProjectionRepository(session).value_projection_plan(
            table_id
        )

    async def _scan(self, item: ClaimedMetadataIndexWork) -> None:
        try:
            async with MySQLDatabase.session() as session:
                outbox = MetadataIndexOutboxRepository(session)
                if not await outbox.lock_authoritative(item):
                    return
                plan = await self._plan(session, item.object_id)
                current = (
                    None
                    if plan is None
                    else _current_column(plan, item.progress_column_id)
                )
                if plan is None or current is None:
                    await outbox.advance_value_state(
                        item,
                        phase=MetadataValueRefreshPhase.SELECT_TOP_N,
                    )
                    return
                index, column_item = current
                frequency = MetadataValueFrequencyRepository(session)
                rows = await frequency.scan_rows(
                    plan,
                    column_item,
                    item.last_primary_key,
                )
                if rows:
                    assert item.frequency_version is not None
                    await frequency.add_scan_values(
                        table_id=item.object_id,
                        frequency_version=item.frequency_version,
                        column_item=column_item,
                        rows=rows,
                    )
                    last = rows[-1]
                    cursor = _cursor_values(
                        plan,
                        [last[name] for name in plan.desired.primary_key]
                    )
                    await outbox.advance_value_state(
                        item,
                        phase=MetadataValueRefreshPhase.SCAN,
                        progress_column_id=column_item[0],
                        last_primary_key=cursor,
                    )
                    return
                next_column = (
                    plan.columns[index + 1][0]
                    if index + 1 < len(plan.columns)
                    else None
                )
                await outbox.advance_value_state(
                    item,
                    phase=(
                        MetadataValueRefreshPhase.SCAN
                        if next_column is not None
                        else MetadataValueRefreshPhase.SELECT_TOP_N
                    ),
                    progress_column_id=next_column,
                    last_primary_key=None,
                )
        except ProjectionNotReadyError:
            raise
        except Exception as error:
            raise ValueRefreshPersistenceError from error

    async def _select_top_n(self, item: ClaimedMetadataIndexWork) -> None:
        try:
            async with MySQLDatabase.session() as session:
                outbox = MetadataIndexOutboxRepository(session)
                if not await outbox.lock_authoritative(item):
                    return
                plan = await self._plan(session, item.object_id)
                columns = () if plan is None else plan.columns
                if item.progress_column_id is None:
                    index = 0
                else:
                    completed = [column[0] for column in columns]
                    index = completed.index(item.progress_column_id) + 1
                if plan is None or index >= len(columns):
                    await outbox.advance_value_state(
                        item,
                        phase=MetadataValueRefreshPhase.PUBLISH,
                    )
                    return
                await MetadataValueFrequencyRepository(session).materialize_top_n(
                    item,
                    plan,
                    columns[index],
                )
                await outbox.advance_value_state(
                    item,
                    phase=(
                        MetadataValueRefreshPhase.SELECT_TOP_N
                        if index + 1 < len(columns)
                        else MetadataValueRefreshPhase.PUBLISH
                    ),
                    progress_column_id=(
                        columns[index][0] if index + 1 < len(columns) else None
                    ),
                )
        except ProjectionNotReadyError:
            raise
        except Exception as error:
            raise ValueRefreshPersistenceError from error

    async def _publish(self, item: ClaimedMetadataIndexWork) -> None:
        try:
            async with MySQLDatabase.session() as session:
                outbox = MetadataIndexOutboxRepository(session)
                if not await outbox.lock_authoritative(item):
                    return
                actions = await MetadataValueFrequencyRepository(
                    session
                ).prepare_publish(item)
                if not actions:
                    await outbox.advance_value_state(
                        item,
                        phase=MetadataValueRefreshPhase.CLEANUP,
                    )
                    return
        except Exception as error:
            raise ValueRefreshPersistenceError from error
        index = MetadataValueElasticsearchIndex(ElasticsearchClient.get_client())
        await index.upsert_batch([projection for _, projection in actions])
        document_ids = [document_id for document_id, _ in actions]
        try:
            async with MySQLDatabase.session() as session:
                repository = MetadataValueFrequencyRepository(session)
                outbox = MetadataIndexOutboxRepository(session)
                if not await outbox.lock_authoritative(item):
                    return
                await repository.settle_publish(item, document_ids)
                await outbox.advance_value_state(
                    item,
                    phase=MetadataValueRefreshPhase.PUBLISH,
                    bulk_cursor={
                        "phase": MetadataValueRefreshPhase.PUBLISH.value,
                        "desired_version": item.desired_version,
                        "index_generation": item.index_generation,
                        "last_document_id": document_ids[-1],
                    },
                )
        except Exception as error:
            raise ValueRefreshPersistenceError from error

    async def _cleanup(self, item: ClaimedMetadataIndexWork) -> None:
        complete = False
        try:
            async with MySQLDatabase.session() as session:
                outbox = MetadataIndexOutboxRepository(session)
                if not await outbox.lock_authoritative(item):
                    return
                document_ids = await MetadataValueFrequencyRepository(
                    session
                ).prepare_cleanup(item)
                if not document_ids:
                    complete = True
        except Exception as error:
            raise ValueRefreshPersistenceError from error
        index = MetadataValueElasticsearchIndex(ElasticsearchClient.get_client())
        if complete:
            await index.refresh()
            try:
                async with MySQLDatabase.session() as session:
                    outbox = MetadataIndexOutboxRepository(session)
                    if not await outbox.lock_authoritative(item):
                        return
                    await outbox.advance_value_state(
                        item,
                        phase=MetadataValueRefreshPhase.COMPLETE,
                    )
            except Exception as error:
                raise ValueRefreshPersistenceError from error
            return
        await index.delete_documents(document_ids)
        try:
            async with MySQLDatabase.session() as session:
                repository = MetadataValueFrequencyRepository(session)
                outbox = MetadataIndexOutboxRepository(session)
                if not await outbox.lock_authoritative(item):
                    return
                await repository.settle_cleanup(item, document_ids)
                await outbox.advance_value_state(
                    item,
                    phase=MetadataValueRefreshPhase.CLEANUP,
                    bulk_cursor={
                        "phase": MetadataValueRefreshPhase.CLEANUP.value,
                        "desired_version": item.desired_version,
                        "index_generation": item.index_generation,
                        "last_document_id": document_ids[-1],
                    },
                )
        except Exception as error:
            raise ValueRefreshPersistenceError from error
