"""记忆全文索引映射复核的契约测试。"""

from __future__ import annotations

from typing import Any, cast

from elasticsearch import AsyncElasticsearch

from data_agent.errors import DataAgentError
from data_agent.memory.indexing.elasticsearch import MemoryElasticsearchIndex
from data_agent.settings import app_config
from tests.helpers.checks import check_condition, check_equal, fail_check


class _Response:
    """模拟 Elasticsearch 响应体。"""

    def __init__(self, body: dict[str, Any]) -> None:
        """绑定响应体。"""
        self.body = body


class _FakeIndices:
    """按预置映射与设置响应索引管理调用。"""

    def __init__(
        self,
        *,
        mapping: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        """绑定预置映射与分析设置。"""
        self._mapping = mapping
        self._settings = settings
        self.created = 0

    async def exists(self, *, index: str) -> bool:
        """既有索引始终存在，用于走复核分支。"""
        return True

    async def get_mapping(self, *, index: str) -> _Response:
        """返回预置映射。"""
        return _Response(self._mapping)

    async def get_settings(self, *, index: str) -> _Response:
        """返回预置分析设置。"""
        return _Response(self._settings)

    async def create(self, **kwargs: Any) -> None:
        """记录创建调用，复核通过路径不应触发。"""
        self.created += 1


class _FakeClient:
    """只暴露索引管理接口的客户端替身。"""

    def __init__(self, indices: _FakeIndices) -> None:
        """绑定索引管理替身。"""
        self.indices = indices


def _mapping(dynamic: str) -> dict[str, Any]:
    """构造指定 dynamic 取值的映射响应。"""
    index = app_config.elasticsearch.memory_index
    return {index: {"mappings": {"dynamic": dynamic}}}


def _settings(analyzers: dict[str, Any]) -> dict[str, Any]:
    """构造指定分析器集合的设置响应。"""
    index = app_config.elasticsearch.memory_index
    return {index: {"settings": {"index": {"analysis": {"analyzer": analyzers}}}}}


def _index(indices: _FakeIndices) -> MemoryElasticsearchIndex:
    """构造绑定替身客户端的索引封装。"""
    return MemoryElasticsearchIndex(cast(AsyncElasticsearch, _FakeClient(indices)))


async def test_setup_accepts_index_with_strict_mapping_and_analyzer() -> None:
    """既有索引具备严格映射与中文分析器时直接复用。"""
    indices = _FakeIndices(
        mapping=_mapping("strict"),
        settings=_settings({"memory_zh": {"type": "custom"}}),
    )

    await _index(indices).setup()

    check_equal("复用既有索引不重建", indices.created, 0)


async def test_setup_rejects_dynamically_created_index() -> None:
    """动态映射自动建出的索引必须报错，不得静默复用。"""
    cases = (
        (
            "缺少严格映射",
            _mapping("true"),
            _settings({"memory_zh": {"type": "custom"}}),
        ),
        ("缺少中文分析器", _mapping("strict"), _settings({})),
        ("两者都缺", _mapping("true"), _settings({})),
    )
    for label, mapping, settings in cases:
        indices = _FakeIndices(mapping=mapping, settings=settings)
        try:
            await _index(indices).setup()
        except DataAgentError as error:
            check_equal(f"{label} 错误码", error.code, "memory_index_mapping_invalid")
            check_equal(f"{label} 阶段", error.stage, "memory_index_setup")
            check_condition(
                f"{label} 报错指向配置的索引",
                error.details["index"] == app_config.elasticsearch.memory_index,
                actual=error.details,
                expected="details 含配置索引名",
            )
        else:
            fail_check(
                label,
                actual="静默复用既有索引",
                expected="抛出 memory_index_mapping_invalid",
            )
