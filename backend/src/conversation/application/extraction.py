"""Conversation 长期记忆提炼用例。"""

from __future__ import annotations

import asyncio

from loguru import logger

from conversation.application.contracts import (
    ExtractionClaimStore,
    ExtractionCommitter,
    ExtractionModel,
)
from conversation.models import (
    ClaimedExtraction,
    ExtractionResult,
    MessageRole,
)
from identifiers import CONVERSATION_MEMORY_SOURCE, memory_uid, stable_id
from memory.domain.payloads import (
    build_memory_text,
    canonical_content_json,
    memory_content_hash,
)
from memory.domain.policies import category_policy, user_memory_category
from models.memory import (
    MemoryCandidate,
    MemoryTrust,
    UserMemoryContent,
)

_AMBIGUOUS_CONFIRMATIONS = {
    "yes",
    "ok",
    "okay",
    "sure",
    "可以",
    "好的",
    "好",
    "是",
    "对",
}


def validate_extraction_candidates(
    claim: ClaimedExtraction,
    result: ExtractionResult,
    *,
    content_version: str = "v1",
    projection_version: str = "v1",
) -> list[MemoryCandidate]:
    """用消息归属、角色、顺序和精确 quote 校验模型候选。"""
    by_uid = {message.uid: message for message in claim.messages}
    accepted: list[MemoryCandidate] = []
    accepted_scopes: set[str] = set()
    for candidate in result.candidates:
        # 步骤一：规范化候选值与逻辑键，空白模型输出不进入证据校验。
        value = candidate.value.strip()
        memory_key = candidate.key.strip().casefold()
        if not value or not memory_key:
            continue

        # 步骤二：证据 UID 必须属于同租户会话窗口，且基础证据只能引用用户消息。
        evidence = [by_uid.get(uid) for uid in candidate.evidence_message_uids]
        if any(message is None for message in evidence) or any(
            message is not None and message.role != MessageRole.USER
            for message in evidence
        ):
            continue
        user_messages = [message for message in evidence if message is not None]

        # 步骤三：支持原文必须逐字存在于用户消息，并直接包含候选值。
        quoting = [
            message
            for message in user_messages
            if candidate.supporting_user_quote in message.content
        ]
        if (
            not quoting
            or value.casefold() not in candidate.supporting_user_quote.casefold()
        ):
            continue

        # 步骤四：助手结论还需精确助手原文及时间更晚的用户明确复述。
        assistant_uid = candidate.confirmed_assistant_message_uid
        if assistant_uid is not None:
            assistant = by_uid.get(assistant_uid)
            if (
                assistant is None
                or assistant.role != MessageRole.ASSISTANT
                or not candidate.assistant_quote
                or candidate.assistant_quote not in assistant.content
                or not any(message.id > assistant.id for message in quoting)
                or not any(
                    candidate.assistant_quote.casefold() in message.content.casefold()
                    for message in quoting
                )
                or candidate.supporting_user_quote.strip().casefold()
                in _AMBIGUOUS_CONFIRMATIONS
            ):
                continue
        elif candidate.assistant_quote is not None:
            continue

        content = UserMemoryContent(
            value=value,
            supporting_user_quote=candidate.supporting_user_quote,
            evidence_message_uids=candidate.evidence_message_uids,
            confirmed_assistant_message_uid=assistant_uid,
        )
        # 步骤五：同批结果按类别和规范化键占用一个逻辑作用域。
        category = user_memory_category(candidate.category)
        logical_key = f"{category}:{memory_key}"
        if logical_key in accepted_scopes:
            continue
        accepted_scopes.add(logical_key)
        fingerprint = stable_id("user-memory-scope", claim.user_id, logical_key)
        policy = category_policy(category)
        content_json = canonical_content_json(content)

        # 步骤六：证据与作用域均通过后构建带 Conversation 来源的 Memory proposal。
        accepted.append(
            MemoryCandidate(
                uid=memory_uid(
                    f"{CONVERSATION_MEMORY_SOURCE}:{claim.user_id}",
                    category,
                    memory_key,
                    fingerprint,
                    content_json,
                ),
                source=CONVERSATION_MEMORY_SOURCE,
                user_id=claim.user_id,
                created_conversation_uid=claim.conversation_uid,
                created_message_uid=quoting[0].uid,
                category=category,
                memory_key=memory_key,
                content_schema=policy.content_schema,
                schema_fingerprint=None,
                memory_text=build_memory_text(content),
                content=content,
                content_hash=memory_content_hash(content),
                trust=MemoryTrust.USER_CONFIRMED,
                content_version=content_version,
                projection_version=projection_version,
                importance_score=policy.importance_score,
                lifecycle_policy=policy.lifecycle_policy,
            )
        )
    return accepted


class ConversationMemoryExtractor:
    """通过注入端口领取、提炼并原子提交 Conversation Memory。"""

    def __init__(
        self,
        model: ExtractionModel,
        claims: ExtractionClaimStore,
        committer: ExtractionCommitter,
        *,
        batch_size: int,
        max_concurrency: int,
        lease_seconds: int,
        message_limit: int,
        summary_max_chars: int,
        content_version: str,
        projection_version: str,
    ) -> None:
        """绑定提炼边界和显式运行预算。"""
        self._model = model
        self._claims = claims
        self._committer = committer
        self._batch_size = batch_size
        self._max_concurrency = max_concurrency
        self._lease_seconds = lease_seconds
        self._message_limit = message_limit
        self._summary_max_chars = summary_max_chars
        self._content_version = content_version
        self._projection_version = projection_version

    async def dispatch(self) -> int:
        """处理一个有界提炼批次。"""
        processed = 0
        remaining = self._batch_size
        while remaining > 0:
            # 步骤一：短事务 claim 提交租约后，才进入事务外模型调用。
            claims = await self._claims.claim(
                limit=min(remaining, self._max_concurrency),
                lease_seconds=self._lease_seconds,
                message_limit=self._message_limit,
            )
            if not claims:
                break
            # 步骤二：并发处理本波 claim，并按领取数消耗批次预算。
            results = await asyncio.gather(
                *(self._process_claim(claim) for claim in claims)
            )
            processed += sum(results)
            remaining -= len(claims)
        return processed

    async def _process_claim(self, claim: ClaimedExtraction) -> int:
        """处理一条 claim，并在失败时通过 claim store 登记退避。"""
        try:
            # 步骤一：模型调用和证据校验均在数据库事务外完成。
            result = await self._model.extract(claim)
            candidates = validate_extraction_candidates(
                claim,
                result,
                content_version=self._content_version,
                projection_version=self._projection_version,
            )
            # 步骤二：outer committer 在一个新事务内提交 Memory 与 Conversation。
            await self._committer.commit(
                claim,
                candidates,
                result.summary[: self._summary_max_chars],
            )
            return 1
        except Exception as error:  # noqa: BLE001
            # 步骤三：失败不改变消息，按 lease authority 释放任务并退避。
            await self._claims.retry(claim, type(error).__name__)
            logger.warning("对话长期记忆提炼失败，任务已释放租约并将在退避后自动重试")
            return 0
