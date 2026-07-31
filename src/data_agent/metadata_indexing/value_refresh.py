"""字段值索引的有界扫描、精确频次与差量发布状态机。"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    case,
    column,
    delete,
    func,
    literal,
    or_,
    select,
    table,
    tuple_,
    update,
)
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

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
    metadata_value_projection_fits_bulk,
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
        if _has_pending_structure_generation(row):
            # 当前权威结构已经切换到待处理代次，旧 SCAN 游标无法用新 plan
            # 安全解释。跳过旧代次增量，由 dispatcher 提升后从全量 SCAN
            # 建立新基线，不能因此回滚权威 DW 事务。
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


def _has_pending_structure_generation(row: Mapping[Any, object]) -> bool:
    """判断权威 plan 是否已越过当前频次代次的 SCAN 游标身份。"""
    return (
        row["pending_desired_version"] is not None
        and row["pending_frequency_version"] != row["frequency_version"]
    )


def _literal_type_members(data_type: str) -> tuple[str, ...]:
    """解析 ENUM/SET 的声明顺序，供 MySQL 原生排序表达式使用。"""
    start = data_type.find("(")
    if start < 0 or not data_type.rstrip().endswith(")"):
        return ()
    return tuple(
        next(
            csv.reader(
                [data_type[start + 1 : data_type.rfind(")")]],
                delimiter=",",
                quotechar="'",
                escapechar="\\",
                skipinitialspace=True,
            )
        )
    )


def _mysql_order_value(value: object, data_type: str) -> ColumnElement[Any]:
    """把绑定值转换为与 MySQL ENUM/SET 列排序一致的数据库表达式。"""
    normalized = data_type.lstrip().upper()
    members = _literal_type_members(data_type)
    if normalized.startswith("ENUM(") and members:
        bound = literal(value)
        return func.field(bound, *members)
    if normalized.startswith("SET(") and members:
        encoded = encode_row_value(value)
        bound = literal(encoded.get("$set") if isinstance(encoded, dict) else encoded)
        numeric = literal(0)
        for index, member in enumerate(members):
            numeric += case(
                (func.find_in_set(member, bound) > 0, 1 << index),
                else_=0,
            )
        return numeric
    base_type = normalized.split("(", 1)[0].strip()
    if base_type in {"CHAR", "VARCHAR", "TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT"}:
        # DW 字符串主键固定使用二进制排序规则；literal 默认继承当前连接/库的
        # collation，必须显式对齐 keyset scan 才能正确处理大小写和重音差异。
        return literal(value).collate("utf8mb4_0900_bin")
    # DECIMAL、时态与二进制等主键必须按 MySQL 原列类型绑定；游标信封的
    # JSON 编码只用于持久化，不能参与数据库排序比较。
    return literal(value)


async def _row_is_counted(
    session: AsyncSession,
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
    types = _primary_key_types(state.plan)
    primary_key = tuple(
        _mysql_order_value(row[name], data_type)
        for name, data_type in zip(
            state.plan.desired.primary_key,
            types,
            strict=True,
        )
    )
    cursor = tuple(
        _mysql_order_value(value, data_type)
        for value, data_type in zip(
            state.last_primary_key,
            types,
            strict=True,
        )
    )
    return bool(await session.scalar(select(tuple_(*primary_key) <= tuple_(*cursor))))


async def _rows_are_counted(
    session: AsyncSession,
    state: FrequencyMutationState,
    column_id: str,
    rows: Sequence[Mapping[str, object]],
) -> list[bool]:
    """用一次数据库往返批量判断一组行是否已经越过 SCAN 游标。"""
    if not rows:
        return []
    if state.phase != MetadataValueRefreshPhase.SCAN:
        return [True] * len(rows)
    column_ids = [item[0] for item in state.plan.columns]
    if not column_ids:
        return [False] * len(rows)
    try:
        current_index = (
            0
            if state.progress_column_id is None
            else column_ids.index(state.progress_column_id)
        )
        column_index = column_ids.index(column_id)
    except ValueError:
        return [False] * len(rows)
    if column_index < current_index:
        return [True] * len(rows)
    if column_index > current_index or state.last_primary_key is None:
        return [False] * len(rows)

    types = _primary_key_types(state.plan)
    cursor = tuple(
        _mysql_order_value(value, data_type)
        for value, data_type in zip(state.last_primary_key, types, strict=True)
    )
    comparisons = [
        tuple_(
            *(
                _mysql_order_value(row[name], data_type)
                for name, data_type in zip(
                    state.plan.desired.primary_key,
                    types,
                    strict=True,
                )
            )
        )
        <= tuple_(*cursor)
        for row in rows
    ]
    result = (await session.execute(select(*comparisons))).one()
    return [bool(value) for value in result]


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
            before_counted = await _rows_are_counted(
                session, state, column_id, before_rows
            )
            after_counted = await _rows_are_counted(
                session, state, column_id, after_rows
            )
            for row, counted in zip(before_rows, before_counted, strict=True):
                if row.get(name) is not None and counted:
                    delta[_stable_value_text(row[name], data_type)] -= 1
            for row, counted in zip(after_rows, after_counted, strict=True):
                if row.get(name) is not None and counted:
                    delta[_stable_value_text(row[name], data_type)] += 1
            await repository.apply_deltas(
                table_id=state.table_id,
                column_id=column_id,
                frequency_version=state.frequency_version,
                deltas={value: amount for value, amount in delta.items() if amount},
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
        await self.apply_deltas(
            table_id=table_id,
            column_id=column_id,
            frequency_version=frequency_version,
            deltas=counts,
        )

    async def apply_deltas(
        self,
        *,
        table_id: str,
        column_id: str,
        frequency_version: str,
        deltas: Mapping[str, int],
    ) -> None:
        """批量锁定并应用一个字段的规范值频次变化。"""
        effective = {value: delta for value, delta in deltas.items() if delta}
        if not effective:
            return
        hashes = {_value_hash(value): value for value in effective}
        rows = (
            (
                await self._session.execute(
                    select(metadata_value_frequency)
                    .where(
                        metadata_value_frequency.c.table_id == table_id,
                        metadata_value_frequency.c.column_id == column_id,
                        metadata_value_frequency.c.frequency_version
                        == frequency_version,
                        metadata_value_frequency.c.value_hash.in_(hashes),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        existing = {str(row["value_hash"]): row for row in rows}
        for value_hash, value_text in hashes.items():
            row = existing.get(value_hash)
            delta = effective[value_text]
            if row is None and delta < 0:
                raise RuntimeError("字段值精确频次出现未应用事件导致的负数")
            if row is not None:
                if str(row["value_text"]) != value_text:
                    raise RuntimeError("字段值哈希碰撞")
                if int(row["frequency"]) + delta < 0:
                    raise RuntimeError("字段值精确频次不能为负数")
        positive = {
            value_hash: value_text
            for value_hash, value_text in hashes.items()
            if effective[value_text] > 0
        }
        if positive:
            statement = insert(metadata_value_frequency).values(
                [
                    {
                        "table_id": table_id,
                        "column_id": column_id,
                        "frequency_version": frequency_version,
                        "value_hash": value_hash,
                        "value_text": value_text,
                        "frequency": effective[value_text],
                    }
                    for value_hash, value_text in sorted(positive.items())
                ]
            )
            await self._session.execute(
                statement.on_duplicate_key_update(
                    frequency=metadata_value_frequency.c.frequency
                    + statement.inserted.frequency
                )
            )
        negative = {
            value_hash: effective[value_text]
            for value_hash, value_text in hashes.items()
            if effective[value_text] < 0
        }
        if negative:
            await self._session.execute(
                update(metadata_value_frequency)
                .where(
                    metadata_value_frequency.c.table_id == table_id,
                    metadata_value_frequency.c.column_id == column_id,
                    metadata_value_frequency.c.frequency_version
                    == frequency_version,
                    metadata_value_frequency.c.value_hash.in_(negative),
                )
                .values(
                    frequency=metadata_value_frequency.c.frequency
                    + case(
                        negative,
                        value=metadata_value_frequency.c.value_hash,
                    )
                )
            )
        await self._session.execute(
            delete(metadata_value_frequency).where(
                metadata_value_frequency.c.table_id == table_id,
                metadata_value_frequency.c.column_id == column_id,
                metadata_value_frequency.c.frequency_version == frequency_version,
                metadata_value_frequency.c.value_hash.in_(hashes),
                metadata_value_frequency.c.frequency == 0,
            )
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
        values: list[dict[str, object]] = []
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
            # 超过 Elasticsearch 单文档预算的值是确定性的不可索引输入，
            # 不应进入 publication 后作为远程失败反复重试。未写入本代
            # membership 也会让曾发布的同值文档在 CLEANUP 中收敛删除。
            if not metadata_value_projection_fits_bulk(projection):
                continue
            payload = projection.model_dump(mode="json")
            payload_hash = metadata_desired_version(payload)
            document_id = metadata_value_document_id(
                item.object_id,
                column_id,
                str(row["value_hash"]),
            )
            values.append(
                {
                    "table_id": item.object_id,
                    "index_generation": item.index_generation,
                    "document_id": document_id,
                    "column_id": column_id,
                    "value_hash": row["value_hash"],
                    "value_text": row["value_text"],
                    "schema_fingerprint": plan.desired.schema_fingerprint,
                    "desired_membership_version": item.desired_version,
                    "desired_frequency": row["frequency"],
                    "desired_payload_hash": payload_hash,
                }
            )
        batch_size = app_config.metadata_index.value_bulk_batch_size
        for start in range(0, len(values), batch_size):
            statement = insert(metadata_value_publication).values(
                values[start : start + batch_size]
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
        document_ids = await self._publication_candidate_ids(item, "upsert")
        rows = await self._publication_candidates(item, document_ids)
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

    def _publication_predicates(
        self,
        item: ClaimedMetadataIndexWork,
        action: str,
    ) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
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
        return membership, predicate

    async def _publication_candidate_ids(
        self,
        item: ClaimedMetadataIndexWork,
        action: str,
    ) -> list[str]:
        """先读取轻量候选并按字节预算选择本 claim 的稳定文档集合。"""
        membership, predicate = self._publication_predicates(item, action)
        columns: list[ColumnElement[object]] = [
            metadata_value_publication.c.document_id
        ]
        if action == "upsert":
            columns.append(
                (
                    func.octet_length(metadata_value_publication.c.value_text)
                    + func.coalesce(
                        func.octet_length(
                            metadata_value_publication.c.action_payload_json
                        ),
                        0,
                    )
                    + 2048
                ).label("estimated_bytes")
            )
        rows = (
            (
                await self._session.execute(
                    select(*columns)
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
        document_ids: list[str] = []
        payload_bytes = 0
        for row in rows:
            estimated_bytes = int(row.get("estimated_bytes", 0))
            if (
                document_ids
                and payload_bytes + estimated_bytes > _ACTION_PAYLOAD_BYTE_LIMIT
            ):
                break
            payload_bytes += estimated_bytes
            document_ids.append(str(row["document_id"]))
        return document_ids

    async def _publication_candidates(
        self,
        item: ClaimedMetadataIndexWork,
        document_ids: Sequence[str],
    ) -> list[Mapping[str, object]]:
        if not document_ids:
            return []
        rows = (
            (
                await self._session.execute(
                    select(metadata_value_publication)
                    .where(
                        metadata_value_publication.c.table_id == item.object_id,
                        metadata_value_publication.c.index_generation
                        == item.index_generation,
                        metadata_value_publication.c.document_id.in_(document_ids),
                    )
                    .order_by(metadata_value_publication.c.document_id)
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
        document_ids = await self._publication_candidate_ids(item, "delete")
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

    async def delete_stale_frequency_batch(
        self,
        item: ClaimedMetadataIndexWork,
    ) -> int:
        """删除一个有界批次的旧频次代次，并以已删除行数表示进度。"""
        if item.frequency_version is None:
            raise ValueError("字段值刷新缺少 frequency_version")
        pending_frequency_version = await self._session.scalar(
            select(metadata_index_outbox.c.pending_frequency_version).where(
                metadata_index_outbox.c.target == item.target.value,
                metadata_index_outbox.c.object_kind == item.object_kind.value,
                metadata_index_outbox.c.object_id == item.object_id,
                metadata_index_outbox.c.desired_version == item.desired_version,
                metadata_index_outbox.c.lease_token == item.lease_token,
            )
        )
        retained_versions = [item.frequency_version]
        if pending_frequency_version is not None:
            retained_versions.append(str(pending_frequency_version))
        identity_columns = (
            metadata_value_frequency.c.table_id,
            metadata_value_frequency.c.column_id,
            metadata_value_frequency.c.frequency_version,
            metadata_value_frequency.c.value_hash,
        )
        rows = (
            (
                await self._session.execute(
                    select(*identity_columns)
                    .where(
                        metadata_value_frequency.c.table_id == item.object_id,
                        metadata_value_frequency.c.frequency_version.not_in(
                            retained_versions
                        ),
                    )
                    .order_by(
                        metadata_value_frequency.c.frequency_version,
                        metadata_value_frequency.c.column_id,
                        metadata_value_frequency.c.value_hash,
                    )
                    .limit(app_config.metadata_index.value_scan_batch_size)
                    .with_for_update()
                )
            )
            .tuples()
            .all()
        )
        if not rows:
            return 0
        await self._session.execute(
            delete(metadata_value_frequency).where(
                tuple_(*identity_columns).in_(rows)
            )
        )
        return len(rows)


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
                # 当前权威 plan 始终反映最新结构。先在锁内提升 pending 代次，
                # 避免拿新 plan 解码旧 schema 的持久化游标而无限 defer。
                if await outbox.promote_pending_value_state(item):
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
                # 当前权威 plan 可能已经切换到 pending 结构代次；必须在
                # 解释旧字段进度前提升，否则删除字段会令 index() 永久失败。
                if await outbox.promote_pending_value_state(item):
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
                    deleted = await MetadataValueFrequencyRepository(
                        session
                    ).delete_stale_frequency_batch(item)
                    if deleted:
                        await outbox.advance_value_state(
                            item,
                            phase=MetadataValueRefreshPhase.CLEANUP,
                            bulk_cursor={
                                "phase": MetadataValueRefreshPhase.CLEANUP.value,
                                "desired_version": item.desired_version,
                                "frequency_version": item.frequency_version,
                                "deleted_frequency_rows": deleted,
                            },
                        )
                        return
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
