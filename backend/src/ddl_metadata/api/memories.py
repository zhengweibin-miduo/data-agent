"""DDL 元数据记忆 HTTP 路由。"""

from fastapi import APIRouter, Query, Request

from memory.application.service import MemoryService
from models.memory import (
    MemoryDeleteResponse,
    MemoryDetail,
    MemoryHistoryPage,
    MemorySearchResponse,
    MemoryUpdateRequest,
    MemoryUpdateResponse,
)

router = APIRouter()


def _memories(request: Request) -> MemoryService:
    """读取应用生命周期创建的记忆服务。"""
    return request.app.state.memories


@router.get(
    "/api/v1/metadata/memories/search",
    response_model=MemorySearchResponse,
)
async def search_memories(
    request: Request,
    query: str = Query(min_length=1, max_length=2000),
    source: str = Query(min_length=1, max_length=128),
    category: list[str] | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> MemorySearchResponse:
    """按来源、查询和可选类型执行混合检索。"""
    return await _memories(request).search(
        query,
        source,
        categories=set(category) if category else None,
        limit=limit,
    )


@router.get(
    "/api/v1/metadata/memories/{memory_uid}",
    response_model=MemoryDetail,
)
async def get_memory(memory_uid: str, request: Request) -> MemoryDetail:
    """从 MySQL 读取一条权威记忆详情。"""
    return await _memories(request).get(memory_uid)


@router.get(
    "/api/v1/metadata/memories/{memory_uid}/history",
    response_model=MemoryHistoryPage,
)
async def get_memory_history(
    memory_uid: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> MemoryHistoryPage:
    """读取记忆的有界只追加历史。"""
    return await _memories(request).history(
        memory_uid,
        offset=offset,
        limit=limit,
    )


@router.patch(
    "/api/v1/metadata/memories/{memory_uid}",
    response_model=MemoryUpdateResponse,
)
async def update_memory(
    memory_uid: str,
    body: MemoryUpdateRequest,
    request: Request,
) -> MemoryUpdateResponse:
    """记录结构化用户修正并要求重新处理 DDL。"""
    return await _memories(request).update(
        memory_uid, body.content, expected_version=body.expected_version
    )


@router.delete(
    "/api/v1/metadata/memories/{memory_uid}",
    response_model=MemoryDeleteResponse,
)
async def delete_memory(
    memory_uid: str,
    request: Request,
    expected_version: int = Query(ge=1),
) -> MemoryDeleteResponse:
    """执行可审计软删除并排除未来召回。"""
    return await _memories(request).delete(
        memory_uid, expected_version=expected_version
    )
