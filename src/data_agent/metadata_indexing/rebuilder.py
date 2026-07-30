"""从 Meta 与 DW 重新投递项目专用派生索引。"""

from uuid import uuid4

from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.metadata_indexing.elasticsearch import (
    MetadataValueElasticsearchIndex,
)
from data_agent.metadata_indexing.models import (
    MetadataIndexDesired,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataRebuildResult,
)
from data_agent.metadata_indexing.projections import MetadataProjectionRepository
from data_agent.metadata_indexing.qdrant import MetadataQdrantIndex
from data_agent.metadata_indexing.repository import (
    MetadataIndexOutboxRepository,
    metadata_desired_version,
)
from data_agent.settings import app_config


class MetadataIndexRebuilder:
    """重建两个配置明确指定的 Meta 派生索引。"""

    async def reset_indexes(
        self,
        *,
        confirmed_es_index: str,
        confirmed_qdrant_collection: str,
    ) -> None:
        """逐字确认目标后，仅重建项目自己的两个索引。"""
        # 步骤一：防止调用方误删其他项目资源。
        expected = (
            app_config.elasticsearch.metadata_value_index,
            app_config.qdrant.metadata_collection,
        )
        if (confirmed_es_index, confirmed_qdrant_collection) != expected:
            raise ValueError(
                "索引重建目标确认不匹配: "
                f"Elasticsearch={expected[0]}, Qdrant={expected[1]}"
            )
        # 步骤二：只持久化两个幂等重建阶段。dispatcher 完成各后端 recreate
        # 后才确认对应行；任一阶段中断都会在重启后重新执行，而不会只补写现存对象。
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
        async with MySQLDatabase.session() as session:
            await MetadataIndexOutboxRepository(session).enqueue(desired)

    async def rebuild_target(self, target: MetadataIndexTarget) -> None:
        """幂等重建单个后端，并在成功后投递该后端的当前权威对象。"""
        if target == MetadataIndexTarget.VALUES:
            await MetadataValueElasticsearchIndex(
                ElasticsearchClient.get_client()
            ).recreate()
        else:
            await MetadataQdrantIndex(QdrantClient.get_client()).recreate()
        try:
            await self.enqueue(target=target)
        except Exception as error:
            raise RebuildProjectionError from error

    async def enqueue(
        self, target: MetadataIndexTarget | None = None
    ) -> MetadataRebuildResult:
        """扫描当前 Meta，投递全部语义对象和合格字段表刷新。"""
        # 步骤一：在一个事务中扫描权威身份并合并重建 desired state。
        rebuild_generation = uuid4().hex
        async with MySQLDatabase.session() as session:
            projections = MetadataProjectionRepository(session)
            identities = await projections.semantic_identities()
            table_ids = await projections.eligible_table_ids()
            desired = [
                MetadataIndexDesired(
                    target=MetadataIndexTarget.SEMANTIC,
                    object_kind=kind,
                    object_id=object_id,
                    operation=MetadataIndexOperation.UPSERT,
                    desired_version=metadata_desired_version(
                        {
                            "rebuild": True,
                            "rebuild_generation": rebuild_generation,
                            "projection_version": (
                                app_config.metadata_index.projection_version
                            ),
                            "kind": kind.value,
                            "object_id": object_id,
                        }
                    ),
                )
                for kind, object_id in identities
                if target in (None, MetadataIndexTarget.SEMANTIC)
            ]
            desired.extend(
                MetadataIndexDesired(
                    target=MetadataIndexTarget.VALUES,
                    object_kind=MetadataObjectKind.TABLE,
                    object_id=table_id,
                    operation=MetadataIndexOperation.REFRESH,
                    desired_version=metadata_desired_version(
                        {
                            "rebuild": True,
                            "rebuild_generation": rebuild_generation,
                            "projection_version": (
                                app_config.metadata_index.projection_version
                            ),
                            "table_id": table_id,
                        }
                    ),
                )
                for table_id in sorted(table_ids)
                if target in (None, MetadataIndexTarget.VALUES)
            )
            await MetadataIndexOutboxRepository(session).enqueue(desired)
        # 步骤二：返回本次可观察投递规模，不等待外部索引完成。
        return MetadataRebuildResult(
            semantic_objects=len(identities),
            value_tables=len(table_ids),
        )


class RebuildProjectionError(RuntimeError):
    """后端重建后的权威扫描失败，不应消耗远程失败预算。"""
