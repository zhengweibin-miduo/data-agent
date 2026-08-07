"""Query Application 依赖的技术中立端口与流事件。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from typing import Literal, Protocol

from pydantic import Field

from conversation.models import CompleteTurnResponse, MessageRecord, StartTurnResponse
from models.base import ContractModel
from models.jobs import DDLJobRequest
from models.physical import PhysicalSchema
from query.domain import (
    QueryContext,
    QueryDraft,
    QueryIntent,
    SQLValidationIssue,
    ValidatedQuery,
)


class QueryRequest(ContractModel):
    """运行一轮自然语言只读查询所需的完整输入。"""

    user_id: str = Field(min_length=1, max_length=128, description="用户标识。")
    conversation_uid: str = Field(
        min_length=1,
        max_length=64,
        description="永久会话唯一标识。",
    )
    turn_uid: str = Field(min_length=1, max_length=64, description="轮次唯一标识。")
    question: str = Field(min_length=1, max_length=32768, description="用户查询原文。")
    ddl_context: DDLJobRequest = Field(description="当前数据来源和 MySQL DDL。")


class QueryClarification(ContractModel):
    """本轮唯一且最高影响的歧义澄清。"""

    slot: str = Field(description="需要澄清的意图槽位。")
    quote: str = Field(description="产生歧义的用户原文。")
    question: str = Field(
        min_length=1, max_length=1024, description="用户可见澄清问题。"
    )


class QueryStreamError(ContractModel):
    """NDJSON 响应开始后唯一允许发送的安全错误投影。"""

    code: str = Field(description="稳定错误代码。")
    stage: str = Field(description="稳定失败阶段。")
    retryable: bool = Field(description="相同请求稍后重试是否可能成功。")


class QueryEvent(ContractModel):
    """NDJSON 查询流中的一个有界事件。"""

    kind: Literal[
        "clarification",
        "metadata",
        "rows",
        "complete",
        "stream_error",
    ] = Field(description="流事件类型。")
    message: str | None = Field(default=None, description="用户可见安全文本。")
    sql: str | None = Field(default=None, description="已经通过全部门禁的只读 SQL。")
    columns: list[str] = Field(default_factory=list, description="结果字段名称。")
    rows: list[list[object]] = Field(default_factory=list, description="本批结果行。")
    row_count: int | None = Field(default=None, description="已经发送的结果总行数。")
    elapsed_ms: int | None = Field(default=None, description="查询执行耗时毫秒数。")
    result_scope: Literal["all_sources"] | None = Field(
        default=None, description="业务结果覆盖的数据来源范围。"
    )
    error: QueryStreamError | None = Field(
        default=None, description="响应开始后的安全流错误。"
    )


class QueryBatch(ContractModel):
    """专用 DW executor 返回的一个有界结果批次。"""

    columns: list[str] = Field(description="结果字段名称。")
    rows: list[list[object]] = Field(description="本批结果行。")


class QueryExplainRejected(Exception):
    """数据库 EXPLAIN 可安全交给一次修复的确定性拒绝。"""

    def __init__(self, issue: SQLValidationIssue) -> None:
        """保存稳定问题，不携带驱动错误或连接信息。"""
        super().__init__(issue.code)
        self.issue = issue


class ConversationPort(Protocol):
    """复用永久 Conversation 轮次语义的应用端口。"""

    async def start_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
        *,
        semantic_fingerprint: str | None = None,
    ) -> StartTurnResponse:
        """原子开始用户轮次并返回有界上下文。"""
        ...

    async def assistant_message(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
    ) -> MessageRecord | None:
        """读取已完成轮次的助手消息。"""
        ...

    async def complete_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
        *,
        semantic_fingerprint: str | None = None,
    ) -> CompleteTurnResponse:
        """原子完成助手消息与提炼 outbox。"""
        ...

    async def abandon_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
    ) -> None:
        """释放失败或取消的查询轮次执行权。"""
        ...

    async def renew_turn(
        self, user_id: str, conversation_uid: str, turn_uid: str
    ) -> bool:
        """仅当当前请求仍持有轮次时续租。"""
        ...


class QueryIntentPort(Protocol):
    """从用户原文生成严格 QueryIntent。"""

    async def parse(
        self,
        question: str,
        context_messages: list[str],
        evidence_messages: list[str],
    ) -> QueryIntent:
        """使用角色上下文解析，并仅用用户原文验证证据。"""
        ...


class QueryMetadataPort(Protocol):
    """构建当前 DDL 范围内的权威查询上下文。"""

    async def build_context(
        self,
        question: str,
        intent: QueryIntent,
        schema: PhysicalSchema,
    ) -> QueryContext | QueryClarification:
        """返回查询上下文或一个澄清问题。"""
        ...

    async def relationships_are_authoritative(self, schema: PhysicalSchema) -> bool:
        """在最终执行协调区内重新核验 accepted 关系快照。"""
        ...


class QueryPlannerPort(Protocol):
    """生成严格 QueryDraft 并消费唯一一次修复预算。"""

    async def draft(self, context: QueryContext, intent: QueryIntent) -> QueryDraft:
        """生成初始查询草稿。"""
        ...

    async def repair(
        self,
        context: QueryContext,
        intent: QueryIntent,
        draft: QueryDraft,
        issues: tuple[SQLValidationIssue, ...],
    ) -> QueryDraft:
        """只使用稳定问题代码修复一次草稿。"""
        ...


class QueryReadinessPort(Protocol):
    """检查 AST 实际目标表的 DW 同步就绪状态。"""

    async def ready(self, target_tables: tuple[str, ...]) -> bool:
        """仅当全部实际目标表处于 streaming 时返回真。"""
        ...

    def hold(
        self, target_tables: tuple[str, ...]
    ) -> AbstractAsyncContextManager[None]:
        """在就绪复核和完整执行期间固定目标表同步代次。"""
        ...


class QueryExecutorPort(Protocol):
    """只接受 ValidatedQuery 的专用 DW 只读执行端口。"""

    async def explain(self, query: ValidatedQuery) -> None:
        """执行不返回业务行的数据库预检。"""
        ...

    def execute(self, query: ValidatedQuery) -> AsyncGenerator[QueryBatch, None]:
        """按固定单批预算流式读取完整业务结果。"""
        ...
