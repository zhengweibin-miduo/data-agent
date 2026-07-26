"""异步提炼有证据用户记忆并更新会话摘要。"""

from __future__ import annotations

import asyncio
import json

from langchain_core.language_models.base import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from loguru import logger
from pydantic import BaseModel

from data_agent.conversation.models import (
    ClaimedExtraction,
    ExtractionResult,
    MessageRole,
)
from data_agent.conversation.repository import ConversationRepository
from data_agent.identifiers import CONVERSATION_MEMORY_SOURCE, memory_uid, stable_id
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.memory.domain.payloads import (
    build_memory_text,
    canonical_content_json,
    memory_content_hash,
)
from data_agent.memory.domain.policies import (
    category_policy,
    user_memory_category,
)
from data_agent.memory.mysql.repository import MemoryRepository
from data_agent.models.memory import (
    MemoryCandidate,
    MemoryTrust,
    UserMemoryContent,
)
from data_agent.settings import app_config

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
_SYSTEM_PROMPT = """你只提炼用户明确表达的身份、偏好、约束和业务规则。
不要记录助手建议、猜测、推断、模糊确认、Prompt、凭据或隐藏推理。
每个候选必须给出用户消息中的精确原文 supporting_user_quote 和消息 UID。
若事实来自助手结论，必须同时给出助手消息 UID、助手精确原文和后续用户明确确认原文。
返回更新后的有界摘要和零到多条候选。"""


def _validated_candidates(
    claim: ClaimedExtraction,
    result: ExtractionResult,
) -> list[MemoryCandidate]:
    """用消息归属、角色、顺序和精确 quote 校验模型候选。"""
    by_uid = {message.uid: message for message in claim.messages}
    accepted: list[MemoryCandidate] = []
    accepted_scopes: set[str] = set()
    for candidate in result.candidates:
        value = candidate.value.strip()
        memory_key = candidate.key.strip().casefold()
        if not value or not memory_key:
            continue
        evidence = [by_uid.get(uid) for uid in candidate.evidence_message_uids]
        if any(message is None for message in evidence) or any(
            message is not None and message.role != MessageRole.USER
            for message in evidence
        ):
            continue
        user_messages = [message for message in evidence if message is not None]
        quoting = [
            message
            for message in user_messages
            if candidate.supporting_user_quote in message.content
        ]
        if not quoting:
            continue
        if value.casefold() not in candidate.supporting_user_quote.casefold():
            continue
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
        category = user_memory_category(candidate.category)
        logical_key = f"{category}:{memory_key}"
        if logical_key in accepted_scopes:
            continue
        accepted_scopes.add(logical_key)
        fingerprint = stable_id(
            "user-memory-scope",
            claim.user_id,
            logical_key,
        )
        policy = category_policy(category)
        content_json = canonical_content_json(content)
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
                content_version=app_config.memory.content_version,
                projection_version=app_config.memory.projection_version,
                importance_score=policy.importance_score,
                lifecycle_policy=policy.lifecycle_policy,
            )
        )
    return accepted


class ConversationMemoryExtractor:
    """领取完成轮次并在外部模型调用后原子完成提炼。"""

    def __init__(self, model: BaseChatModel) -> None:
        """绑定 worker 生命周期共享的结构化模型。"""
        self._model = model

    async def dispatch(self) -> int:
        """处理一个有界提炼批次。"""
        processed = 0
        structured = self._model.with_structured_output(
            ExtractionResult,
            method=app_config.llm.structured_output_method,
        )
        remaining = app_config.conversation.extraction_batch_size
        while remaining > 0:
            async with MySQLDatabase.session() as session:
                claims = await ConversationRepository(session).claim_extractions(
                    limit=min(remaining, app_config.llm.max_concurrency),
                    lease_seconds=app_config.conversation.extraction_lease_seconds,
                    message_limit=app_config.conversation.context_message_limit,
                )
            if not claims:
                break
            results = await asyncio.gather(
                *(self._process_claim(structured, claim) for claim in claims)
            )
            processed += sum(results)
            remaining -= len(claims)
        return processed

    async def _process_claim(
        self,
        structured: Runnable[
            LanguageModelInput,
            dict[str, object] | BaseModel,
        ],
        claim: ClaimedExtraction,
    ) -> int:
        """处理刚领取的一条任务并按 token 完成或退避。"""
        try:
            payload = {
                "summary": claim.summary,
                "messages": [
                    {
                        "uid": message.uid,
                        "id": message.id,
                        "role": message.role.value,
                        "content": message.content,
                    }
                    for message in claim.messages
                ],
            }
            value = await structured.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
                ]
            )
            result = ExtractionResult.model_validate(value)
            candidates = _validated_candidates(claim, result)
            async with MySQLDatabase.session() as session:
                repository = ConversationRepository(session)
                await MemoryRepository(session).upsert_candidates(candidates)
                finished = await repository.finish_extraction(
                    claim,
                    result.summary[: app_config.conversation.summary_max_chars],
                )
                if not finished:
                    raise RuntimeError("提炼 lease 已失效")
            return 1
        except Exception as error:
            async with MySQLDatabase.session() as session:
                await ConversationRepository(session).retry_extraction(
                    claim,
                    type(error).__name__,
                    app_config.memory.outbox_max_backoff_seconds,
                )
            logger.bind(
                trace_id=claim.lease_token,
                component="conversation.extraction",
                event_name="conversation.memory.extraction_deferred",
                operation="extract_conversation_memory",
                outcome="deferred",
                error_type=type(error).__name__,
                retryable=True,
            ).warning("对话长期记忆提炼延后")
            return 0
