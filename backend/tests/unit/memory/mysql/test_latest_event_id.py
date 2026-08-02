"""最新事件编号读取的语句级契约测试。"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ClauseElement

from memory.domain.payloads import memory_text_hash
from memory.mysql.repository import MemoryRepository
from memory.mysql.tables import agent_memory
from tests.helpers.checks import check_condition, check_equal


class _FakeResult:
    """返回预置标量的结果替身。"""

    def __init__(self, value: int | None) -> None:
        """绑定预置标量。"""
        self._value = value

    def scalar_one_or_none(self) -> int | None:
        """返回预置标量。"""
        return self._value

    def all(self) -> list[str]:
        """标量查询返回空结果，本用例只断言语句形态。"""
        return []


class _RecordingSession:
    """记录执行语句并返回预置最大事件编号。"""

    def __init__(self, value: int | None) -> None:
        """绑定预置最大事件编号。"""
        self.statements: list[ClauseElement] = []
        self._value = value

    async def execute(self, statement: ClauseElement) -> _FakeResult:
        """记录语句并返回预置结果。"""
        self.statements.append(statement)
        return _FakeResult(self._value)

    async def scalars(self, statement: ClauseElement) -> _FakeResult:
        """记录标量查询语句并返回空结果视图。"""
        self.statements.append(statement)
        return _FakeResult(None)


class _Detail:
    """模拟权威记忆详情的作用域字段。"""

    source = "dw"
    category = "ddl.semantic"
    memory_key = "orders"


class _Memory:
    """模拟 get_by_uid 的返回值。"""

    detail = _Detail()


def _repository(
    session: _RecordingSession,
    *,
    found: bool = True,
) -> MemoryRepository:
    """构造绑定替身 Session 且短路 get_by_uid 的仓储。"""
    repository = MemoryRepository(cast(AsyncSession, session))

    async def _get_by_uid(uid: str, *, user_id: str | None = None) -> Any:
        """按用例需要返回权威记忆或空。"""
        return _Memory() if found else None

    repository.get_by_uid = _get_by_uid  # type: ignore[method-assign]
    return repository


def _rendered(statement: ClauseElement) -> str:
    """渲染语句文本与参数，供断言检查关键子句和取值。"""
    compiled = statement.compile(dialect=mysql.dialect())
    return f"{compiled} {compiled.params}"


async def test_latest_event_id_uses_max_without_paging() -> None:
    """最新事件编号必须取作用域内最大 id，不依赖分页窗口。"""
    session = _RecordingSession(731)
    repository = _repository(session)

    check_equal(
        "返回作用域内最大事件编号",
        await repository.latest_event_id("memory-1"),
        731,
    )
    rendered = _rendered(session.statements[0])
    check_condition(
        "使用聚合最大值而非分页",
        "max(" in rendered.casefold()
        and "limit" not in rendered.casefold()
        and "offset" not in rendered.casefold(),
        actual=rendered,
        expected="包含 max( 且不含 LIMIT/OFFSET",
    )
    check_condition(
        "按逻辑事实作用域限定",
        "source = " in rendered
        and "category = " in rendered
        and "memory_key = " in rendered,
        actual=rendered,
        expected="按 source/category/memory_key 限定作用域",
    )


async def test_latest_event_id_returns_zero_for_invisible_memory() -> None:
    """当前租户不可见的记忆返回 0，不查询事件表。"""
    session = _RecordingSession(None)
    repository = _repository(session, found=False)

    check_equal(
        "不可见记忆返回 0",
        await repository.latest_event_id("memory-missing", user_id="user-1"),
        0,
    )
    check_equal("不可见记忆不查询事件表", len(session.statements), 0)


async def test_latest_event_id_returns_zero_without_events() -> None:
    """作用域内没有事件时返回 0 而不是 None。"""
    session = _RecordingSession(None)
    repository = _repository(session)

    check_equal(
        "无事件返回 0",
        await repository.latest_event_id("memory-1"),
        0,
    )


async def test_record_access_preserves_content_update_time() -> None:
    """访问统计不得推进 updated_at，否则读路径会改变读路径的排序。"""
    session = _RecordingSession(0)
    repository = MemoryRepository(cast(AsyncSession, session))

    await repository.record_access({"uid-a"}, source="dw", user_id=None)

    rendered = _rendered(session.statements[0])
    check_condition(
        "递增访问计数并记录访问时间",
        "access_count" in rendered and "last_accessed_at" in rendered,
        actual=rendered,
        expected="SET 包含 access_count 与 last_accessed_at",
    )
    compact = rendered.replace(" ", "")
    qualified_updated_at = (
        f"{agent_memory.schema}.{agent_memory.name}.{agent_memory.c.updated_at.name}"
    )
    check_condition(
        "显式写回 updated_at 以抑制 onupdate",
        f"updated_at={qualified_updated_at}" in compact,
        actual=rendered,
        expected="SET 中 updated_at 自赋值，不被 onupdate 推进为 now()",
    )
    check_condition(
        "updated_at 未被写成 now()",
        "updated_at=now()" not in compact,
        actual=rendered,
        expected="SET 中不出现 updated_at=now()",
    )


async def test_find_exact_query_uses_indexed_hash_equality() -> None:
    """精确基线检索必须比较定长哈希，而不是对 TEXT 列做全等比较。"""
    session = _RecordingSession(0)
    repository = MemoryRepository(cast(AsyncSession, session))

    await repository.find_exact_query("dw", "订单事实表", None, user_id=None, limit=20)

    rendered = _rendered(session.statements[0])
    compact = rendered.replace(" ", "")
    check_condition(
        "改为比较文本哈希",
        "memory_text_hash=" in compact,
        actual=rendered,
        expected="WHERE 使用 memory_text_hash 等值比较",
    )
    check_condition(
        "不再对 TEXT 列做全等比较",
        "agent_memory.memory_text=" not in compact,
        actual=rendered,
        expected="WHERE 不含 memory_text 全等比较",
    )
    check_condition(
        "文本分支绑定的是查询文本的哈希",
        memory_text_hash("订单事实表") in rendered,
        actual=rendered,
        expected="memory_text_hash 参数为查询文本的 SHA-256",
    )
    check_condition(
        "保留可走索引的 memory_key 等值分支",
        "memory_key=" in compact,
        actual=rendered,
        expected="WHERE 仍包含 memory_key 等值比较",
    )
