"""从权威 Meta 与 DW 构造当前索引投影。"""

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from data_sync.models import DesiredSyncTable, SyncPhase, encode_row_value
from data_sync.tables import data_sync_task
from ddl_metadata.meta_projection.application.contracts import (
    ProjectionNotReadyError,
)
from ddl_metadata.meta_projection.models import (
    MetadataCandidate,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataSemanticHit,
    MetadataSemanticProjection,
    MetadataValueCandidate,
    MetadataValueProjection,
    MetadataValueRefreshPhase,
)
from ddl_metadata.meta_projection.repository import metadata_desired_version
from ddl_metadata.meta_projection.tables import metadata_index_outbox
from ddl_metadata.persistence.tables import (
    column_info,
    column_metric,
    metric_info,
    table_info,
)
from models.semantic import ColumnValueIndexProfile
from settings import app_config


def _pending_value_scope_statement(table_ids: set[str]) -> Select[tuple[str]]:
    """构造字段表及全局重建的待处理状态查询。"""
    return select(metadata_index_outbox.c.object_id).where(
        metadata_index_outbox.c.target == MetadataIndexTarget.VALUES.value,
        or_(
            (
                metadata_index_outbox.c.object_id.in_(table_ids)
                & (
                    metadata_index_outbox.c.phase
                    != MetadataValueRefreshPhase.COMPLETE.value
                )
            ),
            metadata_index_outbox.c.operation == MetadataIndexOperation.REBUILD.value,
        ),
    )


@dataclass(frozen=True)
class ValueProjectionPlan:
    """一次字段值刷新所需的稳定 DW 表与字段读取计划。"""

    desired: DesiredSyncTable
    columns: tuple[tuple[str, str, str], ...]


def _stable_value_text(value: object, data_type: str | None = None) -> str:
    """把特殊 MySQL 值转换为跨进程稳定的可检索业务文本。"""
    base_type = data_type.upper().split("(", 1)[0] if data_type else None
    if base_type == "JSON":
        decoded = (
            json.loads(value)
            if isinstance(value, (str, bytes, bytearray))
            else value
        )
        return json.dumps(
            decoded,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    if data_type and data_type.upper().startswith("BIT") and isinstance(value, bytes):
        return str(int.from_bytes(value, byteorder="big", signed=False))
    if (
        base_type == "TIME"
        and isinstance(value, timedelta)
    ):
        total_microseconds = (
            value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
        )
        sign = "-" if total_microseconds < 0 else ""
        absolute = abs(total_microseconds)
        hours, remainder = divmod(absolute, 3_600_000_000)
        minutes, remainder = divmod(remainder, 60_000_000)
        seconds, microseconds = divmod(remainder, 1_000_000)
        fraction = f".{microseconds:06d}" if microseconds else ""
        return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}{fraction}"
    encoded = encode_row_value(value)
    if isinstance(encoded, dict):
        return next(iter(encoded.values()))
    if encoded is None:
        raise TypeError("Meta 字段值投影不接受 NULL")
    return str(encoded)


def _search_text(*parts: object) -> str:
    """把名称、别名、描述和上下文规范化为单一检索文本。"""
    values: list[str] = []
    for part in parts:
        if isinstance(part, list):
            values.extend(str(item).strip() for item in part if str(item).strip())
        elif part is not None and str(part).strip():
            values.append(str(part).strip())
    return "\n".join(values)


def _safe_shared_column_names(
    peer_column_ids_by_name: dict[str, set[str]],
    eligible_peer_ids: set[str],
) -> set[str]:
    """仅保留所有来源字段均通过资格门禁的共享物理列名。"""
    return {
        name
        for name, column_ids in peer_column_ids_by_name.items()
        if column_ids and column_ids <= eligible_peer_ids
    }


def _semantic_projection(
    *,
    kind: MetadataObjectKind,
    object_id: str,
    search_text: str,
    table_id: str | None = None,
    role: str | None = None,
    data_type: str | None = None,
) -> MetadataSemanticProjection:
    """用当前权威投影内容生成可回查的稳定版本。"""
    source = {
        "kind": kind.value,
        "object_id": object_id,
        "table_id": table_id,
        "role": role,
        "data_type": data_type,
        "search_text": search_text,
    }
    return MetadataSemanticProjection(
        kind=kind,
        object_id=object_id,
        table_id=table_id,
        role=role,
        data_type=data_type,
        search_text=search_text,
        schema_fingerprint=metadata_desired_version(source),
        projection_version=app_config.metadata_index.projection_version,
    )


class MetadataProjectionRepository:
    """读取 Meta 权威对象并有界聚合 DW 字段值。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定只读或调用方事务 Session。"""
        self._session = session

    async def schema_is_authoritative(
        self, source: str, schema_fingerprint: str
    ) -> bool:
        """确认请求结构与最近一次 accepted snapshot 完全一致。"""
        from ddl_metadata.persistence.tables import physical_schema_authority

        current = await self._session.scalar(
            select(physical_schema_authority.c.schema_fingerprint).where(
                physical_schema_authority.c.source == source
            )
        )
        return current == schema_fingerprint

    async def semantic_projection(
        self,
        kind: MetadataObjectKind,
        object_id: str,
    ) -> MetadataSemanticProjection | None:
        """重建一个当前 Meta 对象的语义投影。"""
        if kind == MetadataObjectKind.TABLE:
            row = (
                (
                    await self._session.execute(
                        select(table_info).where(table_info.c.id == object_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            columns = (
                await self._session.execute(
                    select(column_info.c.name, column_info.c.description)
                    .where(column_info.c.table_id == object_id)
                    .order_by(column_info.c.id)
                )
            ).all()
            return _semantic_projection(
                kind=kind,
                object_id=object_id,
                role=row["role"],
                search_text=_search_text(
                    row["name"],
                    row["alias"],
                    row["description"],
                    [item for pair in columns for item in pair],
                ),
            )
        if kind == MetadataObjectKind.COLUMN:
            row = (
                (
                    await self._session.execute(
                        select(
                            column_info,
                            table_info.c.name.label("table_name"),
                            table_info.c.description.label("table_description"),
                        )
                        .join(table_info, table_info.c.id == column_info.c.table_id)
                        .where(column_info.c.id == object_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return _semantic_projection(
                kind=kind,
                object_id=object_id,
                table_id=str(row["table_id"]),
                role=row["role"],
                data_type=row["type"],
                search_text=_search_text(
                    row["name"],
                    row["alias"],
                    row["description"],
                    row["table_name"],
                    row["table_description"],
                ),
            )
        row = (
            (
                await self._session.execute(
                    select(metric_info).where(metric_info.c.id == object_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        related = (
            (
                await self._session.execute(
                    select(
                        column_info.c.name,
                        column_info.c.description,
                        column_info.c.table_id,
                        table_info.c.name.label("table_name"),
                    )
                    .join(column_metric, column_metric.c.column_id == column_info.c.id)
                    .join(table_info, table_info.c.id == column_info.c.table_id)
                    .where(column_metric.c.metric_id == object_id)
                    .order_by(column_info.c.id)
                )
            )
            .mappings()
            .all()
        )
        return _semantic_projection(
            kind=kind,
            object_id=object_id,
            table_id=str(row["fact_table_id"]),
            search_text=_search_text(
                row["name"],
                row["alias"],
                row["description"],
                [
                    item
                    for related_row in related
                    for item in (
                        related_row["table_name"],
                        related_row["name"],
                        related_row["description"],
                    )
                ],
            ),
        )

    async def eligible_columns(self, table_id: str) -> list[tuple[str, str]]:
        """返回当前表仍通过严格资格门禁的字段标识与物理名。"""
        rows = (
            (
                await self._session.execute(
                    select(
                        column_info.c.id,
                        column_info.c.name,
                        column_info.c.index_profile,
                    )
                    .where(column_info.c.table_id == table_id)
                    .order_by(column_info.c.id)
                )
            )
            .mappings()
            .all()
        )
        return [
            (str(row["id"]), str(row["name"]))
            for row in rows
            if ColumnValueIndexProfile.model_validate(row["index_profile"]).eligible
        ]

    async def desired_table_for_columns(
        self, column_ids: set[str]
    ) -> tuple[DesiredSyncTable, SyncPhase] | None:
        """通过稳定字段 ID 找到当前 DW generation 与阶段。"""
        rows = (
            await self._session.execute(
                select(data_sync_task.c.desired_json, data_sync_task.c.phase)
            )
        ).all()
        for payload, phase in rows:
            desired = DesiredSyncTable.model_validate(payload)
            if column_ids & {column.id for column in desired.columns}:
                return desired, SyncPhase(str(phase))
        return None

    async def _shared_target_eligible_columns(
        self,
        desired: DesiredSyncTable,
        eligible: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """共享 DW 同名字段仅在所有来源均通过资格门禁时允许聚合。"""
        names = {name for _, name in eligible}
        task_rows = (
            await self._session.execute(select(data_sync_task.c.desired_json))
        ).scalars()
        peer_column_ids_by_name: dict[str, set[str]] = {name: set() for name in names}
        for payload in task_rows:
            peer = DesiredSyncTable.model_validate(payload)
            if peer.target_table != desired.target_table:
                continue
            for column in peer.columns:
                if column.name in peer_column_ids_by_name:
                    peer_column_ids_by_name[column.name].add(column.id)
        peer_column_ids = {
            column_id
            for column_ids in peer_column_ids_by_name.values()
            for column_id in column_ids
        }
        rows = (
            await self._session.execute(
                select(column_info.c.id, column_info.c.index_profile).where(
                    column_info.c.id.in_(peer_column_ids)
                )
            )
        ).mappings()
        eligible_peer_ids = {
            str(row["id"])
            for row in rows
            if ColumnValueIndexProfile.model_validate(row["index_profile"]).eligible
        }
        safe_names = _safe_shared_column_names(
            peer_column_ids_by_name, eligible_peer_ids
        )
        return [(column_id, name) for column_id, name in eligible if name in safe_names]

    async def value_projection_plan(self, table_id: str) -> ValueProjectionPlan | None:
        """在短事务中解析字段值刷新所需的 DW 表与安全字段。"""
        eligible = await self.eligible_columns(table_id)
        if not eligible:
            return None
        resolved = await self.desired_table_for_columns({item[0] for item in eligible})
        if resolved is None:
            return None
        desired, phase = resolved
        if phase == SyncPhase.PENDING_SCHEMA:
            raise ProjectionNotReadyError("DW 表尚未完成结构物化")
        columns = await self._shared_target_eligible_columns(desired, eligible)
        data_types = {column.name: column.data_type for column in desired.columns}
        return ValueProjectionPlan(
            desired=desired,
            columns=tuple(
                sorted(
                    (
                        (column_id, name, data_types[name])
                        for column_id, name in columns
                    ),
                    key=lambda item: item[0].encode(),
                )
            ),
        )

    async def semantic_identities(
        self,
    ) -> list[tuple[MetadataObjectKind, str]]:
        """扫描当前全部 Meta 语义对象身份。"""
        identities: list[tuple[MetadataObjectKind, str]] = []
        for kind, table in (
            (MetadataObjectKind.TABLE, table_info),
            (MetadataObjectKind.COLUMN, column_info),
            (MetadataObjectKind.METRIC, metric_info),
        ):
            identities.extend(
                (kind, str(object_id))
                for object_id in await self._session.scalars(select(table.c.id))
            )
        return identities

    async def eligible_table_ids(self) -> set[str]:
        """返回至少包含一个当前合格值索引字段的 Meta 表。"""
        rows = (
            await self._session.execute(
                select(column_info.c.table_id, column_info.c.index_profile)
            )
        ).mappings()
        return {
            str(row["table_id"])
            for row in rows
            if ColumnValueIndexProfile.model_validate(row["index_profile"]).eligible
        }

    async def resolve_value_scope(
        self,
        column_ids: set[str],
    ) -> tuple[dict[str, tuple[str, str]], bool]:
        """解析合格字段的当前表、结构指纹与完整性。"""
        if not column_ids:
            return {}, False
        rows = (
            await self._session.execute(
                select(
                    column_info.c.id,
                    column_info.c.name,
                    column_info.c.table_id,
                    column_info.c.index_profile,
                ).where(column_info.c.id.in_(column_ids))
            )
        ).mappings()
        eligible = {
            str(row["id"]): (str(row["table_id"]), str(row["name"]))
            for row in rows
            if ColumnValueIndexProfile.model_validate(row["index_profile"]).eligible
        }
        task_rows = (
            await self._session.execute(
                select(data_sync_task.c.desired_json, data_sync_task.c.phase)
            )
        ).all()
        matches: dict[str, list[tuple[str, SyncPhase, DesiredSyncTable]]] = {
            column_id: [] for column_id in eligible
        }
        for payload, phase in task_rows:
            desired = DesiredSyncTable.model_validate(payload)
            for column in desired.columns:
                if column.id in matches:
                    matches[column.id].append(
                        (
                            desired.schema_fingerprint,
                            SyncPhase(str(phase)),
                            desired,
                        )
                    )
        safe_column_ids: set[str] = set()
        eligible_by_target: dict[str, list[tuple[str, str]]] = {}
        desired_by_target: dict[str, DesiredSyncTable] = {}
        for column_id, task_matches in matches.items():
            if len(task_matches) != 1:
                continue
            desired = task_matches[0][2]
            desired_by_target[desired.target_table] = desired
            eligible_by_target.setdefault(desired.target_table, []).append(
                (column_id, eligible[column_id][1])
            )
        for target_table, target_eligible in eligible_by_target.items():
            safe_column_ids.update(
                column_id
                for column_id, _ in await self._shared_target_eligible_columns(
                    desired_by_target[target_table], target_eligible
                )
            )
        resolved = {
            column_id: (eligible[column_id][0], task_matches[0][0])
            for column_id, task_matches in matches.items()
            if len(task_matches) == 1 and column_id in safe_column_ids
        }
        table_ids = {table_id for table_id, _ in resolved.values()}
        pending = set(
            await self._session.scalars(_pending_value_scope_statement(table_ids))
        )
        complete = (
            len(resolved) == len(column_ids)
            and not pending
            and all(
                task_matches[0][1] == SyncPhase.STREAMING
                for task_matches in matches.values()
                if len(task_matches) == 1
            )
        )
        target_tables = {
            task_matches[0][2].target_table
            for task_matches in matches.values()
            if len(task_matches) == 1
        }
        if complete and target_tables:
            peer_phases = []
            for payload, phase in task_rows:
                peer = DesiredSyncTable.model_validate(payload)
                if peer.target_table in target_tables:
                    peer_phases.append(SyncPhase(str(phase)))
            complete = bool(peer_phases) and all(
                phase == SyncPhase.STREAMING for phase in peer_phases
            )
        return resolved, complete

    def authoritative_value_candidates(
        self,
        projections: list[MetadataValueProjection],
        scope: dict[str, tuple[str, str]],
    ) -> list[MetadataValueCandidate]:
        """拒绝越界、过期结构或已失去资格的 ES 候选。"""
        return [
            MetadataValueCandidate(
                column_id=projection.column_id,
                table_id=projection.table_id,
                value=projection.value_text,
                frequency=projection.frequency,
            )
            for projection in projections
            if scope.get(projection.column_id)
            == (projection.table_id, projection.schema_fingerprint)
        ]

    async def authoritative_candidates(
        self,
        identities: list[MetadataSemanticHit],
        *,
        table_ids: set[str] | None = None,
        column_ids: set[str] | None = None,
    ) -> list[MetadataCandidate]:
        """按 Qdrant 顺序回读当前 Meta 对象，拒绝已删除候选。"""
        if not identities:
            return []
        scope_object_ids = {hit.object_id for hit in identities}
        if column_ids:
            scope_object_ids.update(column_ids)
            metric_rows = (
                await self._session.execute(
                    select(column_metric.c.metric_id).where(
                        column_metric.c.column_id.in_(column_ids)
                    )
                )
            ).all()
            scope_object_ids.update(str(metric_id) for (metric_id,) in metric_rows)
        if table_ids:
            scope_object_ids.update(table_ids)
        pending_rows = (
            await self._session.execute(
                select(
                    metadata_index_outbox.c.object_kind,
                    metadata_index_outbox.c.object_id,
                ).where(
                    metadata_index_outbox.c.target
                    == MetadataIndexTarget.SEMANTIC.value,
                    metadata_index_outbox.c.object_id.in_(scope_object_ids),
                )
            )
        ).all()
        pending = {
            (MetadataObjectKind(str(kind)), str(object_id))
            for kind, object_id in pending_rows
        }
        # 唯一绑定只能建立在完整收敛的作用域上。只要本次完整身份召回中存在
        # pending/retry/dead-letter 对象，就不能用剩余候选证明唯一。
        if pending:
            return []
        active_hits = [
            hit for hit in identities if (hit.kind, hit.object_id) not in pending
        ]
        projections, content = await self._semantic_candidate_rows(active_hits)
        candidates: list[MetadataCandidate] = []
        for hit in active_hits:
            kind, object_id = hit.kind, hit.object_id
            projection = projections.get((kind, object_id))
            if (
                projection is None
                or projection.schema_fingerprint != hit.schema_fingerprint
            ):
                continue
            row = content[(kind, object_id)]
            candidates.append(
                MetadataCandidate(
                    kind=kind,
                    object_id=object_id,
                    table_id=(
                        str(row["table_id"])
                        if row.get("table_id") is not None
                        else None
                    ),
                    name=str(row["name"]),
                    description=str(row.get("description") or ""),
                    related_column_ids=cast(
                        list[str], row.get("related_column_ids", [])
                    ),
                    score=hit.score,
                    matched_text=hit.matched_text,
                )
            )
        return candidates

    async def _semantic_candidate_rows(
        self,
        identities: list[MetadataSemanticHit],
    ) -> tuple[
        dict[tuple[MetadataObjectKind, str], MetadataSemanticProjection],
        dict[tuple[MetadataObjectKind, str], dict[str, object]],
    ]:
        """按对象类型批量回读投影依赖与候选展示内容。"""
        ids_by_kind = {
            kind: {hit.object_id for hit in identities if hit.kind == kind}
            for kind in MetadataObjectKind
        }
        projections: dict[
            tuple[MetadataObjectKind, str], MetadataSemanticProjection
        ] = {}
        content: dict[tuple[MetadataObjectKind, str], dict[str, object]] = {}

        table_ids = ids_by_kind[MetadataObjectKind.TABLE]
        if table_ids:
            tables = (
                await self._session.execute(
                    select(table_info).where(table_info.c.id.in_(table_ids))
                )
            ).mappings().all()
            column_rows = (
                await self._session.execute(
                    select(
                        column_info.c.table_id,
                        column_info.c.name,
                        column_info.c.description,
                    )
                    .where(column_info.c.table_id.in_(table_ids))
                    .order_by(column_info.c.id)
                )
            ).mappings().all()
            columns_by_table: dict[str, list[object]] = {}
            for row in column_rows:
                columns_by_table.setdefault(str(row["table_id"]), []).extend(
                    (row["name"], row["description"])
                )
            for row in tables:
                object_id = str(row["id"])
                key = (MetadataObjectKind.TABLE, object_id)
                projections[key] = _semantic_projection(
                    kind=key[0],
                    object_id=object_id,
                    role=row["role"],
                    search_text=_search_text(
                        row["name"], row["alias"], row["description"],
                        columns_by_table.get(object_id, []),
                    ),
                )
                content[key] = {"name": row["name"], "description": row["description"]}

        column_ids = ids_by_kind[MetadataObjectKind.COLUMN]
        if column_ids:
            rows = (
                await self._session.execute(
                    select(
                        column_info,
                        table_info.c.name.label("table_name"),
                        table_info.c.description.label("table_description"),
                    )
                    .join(table_info, table_info.c.id == column_info.c.table_id)
                    .where(column_info.c.id.in_(column_ids))
                )
            ).mappings().all()
            for row in rows:
                object_id = str(row["id"])
                table_id = str(row["table_id"])
                key = (MetadataObjectKind.COLUMN, object_id)
                projections[key] = _semantic_projection(
                    kind=key[0], object_id=object_id, table_id=table_id,
                    role=row["role"], data_type=row["type"],
                    search_text=_search_text(
                        row["name"], row["alias"], row["description"],
                        row["table_name"], row["table_description"],
                    ),
                )
                content[key] = {
                    "table_id": table_id,
                    "name": row["name"],
                    "description": row["description"],
                }

        metric_ids = ids_by_kind[MetadataObjectKind.METRIC]
        if metric_ids:
            metrics = (
                await self._session.execute(
                    select(metric_info).where(metric_info.c.id.in_(metric_ids))
                )
            ).mappings().all()
            related_rows = (
                await self._session.execute(
                    select(
                        column_metric.c.metric_id,
                        column_info.c.id.label("column_id"),
                        column_info.c.name,
                        column_info.c.description,
                        column_info.c.table_id,
                        table_info.c.name.label("table_name"),
                    )
                    .join(column_info, column_info.c.id == column_metric.c.column_id)
                    .join(table_info, table_info.c.id == column_info.c.table_id)
                    .where(column_metric.c.metric_id.in_(metric_ids))
                    .order_by(column_info.c.id)
                )
            ).mappings().all()
            related_by_metric: dict[str, list[object]] = {}
            related_ids: dict[str, list[str]] = {}
            for row in related_rows:
                metric_id = str(row["metric_id"])
                related_by_metric.setdefault(metric_id, []).extend(
                    (row["table_name"], row["name"], row["description"])
                )
                related_ids.setdefault(metric_id, []).append(str(row["column_id"]))
            for row in metrics:
                object_id = str(row["id"])
                key = (MetadataObjectKind.METRIC, object_id)
                projections[key] = _semantic_projection(
                    kind=key[0], object_id=object_id,
                    table_id=str(row["fact_table_id"]),
                    search_text=_search_text(
                        row["name"], row["alias"], row["description"],
                        related_by_metric.get(object_id, []),
                    ),
                )
                content[key] = {
                    "table_id": str(row["fact_table_id"]),
                    "name": row["name"],
                    "description": row["description"],
                    "related_column_ids": sorted(related_ids.get(object_id, [])),
                }
        return projections, content
