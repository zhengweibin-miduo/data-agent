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

    user_id: str = Field(min_length=1, max_length=128, description="用户标识。")


class ConversationRecord(ContractModel):
    """用户拥有的一条永久会话记录。"""

    uid: str = Field(description="对象唯一标识。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最近更新时间。")


class ConversationPage(ContractModel):
    """按稳定主键游标读取的会话页。"""

    items: list[ConversationRecord] = Field(description="列表项。")
    next_before: int | None = Field(default=None, description="下一页游标。")


class MessageRecord(ContractModel):
    """一条永久纯文本消息。"""

    id: int = Field(description="记录唯一标识。")
    uid: str = Field(description="对象唯一标识。")
    turn_uid: str = Field(description="轮次唯一标识。")
    role: MessageRole = Field(description="对象角色。")
    content: str = Field(description="对象内容。")
    created_at: datetime = Field(description="创建时间。")


class MessagePage(ContractModel):
    """按消息主键读取并按时间正序展示的历史页。"""

    items: list[MessageRecord] = Field(description="列表项。")
    next_before: int | None = Field(default=None, description="下一页游标。")


class StartTurnRequest(ContractModel):
    """持久化一轮用户输入。"""

    user_id: str = Field(min_length=1, max_length=128, description="用户标识。")
    turn_uid: str = Field(min_length=1, max_length=64, description="轮次唯一标识。")
    content: str = Field(
        min_length=1,
        max_length=app_config.conversation.max_message_chars,
        description="用户输入文本。",
    )


class ContextMessage(ContractModel):
    """提供给未来 Agent 运行时的有界历史消息。"""

    role: MessageRole = Field(description="对象角色。")
    content: str = Field(description="对象内容。")


class ConversationContext(ContractModel):
    """摘要、最近消息和同用户长期记忆组成的有界上下文。"""

    summary: str | None = Field(default=None, description="摘要文本。")
    messages: list[ContextMessage] = Field(description="消息列表。")
    memories: list[MemoryDetail] = Field(description="长期记忆列表。")


class StartTurnResponse(ContractModel):
    """已经持久化的用户消息及有界上下文。"""

    message: MessageRecord = Field(description="消息文本。")
    context: ConversationContext = Field(description="有界会话上下文。")


class CompleteTurnRequest(ContractModel):
    """持久化一轮助手纯文本响应。"""

    user_id: str = Field(min_length=1, max_length=128, description="用户标识。")
    content: str = Field(
        min_length=1,
        max_length=app_config.conversation.max_message_chars,
        description="助手回复文本。",
    )


class CompleteTurnResponse(ContractModel):
    """已经原子提交消息与提炼任务的完成轮次。"""

    message: MessageRecord = Field(description="消息文本。")


class DeleteConversationResponse(ContractModel):
    """会话删除响应。"""

    conversation_uid: str = Field(description="会话唯一标识。")
    deleted: Literal[True] = Field(default=True, description="是否已删除。")


class DeleteConversationDataResponse(ContractModel):
    """用户全部对话数据删除响应。"""

    deleted: Literal[True] = Field(default=True, description="是否已删除。")


class ExtractionCandidate(ContractModel):
    """模型返回且必须由代码验证原文证据的记忆候选。"""

    category: UserMemoryCategory = Field(description="对象类别。")
    key: str = Field(min_length=1, max_length=256, description="记忆键。")
    value: str = Field(min_length=1, max_length=4096, description="记忆值。")
    supporting_user_quote: str = Field(
        min_length=1, max_length=4096, description="支持内容的用户原文。"
    )
    evidence_message_uids: list[str] = Field(
        min_length=1, max_length=20, description="证据消息标识列表。"
    )
    confirmed_assistant_message_uid: str | None = Field(
        default=None, description="确认内容的助手消息标识。"
    )
    assistant_quote: str | None = Field(
        default=None, max_length=4096, description="助手确认原文。"
    )


class ExtractionResult(ContractModel):
    """一次结构化提炼产生的摘要和记忆候选。"""

    summary: str = Field(max_length=4096, description="摘要文本。")
    candidates: list[ExtractionCandidate] = Field(
        default_factory=list, max_length=20, description="提炼出的记忆候选列表。"
    )


class ClaimedExtraction(ContractModel):
    """worker 已领取的提炼任务。"""

    outbox_id: int = Field(description="发件箱记录标识。")
    lease_token: str = Field(description="领取租约令牌。")
    attempts: int = Field(description="处理尝试次数。")
    user_id: str = Field(description="用户标识。")
    conversation_id: int = Field(description="会话数据库标识。")
    conversation_uid: str = Field(description="会话唯一标识。")
    summary: str | None = Field(default=None, description="摘要文本。")
    messages: list[MessageRecord] = Field(description="消息列表。")
