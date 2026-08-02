"""当前 DDL 上下文的服务端 AI 聊天路由。"""

from fastapi import APIRouter, Request

from chat.models import ChatTurnRequest, ChatTurnResponse
from chat.service import ChatService

router = APIRouter()


def _chat(request: Request) -> ChatService:
    """读取生命周期创建的聊天编排服务。"""
    return request.app.state.chat


@router.post(
    "/api/v1/conversations/{conversation_uid}/chat-turns",
    response_model=ChatTurnResponse,
)
async def run_chat_turn(
    conversation_uid: str,
    body: ChatTurnRequest,
    request: Request,
) -> ChatTurnResponse:
    """服务端生成并持久化一轮当前 DDL 协作回复。"""
    return await _chat(request).run_turn(conversation_uid, body)
