"""当前 DDL 上下文聊天的严格 HTTP 契约。"""

from pydantic import Field

from answer_readiness.models import AnswerGateDecision
from conversation.models import MessageRecord
from models.base import ContractModel
from models.jobs import DDLJobRequest
from settings import app_config


class ChatTurnRequest(ContractModel):
    """服务端生成一轮 DDL 协作回复的请求。"""

    user_id: str = Field(min_length=1, max_length=128, description="用户标识。")
    turn_uid: str = Field(min_length=1, max_length=64, description="轮次唯一标识。")
    content: str = Field(
        min_length=1,
        max_length=app_config.conversation.max_message_chars,
        description="用户输入文本。",
    )
    ddl_context: DDLJobRequest = Field(description="当前数据来源和 MySQL DDL。")


class ChatTurnResponse(ContractModel):
    """已经持久化的助手回复及安全就绪决策。"""

    message: MessageRecord = Field(description="已经持久化的助手消息。")
    readiness: AnswerGateDecision = Field(description="本轮数据就绪门禁结果。")
