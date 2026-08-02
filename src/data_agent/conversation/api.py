"""Agent 文本会话和用户长期记忆 HTTP 路由。"""

from fastapi import APIRouter, Query, Request

from data_agent.conversation.application.service import ConversationService
from data_agent.conversation.models import (
    CompleteTurnRequest,
    CompleteTurnResponse,
    ConversationPage,
    ConversationRecord,
    CreateConversationRequest,
    DeleteConversationDataResponse,
    DeleteConversationResponse,
    MessagePage,
    StartTurnRequest,
    StartTurnResponse,
)
from data_agent.memory.application.service import MemoryService
from data_agent.models.memory import (
    MemoryDeleteResponse,
    MemoryDetail,
    MemoryHistoryPage,
    MemorySearchResponse,
    MemoryUpdateRequest,
    MemoryUpdateResponse,
)

router = APIRouter()


def _conversations(request: Request) -> ConversationService:
    """读取生命周期创建的对话服务。"""
    return request.app.state.conversations


def _memories(request: Request) -> MemoryService:
    """读取生命周期创建的记忆服务。"""
    return request.app.state.memories


@router.post("/api/v1/conversations", response_model=ConversationRecord)
async def create_conversation(
    body: CreateConversationRequest,
    request: Request,
) -> ConversationRecord:
    """创建永久文本会话。"""
    return await _conversations(request).create(body.user_id)


@router.get("/api/v1/conversations", response_model=ConversationPage)
async def list_conversations(
    request: Request,
    user_id: str = Query(min_length=1, max_length=128),
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
) -> ConversationPage:
    """按稳定游标读取用户会话。"""
    return await _conversations(request).list(user_id, before=before, limit=limit)


@router.get(
    "/api/v1/conversations/{conversation_uid}/messages",
    response_model=MessagePage,
)
async def list_messages(
    conversation_uid: str,
    request: Request,
    user_id: str = Query(min_length=1, max_length=128),
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
) -> MessagePage:
    """读取永久原始消息历史。"""
    return await _conversations(request).history(
        user_id,
        conversation_uid,
        before=before,
        limit=limit,
    )


@router.delete(
    "/api/v1/conversations/{conversation_uid}",
    response_model=DeleteConversationResponse,
)
async def delete_conversation(
    conversation_uid: str,
    request: Request,
    user_id: str = Query(min_length=1, max_length=128),
) -> DeleteConversationResponse:
    """删除所属用户的会话。"""
    return await _conversations(request).delete(user_id, conversation_uid)


@router.post(
    "/api/v1/conversations/{conversation_uid}/turns",
    response_model=StartTurnResponse,
)
async def start_turn(
    conversation_uid: str,
    body: StartTurnRequest,
    request: Request,
) -> StartTurnResponse:
    """持久化用户消息并返回有界上下文。"""
    return await _conversations(request).start_turn(
        body.user_id,
        conversation_uid,
        body.turn_uid,
        body.content,
    )


@router.post(
    "/api/v1/conversations/{conversation_uid}/turns/{turn_uid}/assistant",
    response_model=CompleteTurnResponse,
)
async def complete_turn(
    conversation_uid: str,
    turn_uid: str,
    body: CompleteTurnRequest,
    request: Request,
) -> CompleteTurnResponse:
    """原子提交助手消息和异步提炼任务。"""
    return await _conversations(request).complete_turn(
        body.user_id,
        conversation_uid,
        turn_uid,
        body.content,
    )


@router.get(
    "/api/v1/users/{user_id}/memories/search",
    response_model=MemorySearchResponse,
)
async def search_user_memories(
    user_id: str,
    request: Request,
    query: str = Query(min_length=1, max_length=2000),
    limit: int = Query(default=20, ge=1, le=100),
) -> MemorySearchResponse:
    """只检索指定用户的跨会话长期记忆。"""
    return await _memories(request).search_user(user_id, query, limit=limit)


@router.get(
    "/api/v1/users/{user_id}/memories/{memory_uid}",
    response_model=MemoryDetail,
)
async def get_user_memory(
    user_id: str,
    memory_uid: str,
    request: Request,
) -> MemoryDetail:
    """读取指定用户的长期记忆。"""
    return await _memories(request).get(memory_uid, user_id=user_id)


@router.get(
    "/api/v1/users/{user_id}/memories/{memory_uid}/history",
    response_model=MemoryHistoryPage,
)
async def get_user_memory_history(
    user_id: str,
    memory_uid: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> MemoryHistoryPage:
    """读取指定用户记忆历史。"""
    return await _memories(request).history(
        memory_uid,
        user_id=user_id,
        offset=offset,
        limit=limit,
    )


@router.patch(
    "/api/v1/users/{user_id}/memories/{memory_uid}",
    response_model=MemoryUpdateResponse,
)
async def update_user_memory(
    user_id: str,
    memory_uid: str,
    body: MemoryUpdateRequest,
    request: Request,
) -> MemoryUpdateResponse:
    """修正指定用户的跨会话记忆。"""
    return await _memories(request).update(
        memory_uid,
        body.content,
        expected_version=body.expected_version,
        user_id=user_id,
    )


@router.delete(
    "/api/v1/users/{user_id}/memories/{memory_uid}",
    response_model=MemoryDeleteResponse,
)
async def delete_user_memory(
    user_id: str,
    memory_uid: str,
    request: Request,
    expected_version: int = Query(ge=1),
) -> MemoryDeleteResponse:
    """软删除指定用户的跨会话记忆。"""
    return await _memories(request).delete(
        memory_uid,
        expected_version=expected_version,
        user_id=user_id,
    )


@router.delete(
    "/api/v1/users/{user_id}/conversation-data",
    response_model=DeleteConversationDataResponse,
)
async def delete_user_conversation_data(
    user_id: str,
    request: Request,
) -> DeleteConversationDataResponse:
    """删除用户会话并 tombstone 用户长期记忆。"""
    return await _conversations(request).delete_user_data(user_id)
