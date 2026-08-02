"""Meta 派生索引的可恢复重建用例。"""

from collections.abc import Sequence
from uuid import uuid4

from ddl_metadata.meta_projection.application.contracts import (
    ProjectionReader,
    ProjectionWorkStore,
    RebuildProjectionError,
    SemanticIndex,
    ValueIndex,
)
from ddl_metadata.meta_projection.domain import metadata_desired_version
from ddl_metadata.meta_projection.models import (
    MetadataIndexDesired,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataRebuildResult,
)


class MetadataIndexRebuilder:
    """先持久化恢复阶段，再重建并重新投递当前权威对象。"""

    def __init__(
        self,
        *,
        work_store: ProjectionWorkStore,
        reader: ProjectionReader,
        semantic_index: SemanticIndex,
        value_index: ValueIndex,
        projection_version: str,
        es_index: str,
        qdrant_collection: str,
    ) -> None:
        """绑定持久化、权威读取、索引端口与显式配置值。"""
        self._work_store = work_store
        self._reader = reader
        self._semantic_index = semantic_index
        self._value_index = value_index
        self._projection_version = projection_version
        self._es_index = es_index
        self._qdrant_collection = qdrant_collection

    async def reset_indexes(
        self,
        *,
        confirmed_es_index: str,
        confirmed_qdrant_collection: str,
    ) -> None:
        """逐字确认目标并持久化两个可重放的重建阶段。"""
        expected = (self._es_index, self._qdrant_collection)
        if (confirmed_es_index, confirmed_qdrant_collection) != expected:
            raise ValueError(
                "索引重建目标确认不匹配: "
                f"Elasticsearch={expected[0]}, Qdrant={expected[1]}"
            )
        generation = uuid4().hex
        desired = [
            MetadataIndexDesired(
                target=target,
                object_kind=MetadataObjectKind.TABLE,
                object_id="__rebuild__",
                operation=MetadataIndexOperation.REBUILD,
                desired_version=metadata_desired_version(
                    {"rebuild_generation": generation, "target": target.value}
                ),
            )
            for target in MetadataIndexTarget
        ]
        await self._work_store.enqueue(desired)

    async def rebuild_target(self, target: MetadataIndexTarget) -> None:
        """幂等重建单个后端，并在成功后投递当前权威对象。"""
        if target == MetadataIndexTarget.VALUES:
            await self._value_index.recreate()
        else:
            await self._semantic_index.recreate()
        try:
            await self.enqueue(target=target)
        except Exception as error:
            raise RebuildProjectionError from error

    async def enqueue(
        self,
        target: MetadataIndexTarget | None = None,
    ) -> MetadataRebuildResult:
        """扫描当前 Meta，并投递语义对象与合格字段表刷新。"""
        rebuild_generation = uuid4().hex
        identities = await self._reader.semantic_identities()
        table_ids = await self._reader.eligible_table_ids()
        desired: list[MetadataIndexDesired] = [
            MetadataIndexDesired(
                target=MetadataIndexTarget.SEMANTIC,
                object_kind=kind,
                object_id=object_id,
                operation=MetadataIndexOperation.UPSERT,
                desired_version=metadata_desired_version(
                    {
                        "rebuild": True,
                        "rebuild_generation": rebuild_generation,
                        "projection_version": self._projection_version,
                        "kind": kind.value,
                        "object_id": object_id,
                    }
                ),
            )
            for kind, object_id in identities
            if target in (None, MetadataIndexTarget.SEMANTIC)
        ]
        desired.extend(self._value_desired(table_ids, target, rebuild_generation))
        await self._work_store.enqueue(desired)
        return MetadataRebuildResult(
            semantic_objects=len(identities),
            value_tables=len(table_ids),
        )

    def _value_desired(
        self,
        table_ids: Sequence[str] | set[str],
        target: MetadataIndexTarget | None,
        rebuild_generation: str,
    ) -> list[MetadataIndexDesired]:
        """构造确定性的 VALUES 重建 desired state。"""
        if target not in (None, MetadataIndexTarget.VALUES):
            return []
        return [
            MetadataIndexDesired(
                target=MetadataIndexTarget.VALUES,
                object_kind=MetadataObjectKind.TABLE,
                object_id=table_id,
                operation=MetadataIndexOperation.REFRESH,
                desired_version=metadata_desired_version(
                    {
                        "rebuild": True,
                        "rebuild_generation": rebuild_generation,
                        "projection_version": self._projection_version,
                        "table_id": table_id,
                    }
                ),
                frequency_version=metadata_desired_version(
                    {
                        "rebuild_frequency_generation": rebuild_generation,
                        "projection_version": self._projection_version,
                        "table_id": table_id,
                        "normalization_version": 1,
                    }
                ),
            )
            for table_id in sorted(table_ids)
        ]
