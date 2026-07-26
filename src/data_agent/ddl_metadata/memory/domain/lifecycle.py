"""长期记忆候选的确定性生命周期决策。"""

from data_agent.ddl_metadata.models.memory import (
    MemoryContent,
    MemoryDecision,
    UserMemoryContent,
)


def semantically_equivalent(
    current: MemoryContent,
    candidate: MemoryContent,
) -> bool:
    """比较结构化事实含义，忽略用户事实的证据消息差异。"""
    if isinstance(current, UserMemoryContent) and isinstance(
        candidate, UserMemoryContent
    ):
        current_value = " ".join(current.value.split()).casefold()
        candidate_value = " ".join(candidate.value.split()).casefold()
        return current_value == candidate_value
    return current == candidate


def decide_memory(
    *,
    has_active: bool,
    same_content: bool = False,
    delete_requested: bool = False,
    merge_requested: bool = False,
) -> MemoryDecision:
    """在模型辅助比较前完成可确定的五类决策。"""
    # 先裁决删除、空槽位和幂等分支，避免 tombstone 或同内容被误判为新增版本。
    if delete_requested:
        return MemoryDecision.DELETE if has_active else MemoryDecision.NOOP
    if not has_active:
        return MemoryDecision.ADD
    if same_content:
        return MemoryDecision.NOOP
    if merge_requested:
        return MemoryDecision.MERGE
    return MemoryDecision.UPDATE
