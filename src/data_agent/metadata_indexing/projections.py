"""从权威 Meta 与 DW 构造当前索引投影。"""

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.data_sync.models import DesiredSyncTable, SyncPhase
from data_agent.data_sync.tables import data_sync_task
from data_agent.ddl_metadata.persistence.tables import (
    column_info,
    column_metric,
    metric_info,
    table_info,
)
from data_agent.metadata_indexing.models import (
    MetadataCandidate,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataSemanticHit,
    MetadataSemanticProjection,
    MetadataValueCandidate,
    MetadataValueProjection,
)
from data_agent.metadata_indexing.repository import metadata_desired_version
from data_agent.metadata_indexing.tables import metadata_index_outbox
from data_agent.models.semantic import ColumnValueIndexProfile
from data_agent.settings import app_config


class ProjectionNotReadyError(RuntimeError):
    """权威 DW 投影尚未物化，任务应无损延后。"""


@dataclass(frozen=True)
class ValueProjectionPlan:
    """一次字段值刷新所需的稳定 DW 表与字段读取计划。"""

    desired: DesiredSyncTable
    columns: tuple[tuple[str, str], ...]


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
    """仅保留来源归属唯一且通过资格门禁的物理列名。"""
    return {
        name
        for name, column_ids in peer_column_ids_by_name.items()
        if len(column_ids) == 1 and column_ids <= eligible_peer_ids
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
            table_id=str(related[0]["table_id"]) if related else None,
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
                    ).where(column_info.c.table_id == table_id)
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
        return ValueProjectionPlan(desired=desired, columns=tuple(columns))

    async def value_projection_batch(
        self,
        table_id: str,
        refresh_version: str,
        plan: ValueProjectionPlan,
        column: tuple[str, str],
    ) -> list[MetadataValueProjection]:
        """在单个短事务中物化一个字段的有界 top-N 投影。"""
        column_id, name = column
        quote = mysql_dialect().identifier_preparer.quote
        qualified = (
            f"{quote(app_config.data_sync.dw_database)}."
            f"{quote(plan.desired.target_table)}"
        )
        quoted = quote(name)
        rows = await self._session.execute(
            text(
                f"SELECT {quoted} AS value, COUNT(*) AS frequency "
                f"FROM {qualified} WHERE {quoted} IS NOT NULL "
                f"GROUP BY {quoted} ORDER BY frequency DESC, {quoted} "
                "LIMIT :limit"
            ),
            {"limit": app_config.metadata_index.value_top_n},
        )
        return [
            MetadataValueProjection(
                column_id=column_id,
                table_id=table_id,
                value_text=str(value),
                value_keyword=str(value),
                frequency=int(frequency),
                refresh_version=refresh_version,
                schema_fingerprint=plan.desired.schema_fingerprint,
            )
            for value, frequency in rows
        ]

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
            await self._session.scalars(
                select(metadata_index_outbox.c.object_id).where(
                    metadata_index_outbox.c.target == MetadataIndexTarget.VALUES.value,
                    metadata_index_outbox.c.object_id.in_(table_ids),
                )
            )
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
    ) -> list[MetadataCandidate]:
        """按 Qdrant 顺序回读当前 Meta 对象，拒绝已删除候选。"""
        if not identities:
            return []
        pending_rows = (
            await self._session.execute(
                select(
                    metadata_index_outbox.c.object_kind,
                    metadata_index_outbox.c.object_id,
                ).where(
                    metadata_index_outbox.c.target
                    == MetadataIndexTarget.SEMANTIC.value,
                    metadata_index_outbox.c.object_id.in_(
                        {hit.object_id for hit in identities}
                    ),
                )
            )
        ).all()
        pending = {
            (MetadataObjectKind(str(kind)), str(object_id))
            for kind, object_id in pending_rows
        }
        candidates: list[MetadataCandidate] = []
        for hit in identities:
            kind, object_id = hit.kind, hit.object_id
            schema_fingerprint = hit.schema_fingerprint
            if (kind, object_id) in pending:
                continue
            projection = await self.semantic_projection(kind, object_id)
            if (
                projection is None
                or projection.schema_fingerprint != schema_fingerprint
            ):
                continue
            if kind == MetadataObjectKind.TABLE:
                row = (
                    await self._session.execute(
                        select(table_info.c.name, table_info.c.description).where(
                            table_info.c.id == object_id
                        )
                    )
                ).one_or_none()
                if row:
                    candidates.append(
                        MetadataCandidate(
                            kind=kind,
                            object_id=object_id,
                            name=str(row.name),
                            description=str(row.description or ""),
                            score=hit.score,
                            matched_text=hit.matched_text,
                        )
                    )
            elif kind == MetadataObjectKind.COLUMN:
                row = (
                    await self._session.execute(
                        select(
                            column_info.c.name,
                            column_info.c.description,
                            column_info.c.table_id,
                        ).where(column_info.c.id == object_id)
                    )
                ).one_or_none()
                if row:
                    candidates.append(
                        MetadataCandidate(
                            kind=kind,
                            object_id=object_id,
                            table_id=str(row.table_id),
                            name=str(row.name),
                            description=str(row.description or ""),
                            score=hit.score,
                            matched_text=hit.matched_text,
                        )
                    )
            else:
                rows = (
                    await self._session.execute(
                        select(
                            metric_info.c.name,
                            metric_info.c.description,
                            column_metric.c.column_id,
                        )
                        .outerjoin(
                            column_metric,
                            column_metric.c.metric_id == metric_info.c.id,
                        )
                        .where(metric_info.c.id == object_id)
                    )
                ).all()
                if rows:
                    candidates.append(
                        MetadataCandidate(
                            kind=kind,
                            object_id=object_id,
                            name=str(rows[0].name),
                            description=str(rows[0].description or ""),
                            related_column_ids=sorted(
                                str(row.column_id)
                                for row in rows
                                if row.column_id is not None
                            ),
                            score=hit.score,
                            matched_text=hit.matched_text,
                        )
                    )
        return candidates
