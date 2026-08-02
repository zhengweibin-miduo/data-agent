"""领域安全的 Mem0 风格记忆 API 服务。"""

from data_agent.errors import DataAgentError
from data_agent.identifiers import CONVERSATION_MEMORY_SOURCE, metric_id
from data_agent.identifiers import memory_uid as build_memory_uid
from data_agent.memory.application.contracts import (
    MemoryMutationLeaseProvider,
    MemoryServiceConfig,
    MemoryStore,
    StoredMemory,
)
from data_agent.memory.application.search import MemorySearchService
from data_agent.memory.domain.payloads import (
    build_memory_text,
    canonical_content_json,
    content_object_ids,
    memory_content_hash,
)
from data_agent.memory.domain.policies import category_policy
from data_agent.memory.versions import category_content_version
from data_agent.models.memory import (
    BuiltinMemoryCategory,
    MemoryActorType,
    MemoryCandidate,
    MemoryContent,
    MemoryDecision,
    MemoryDeleteResponse,
    MemoryDetail,
    MemoryHistoryPage,
    MemoryLinkType,
    MemorySearchResponse,
    MemoryStatus,
    MemoryTrust,
    MemoryUpdateResponse,
    MetricDefinitionContent,
    SemanticDecisionContent,
    UserMemoryContent,
)


class MemoryService:
    """提供 search/get/history/update/delete 且不暴露任意 add。"""

    def __init__(
        self,
        store: MemoryStore,
        search: MemorySearchService,
        leases: MemoryMutationLeaseProvider,
        config: MemoryServiceConfig,
    ) -> None:
        """绑定权威 store、搜索用例、DDL 来源租约与显式版本配置。"""
        self._store = store
        self._search = search
        self._leases = leases
        self._config = config

    async def search(
        self,
        query: str,
        source: str,
        *,
        categories: set[str] | None,
        limit: int,
    ) -> MemorySearchResponse:
        """执行有界混合检索并回查 MySQL 权威内容。"""
        return await self._search.search(
            query,
            source,
            categories=categories,
            limit=limit,
        )

    async def search_user(
        self,
        user_id: str,
        query: str,
        *,
        limit: int,
    ) -> MemorySearchResponse:
        """只搜索指定用户的跨会话长期记忆。"""
        return await self._search.search(
            query,
            CONVERSATION_MEMORY_SOURCE,
            user_id=user_id,
            categories={
                BuiltinMemoryCategory.USER_PROFILE.value,
                BuiltinMemoryCategory.USER_PREFERENCE.value,
                BuiltinMemoryCategory.USER_CONSTRAINT.value,
                BuiltinMemoryCategory.USER_BUSINESS_RULE.value,
            },
            limit=limit,
        )

    async def get(
        self,
        uid: str,
        *,
        user_id: str | None = None,
    ) -> MemoryDetail:
        """按稳定 UID 从 MySQL 读取权威详情。"""
        # 步骤一：按租户边界读取权威记录。
        memory = await self._store.get(uid, user_id=user_id)
        # 步骤二：统一把缺失或租户不匹配投影为安全的不存在错误。
        if memory is None:
            raise DataAgentError(
                "memory_not_found",
                "memory_get",
                "记忆不存在",
                http_status=404,
            )
        return memory.detail

    async def history(
        self,
        uid: str,
        *,
        user_id: str | None = None,
        offset: int,
        limit: int,
    ) -> MemoryHistoryPage:
        """读取有界只追加历史。"""
        # 步骤一：按租户边界和分页窗口读取只追加事件。
        page = await self._store.history(
            uid,
            user_id=user_id,
            offset=offset,
            limit=limit,
        )
        # 步骤二：避免通过历史接口泄露记录是否属于其他租户。
        if page is None:
            raise DataAgentError(
                "memory_not_found",
                "memory_history",
                "记忆不存在",
                http_status=404,
            )
        return page

    async def update(
        self,
        uid: str,
        content: MemoryContent,
        *,
        expected_version: int,
        user_id: str | None = None,
    ) -> MemoryUpdateResponse:
        """记录用户确认修正；DDL 修正需重处理后才应用到 Meta。"""
        # 步骤一：读取当前权威版本，并在进入写事务前快速拒绝无效状态。
        target = await self._get_stored(uid, user_id=user_id)
        if target.detail.status != MemoryStatus.ACTIVE:
            raise DataAgentError(
                "deleted_memory",
                "memory_update",
                "只能修正活动记忆",
                http_status=409,
            )
        if target.detail.record_version != expected_version:
            raise DataAgentError(
                "stale_memory",
                "memory_update",
                "目标记忆版本已发生变化",
                http_status=409,
            )
        # 步骤二：固定原类别、作用域和证据归属，并拒绝内容完全相同的修正。
        normalized = self._normalize_content(target.detail, content)
        if memory_content_hash(normalized) == target.detail.content_hash:
            raise DataAgentError(
                "unchanged_update",
                "memory_update",
                "修正内容与当前事实相同",
                http_status=409,
            )
        # 步骤三：两类修正都立即创建活动版本；用户记忆不参与 DDL 来源竞争。
        # DDL 修正额外与同来源工作流串行，
        # 并用 requires_reprocess 提示调用方重新生成 Meta。
        if user_id is not None:
            return await self._replace_memory(
                target,
                normalized,
                user_id=user_id,
                expected_version=expected_version,
            )
        async with self._leases.mutation_lease(target.detail.source):
            return await self._replace_memory(
                target,
                normalized,
                user_id=None,
                expected_version=expected_version,
            )

    async def delete(
        self,
        uid: str,
        *,
        expected_version: int,
        user_id: str | None = None,
    ) -> MemoryDeleteResponse:
        """执行可审计软删除；DDL 记忆使用来源租约，用户级记忆使用独立事务。"""
        # 步骤一：读取目标并在加锁前快速拒绝陈旧版本。
        target = await self._get_stored(uid, user_id=user_id)
        if target.detail.record_version != expected_version:
            raise DataAgentError(
                "stale_memory",
                "memory_delete",
                "目标记忆版本已发生变化",
                http_status=409,
            )
        # 步骤二：用户记忆在独立事务中锁定并复核版本，再执行带索引 DELETE
        # 期望状态的软删除，保留完整审计历史。
        if user_id is not None:
            await self._store.delete(
                uid, user_id=user_id, expected_version=expected_version
            )
            return MemoryDeleteResponse(memory_uid=uid)
        # 步骤三：DDL 记忆先获取来源租约，再在锁内复核并软删除，
        # 防止并发工作流重建覆盖删除状态。
        async with self._leases.mutation_lease(target.detail.source):
            await self._store.delete(
                uid, user_id=None, expected_version=expected_version
            )
        return MemoryDeleteResponse(memory_uid=uid)

    async def _get_stored(
        self,
        uid: str,
        *,
        user_id: str | None = None,
    ) -> StoredMemory:
        """读取内部主键与权威详情。"""
        # 步骤一：按租户边界读取内部主键和权威详情。
        memory = await self._store.get(uid, user_id=user_id)
        # 步骤二：统一隐藏缺失与跨租户访问的差异。
        if memory is None:
            raise DataAgentError(
                "memory_not_found",
                "memory_get",
                "记忆不存在",
                http_status=404,
            )
        return memory

    def _normalize_content(
        self,
        target: MemoryDetail,
        content: MemoryContent,
    ) -> MemoryContent:
        """固定原类别、作用域键、对象身份与证据归属，阻止修正迁移作用域。"""
        if type(content) is not type(target.content):
            raise DataAgentError(
                "memory_category_conflict",
                "memory_update",
                "修正内容类型必须与目标一致",
                http_status=409,
            )
        if isinstance(content, UserMemoryContent):
            if not isinstance(target.content, UserMemoryContent):
                raise DataAgentError(
                    "memory_category_conflict",
                    "memory_update",
                    "目标记忆内容类型不一致",
                    http_status=409,
                )
            return content.model_copy(
                update={
                    "supporting_user_quote": (target.content.supporting_user_quote),
                    "evidence_message_uids": (target.content.evidence_message_uids),
                    "confirmed_assistant_message_uid": (
                        target.content.confirmed_assistant_message_uid
                    ),
                }
            )
        if isinstance(content, SemanticDecisionContent):
            if content_object_ids(content) != [target.memory_key]:
                raise DataAgentError(
                    "memory_scope_conflict",
                    "memory_update",
                    "语义修正必须保留目标对象 ID",
                    http_status=409,
                )
            return content.model_copy(update={"trust": "user_confirmed"})
        if not isinstance(content, MetricDefinitionContent):
            raise DataAgentError(
                "immutable_memory_category",
                "memory_update",
                "该记忆类别不可直接修正",
                http_status=409,
            )
        if not isinstance(target.content, MetricDefinitionContent):
            raise DataAgentError(
                "memory_category_conflict",
                "memory_update",
                "目标记忆内容类型不一致",
                http_status=409,
            )
        metric = content.metric
        if (metric.id and metric.id != target.memory_key) or metric_id(
            target.source,
            metric.fact_table_id,
            metric.name,
        ) != target.memory_key:
            raise DataAgentError(
                "memory_scope_conflict",
                "memory_update",
                "指标修正必须保留事实表、名称和指标 ID",
                http_status=409,
            )
        return content.model_copy(
            update={
                "trust": "user_confirmed",
                "metric": metric.model_copy(update={"id": target.memory_key}),
                "questions": target.content.questions,
                "answers": target.content.answers,
            }
        )

    async def _replace_memory(
        self,
        target: StoredMemory,
        content: MemoryContent,
        *,
        user_id: str | None,
        expected_version: int,
    ) -> MemoryUpdateResponse:
        """以新活动版本替代旧事实并保留完整历史。"""
        # 步骤一：基于规范内容和原作用域构建内容寻址的新版本候选。
        content_json = canonical_content_json(content)
        policy = category_policy(target.detail.category)
        uid_source = (
            f"{target.detail.source}:{user_id}"
            if user_id is not None
            else target.detail.source
        )
        uid = build_memory_uid(
            uid_source,
            target.detail.category,
            target.detail.memory_key,
            target.detail.schema_fingerprint or "",
            content_json,
        )
        candidate = MemoryCandidate(
            uid=uid,
            source=target.detail.source,
            user_id=user_id,
            created_conversation_uid=target.detail.created_conversation_uid,
            created_message_uid=target.detail.created_message_uid,
            category=target.detail.category,
            memory_key=target.detail.memory_key,
            content_schema=policy.content_schema,
            schema_fingerprint=target.detail.schema_fingerprint,
            memory_text=build_memory_text(content),
            content=content,
            content_hash=memory_content_hash(content),
            trust=MemoryTrust(content.trust),
            content_version=category_content_version(target.detail.category),
            projection_version=self._config.projection_version,
            importance_score=policy.importance_score,
            lifecycle_policy=policy.lifecycle_policy,
            expires_at=target.detail.expires_at,
            supersedes_uids=[target.detail.uid],
            derived_from_uids=[
                link.linked_memory_uid
                for link in target.detail.links
                if link.link_type == MemoryLinkType.DERIVED_FROM
            ],
            related_uids=[
                link.linked_memory_uid
                for link in target.detail.links
                if link.link_type == MemoryLinkType.RELATED
            ],
            decision=MemoryDecision.UPDATE,
            actor_type=MemoryActorType.USER,
        )
        # 步骤二：锁定当前活动版本并复核状态，入口校验不能替代事务内校验。
        # 步骤二：production store 在同一短事务中锁定当前版本、验证 DDL 引用、
        # 写入新版本/历史/双目标 outbox，并返回作用域内最新事件编号。
        event_id = await self._store.replace(
            target.detail.uid,
            candidate,
            content,
            user_id=user_id,
            expected_version=expected_version,
        )
        return MemoryUpdateResponse(
            memory_uid=candidate.uid,
            event_id=event_id,
            record_version=expected_version + 1,
            requires_reprocess=user_id is None,
        )
