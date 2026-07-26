"""Agent 文本对话与记忆提炼的严格契约。"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from data_agent.models.base import ContractModel
from data_agent.models.memory import (
    MemoryDetail,
    UserMemoryCategory,
)
from data_agent.settings import app_config


class MessageRole(StrEnum):
    """首版允许持久化的消息角色。"""

    USER = "user"
    ASSISTANT = "assistant"


class CreateConversationRequest(ContractModel):
    """创建用户会话请求。"""

    user_id: str = Field(min_length=1, max_length=128)


class ConversationRecord(ContractModel):
    """用户拥有的一条永久会话记录。"""

    uid: str
    created_at: datetime
    updated_at: datetime


class ConversationPage(ContractModel):
    """按稳定主键游标读取的会话页。"""

    items: list[ConversationRecord]
    next_before: int | None = None


class MessageRecord(ContractModel):
    """一条永久纯文本消息。"""

    id: int
    uid: str
    turn_uid: str
    role: MessageRole
    content: str
    created_at: datetime


class MessagePage(ContractModel):
    """按消息主键读取并按时间正序展示的历史页。"""

    items: list[MessageRecord]
    next_before: int | None = None


class StartTurnRequest(ContractModel):
    """持久化一轮用户输入。"""

    user_id: str = Field(min_length=1, max_length=128)
    turn_uid: str = Field(min_length=1, max_length=64)
    content: str = Field(
        min_length=1,
        max_length=app_config.conversation.max_message_chars,
    )


class ContextMessage(ContractModel):
    """提供给未来 Agent 运行时的有界历史消息。"""

    role: MessageRole
    content: str


class ConversationContext(ContractModel):
    """摘要、最近消息和同用户长期记忆组成的有界上下文。"""

    summary: str | None = None
    messages: list[ContextMessage]
    memories: list[MemoryDetail]


class StartTurnResponse(ContractModel):
    """已经持久化的用户消息及有界上下文。"""

    message: MessageRecord
    context: ConversationContext


class CompleteTurnRequest(ContractModel):
    """持久化一轮助手纯文本响应。"""

    user_id: str = Field(min_length=1, max_length=128)
    content: str = Field(
        min_length=1,
        max_length=app_config.conversation.max_message_chars,
    )


class CompleteTurnResponse(ContractModel):
    """已经原子提交消息与提炼任务的完成轮次。"""

    message: MessageRecord


class DeleteConversationResponse(ContractModel):
    """会话删除响应。"""

    conversation_uid: str
    deleted: Literal[True] = True


class DeleteConversationDataResponse(ContractModel):
    """用户全部对话数据删除响应。"""

    deleted: Literal[True] = True


class ExtractionCandidate(ContractModel):
    """模型返回且必须由代码验证原文证据的记忆候选。"""

    category: UserMemoryCategory
    key: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=4096)
    supporting_user_quote: str = Field(min_length=1, max_length=4096)
    evidence_message_uids: list[str] = Field(min_length=1, max_length=20)
    confirmed_assistant_message_uid: str | None = None
    assistant_quote: str | None = Field(default=None, max_length=4096)


class ExtractionResult(ContractModel):
    """一次结构化提炼产生的摘要和记忆候选。"""

    summary: str = Field(max_length=4096)
    candidates: list[ExtractionCandidate] = Field(default_factory=list, max_length=20)


class ClaimedExtraction(ContractModel):
    """worker 已领取的提炼任务。"""

    outbox_id: int
    lease_token: str
    attempts: int
    user_id: str
    conversation_id: int
    conversation_uid: str
    summary: str | None = None
    messages: list[MessageRecord]
