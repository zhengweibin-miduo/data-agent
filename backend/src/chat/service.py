"""当前 DDL 上下文的最小 AI 聊天应用编排。"""

import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAIError,
    RateLimitError,
)

from answer_readiness.models import (
    AnswerDataTarget,
    AnswerGateDecision,
    AnswerTargetCatalog,
)
from answer_readiness.service import (
    DATA_PREPARING_MESSAGE,
    INTENT_UNRESOLVED_MESSAGE,
    AnswerReadinessService,
)
from chat.models import ChatTurnRequest, ChatTurnResponse
from conversation.application.service import ConversationService
from conversation.models import ConversationContext, MessageRole
from ddl_metadata.parsing import parse_ddl
from errors import DataAgentError
from settings import app_config

_SYSTEM_PROMPT = """你是当前数据来源和 MySQL DDL 的语义协作助手。
只解释当前结构、追问业务语义、整理用户提供的规则，或起草 DDL 澄清答案。
你不能提交 DDL 澄清答案、推进任务状态或声称已经修改元数据。
你没有业务行查询工具，不得编造实际行数、金额或其它数据结果。
拒绝与当前数据来源和 DDL 无关的通用请求，不输出隐藏推理。"""
_RETRYABLE_MODEL_ERRORS = (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)


class ChatService:
    """编排永久会话、回答就绪门禁和共享聊天模型。"""

    def __init__(
        self,
        conversations: ConversationService,
        readiness: AnswerReadinessService,
        model: BaseChatModel,
    ) -> None:
        """绑定现有会话服务、就绪门禁和服务端模型。"""
        self._conversations = conversations
        self._readiness = readiness
        self._model = model

    async def run_turn(
        self,
        conversation_uid: str,
        request: ChatTurnRequest,
    ) -> ChatTurnResponse:
        """持久化用户消息、生成安全回复并原子完成轮次。"""
        # 步骤一：在占用会话门禁前拒绝过大或无效 DDL，确定性输入错误不能留下
        # 需要租约回收的 active turn。
        max_ddl_bytes = app_config.api.max_ddl_bytes
        if (
            len(request.ddl_context.ddl) > max_ddl_bytes
            or len(request.ddl_context.ddl.encode()) > max_ddl_bytes
        ):
            raise DataAgentError(
                "ddl_too_large",
                "chat_turn",
                "聊天 DDL 超过配置的字节限制",
                http_status=422,
            )
        schema = await parse_ddl(
            request.ddl_context.source,
            request.ddl_context.ddl,
        )

        # 步骤二：复用会话门禁保存用户消息并组装摘要、近期消息和长期记忆。
        started = await self._conversations.start_turn(
            request.user_id,
            conversation_uid,
            request.turn_uid,
            request.content,
        )

        # 步骤三：完成轮次的幂等回放直接返回现有助手消息，不重复调用门禁或模型。
        existing = await self._conversations.assistant_message(
            request.user_id,
            conversation_uid,
            request.turn_uid,
        )
        if existing is not None:
            return ChatTurnResponse(
                message=existing,
                readiness=self._existing_decision(existing.content),
            )

        # 步骤四：只把确定性解析得到的真实表名和当前来源交给就绪分类器。
        catalog = AnswerTargetCatalog(
            targets=[
                AnswerDataTarget(
                    target_table=table.name,
                    sources=[request.ddl_context.source],
                )
                for table in schema.tables
            ]
        )
        try:
            gate = await self._readiness.evaluate(request.content, catalog)
            if gate.decision == AnswerGateDecision.PROCEED:
                assistant_content = await self._generate(
                    started.context,
                    schema.source,
                    schema.canonical_ddl,
                )
            else:
                assistant_content = gate.user_message or "无法继续回答"
        except OpenAIError as error:
            # 步骤五：模型边界只投影稳定错误字段；同一 turn 可在会话租约内安全重试。
            raise DataAgentError(
                "chat_model_failed",
                "chat_turn",
                "聊天模型调用失败",
                retryable=isinstance(error, _RETRYABLE_MODEL_ERRORS),
                http_status=502,
            ) from error

        # 步骤六：复用完成轮次事务，原子写入助手消息、提炼 outbox 并释放门禁。
        completed = await self._conversations.complete_turn(
            request.user_id,
            conversation_uid,
            request.turn_uid,
            assistant_content,
        )
        return ChatTurnResponse(message=completed.message, readiness=gate.decision)

    @staticmethod
    def _existing_decision(content: str) -> AnswerGateDecision:
        """从固定安全回复恢复幂等回放的 readiness 决策。"""
        if content == DATA_PREPARING_MESSAGE:
            return AnswerGateDecision.DATA_PREPARING
        if content == INTENT_UNRESOLVED_MESSAGE:
            return AnswerGateDecision.INTENT_UNRESOLVED
        return AnswerGateDecision.PROCEED

    async def _generate(
        self,
        context: ConversationContext,
        source: str,
        canonical_ddl: str,
    ) -> str:
        """把有界会话上下文和当前 DDL 交给共享文本模型。"""
        payload = {
            "summary": context.summary,
            "memories": [memory.model_dump(mode="json") for memory in context.memories],
            "source": source,
            "source_ddl": canonical_ddl,
        }
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            SystemMessage(content=json.dumps(payload, ensure_ascii=False)),
            *[
                (
                    HumanMessage(content=message.content)
                    if message.role == MessageRole.USER
                    else AIMessage(content=message.content)
                )
                for message in context.messages
            ],
        ]
        response = await self._model.ainvoke(messages)
        content = response.content
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > app_config.conversation.max_message_chars
        ):
            raise DataAgentError(
                "chat_model_invalid",
                "chat_turn",
                "聊天模型未返回有效文本",
                retryable=True,
                http_status=502,
            )
        return content.strip()
