"""长期记忆类别与生命周期策略。"""

from dataclasses import dataclass
from typing import Literal

from data_agent.models.memory import (
    BuiltinMemoryCategory,
    MemoryCandidate,
    MemoryContent,
    MemoryLifecyclePolicy,
    MetricDefinitionContent,
    SemanticDecisionContent,
    UserMemoryCategory,
    UserMemoryContent,
)


@dataclass(frozen=True)
class MemoryCategoryPolicy:
    """一个内置记忆类别的最小写入与检索策略。"""

    content_schema: str
    lifecycle_policy: MemoryLifecyclePolicy
    importance_score: float
    retrieval_weight: float
    content_types: tuple[type[MemoryContent], ...]
    user_scoped: bool = False
    conflict_strategy: Literal["replace", "merge"] = "replace"


_POLICIES = {
    BuiltinMemoryCategory.DDL_SEMANTIC.value: MemoryCategoryPolicy(
        "ddl.semantic.v1",
        MemoryLifecyclePolicy.FINGERPRINT_BOUND,
        0.8,
        1.0,
        (SemanticDecisionContent,),
    ),
    BuiltinMemoryCategory.DDL_METRIC.value: MemoryCategoryPolicy(
        "ddl.metric.v1",
        MemoryLifecyclePolicy.FINGERPRINT_BOUND,
        0.9,
        1.1,
        (MetricDefinitionContent,),
        conflict_strategy="merge",
    ),
    BuiltinMemoryCategory.USER_PROFILE.value: MemoryCategoryPolicy(
        "user.text_fact.v1",
        MemoryLifecyclePolicy.ADAPTIVE,
        0.6,
        0.9,
        (UserMemoryContent,),
        user_scoped=True,
    ),
    BuiltinMemoryCategory.USER_PREFERENCE.value: MemoryCategoryPolicy(
        "user.text_fact.v1",
        MemoryLifecyclePolicy.ADAPTIVE,
        0.7,
        1.1,
        (UserMemoryContent,),
        user_scoped=True,
    ),
    BuiltinMemoryCategory.USER_CONSTRAINT.value: MemoryCategoryPolicy(
        "user.text_fact.v1",
        MemoryLifecyclePolicy.PERMANENT,
        0.9,
        1.2,
        (UserMemoryContent,),
        user_scoped=True,
    ),
    BuiltinMemoryCategory.USER_BUSINESS_RULE.value: MemoryCategoryPolicy(
        "user.text_fact.v1",
        MemoryLifecyclePolicy.PERMANENT,
        0.9,
        1.2,
        (UserMemoryContent,),
        user_scoped=True,
    ),
}

_USER_CATEGORY = {
    UserMemoryCategory.PROFILE: BuiltinMemoryCategory.USER_PROFILE.value,
    UserMemoryCategory.PREFERENCE: BuiltinMemoryCategory.USER_PREFERENCE.value,
    UserMemoryCategory.CONSTRAINT: BuiltinMemoryCategory.USER_CONSTRAINT.value,
    UserMemoryCategory.BUSINESS_RULE: BuiltinMemoryCategory.USER_BUSINESS_RULE.value,
}


def category_policy(category: str) -> MemoryCategoryPolicy:
    """返回已注册类别策略；未知类别拒绝写入。"""
    try:
        return _POLICIES[category]
    except KeyError as exc:
        raise ValueError(f"未注册的长期记忆类别: {category}") from exc


def user_memory_category(category: UserMemoryCategory) -> str:
    """把提取契约类别映射为可扩展持久化类别。"""
    return _USER_CATEGORY[category]


def validate_candidate_policy(candidate: MemoryCandidate) -> None:
    """在数据库写入前强制类别策略与候选保持一致。"""
    policy = category_policy(candidate.category)
    if candidate.content_schema != policy.content_schema:
        raise ValueError("长期记忆 content_schema 与类别策略不一致")
    if candidate.lifecycle_policy != policy.lifecycle_policy:
        raise ValueError("长期记忆 lifecycle_policy 与类别策略不一致")
    if candidate.importance_score != policy.importance_score:
        raise ValueError("长期记忆 importance_score 与类别策略不一致")
    if not isinstance(candidate.content, policy.content_types):
        raise ValueError("长期记忆内容类型不属于指定类别")
    if policy.user_scoped != (candidate.user_id is not None):
        raise ValueError("长期记忆作用域与类别策略不一致")
    if (
        policy.lifecycle_policy == MemoryLifecyclePolicy.FINGERPRINT_BOUND
        and not candidate.schema_fingerprint
    ):
        raise ValueError("指纹绑定长期记忆缺少 schema_fingerprint")
    if (
        policy.lifecycle_policy == MemoryLifecyclePolicy.EXPIRING
        and candidate.expires_at is None
    ):
        raise ValueError("到期长期记忆缺少 expires_at")
