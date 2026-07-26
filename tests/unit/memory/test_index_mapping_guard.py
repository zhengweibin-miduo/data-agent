"""记忆全文索引映射复核的契约测试。"""

from __future__ import annotations

from typing import Any, cast

from elasticsearch import AsyncElasticsearch

from data_agent.ddl_metadata.worker.lifecycle import is_fatal_index_error
from data_agent.errors import DataAgentError
from data_agent.memory.indexing.elasticsearch import MemoryElasticsearchIndex
from data_agent.models.memory import MemoryProjection
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


def _properties(memory_text: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造声明了全部投影字段的 properties 映射。"""
    field = (
        {"type": "text", "analyzer": "memory_zh"}
        if memory_text is None
        else memory_text
    )
    declared: dict[str, Any] = {
        name: {"type": "keyword"} for name in MemoryProjection.model_fields
    }
    declared["memory_text"] = field
    return declared


def _mapping(
    dynamic: str = "strict",
    *,
    memory_text: dict[str, Any] | None = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造指定 dynamic 与字段映射的响应。"""
    index = app_config.elasticsearch.memory_index
    resolved = _properties(memory_text) if properties is None else properties
    return {index: {"mappings": {"dynamic": dynamic, "properties": resolved}}}


def _settings(analyzers: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造指定分析器集合的设置响应，默认与当前配置一致。"""
    index = app_config.elasticsearch.memory_index
    resolved = (
        {
            "memory_zh": {
                "type": "custom",
                "tokenizer": app_config.elasticsearch.analyzer,
            }
        }
        if analyzers is None
        else analyzers
    )
    return {index: {"settings": {"index": {"analysis": {"analyzer": resolved}}}}}


def stale_tokenizer_settings() -> dict[str, Any]:
    """构造分析器同名但分词器过期的设置响应。"""
    return _settings({"memory_zh": {"type": "custom", "tokenizer": "standard"}})


def _mapping_without_memory_text() -> dict[str, Any]:
    """构造缺少 memory_text 字段映射的响应。"""
    properties = _properties()
    del properties["memory_text"]
    return _mapping(properties=properties)


def _mapping_missing_projection_field() -> dict[str, Any]:
    """构造缺少某个投影字段的响应，模拟索引落后于当前投影结构。"""
    properties = _properties()
    del properties["importance_score"]
    return _mapping(properties=properties)


def _index(indices: _FakeIndices) -> MemoryElasticsearchIndex:
    """构造绑定替身客户端的索引封装。"""
    return MemoryElasticsearchIndex(cast(AsyncElasticsearch, _FakeClient(indices)))


async def test_setup_accepts_index_with_strict_mapping_and_analyzer() -> None:
    """既有索引具备严格映射与中文分析器时直接复用。"""
    indices = _FakeIndices(mapping=_mapping(), settings=_settings())

    await _index(indices).setup()

    check_equal("复用既有索引不重建", indices.created, 0)


async def test_setup_rejects_dynamically_created_index() -> None:
    """动态映射自动建出的索引必须报错，不得静默复用。"""
    cases = (
        (
            "缺少严格映射",
            _mapping("true"),
            _settings(),
            "缺少严格映射，索引可能由动态映射自动创建",
        ),
        ("缺少中文分析器", _mapping(), _settings({}), "缺少中文分析器定义"),
        # 索引建于旧配置：分析器同名但 tokenizer 仍是上一版取值，中文 BM25 会持续
        # 按错误分词检索，只检查分析器名称无法发现。
        (
            "分词器与当前配置不一致",
            _mapping(),
            stale_tokenizer_settings(),
            "中文分析器的分词器与当前配置不一致",
        ),
        # memory_text 存在但没有绑定该分析器，实际按默认分词建索引。
        (
            "memory_text 未绑定分析器",
            _mapping(memory_text={"type": "text"}),
            _settings(),
            "memory_text 未绑定当前中文分析器",
        ),
        (
            "memory_text 不是全文字段",
            _mapping(memory_text={"type": "keyword"}),
            _settings(),
            "memory_text 不是全文检索字段",
        ),
        (
            "缺少 memory_text 映射",
            _mapping_without_memory_text(),
            _settings(),
            "缺少 memory_text 字段映射",
        ),
        # 写入侧分词正确但查询侧被覆盖：match 查询不显式指定 analyzer，ES 会采用
        # 字段的 search_analyzer，中文查询因此按错误规则切分而写入不会报错。
        (
            "查询分析器被覆盖",
            _mapping(
                memory_text={
                    "type": "text",
                    "analyzer": "memory_zh",
                    "search_analyzer": "standard",
                }
            ),
            _settings(),
            "memory_text 的查询分析器被覆盖为其它分析器",
        ),
        # 严格映射会拒绝未声明字段：索引落后于当前投影时写入持续失败并最终死信。
        (
            "映射落后于当前投影结构",
            _mapping_missing_projection_field(),
            _settings(),
            "映射缺少当前投影字段：importance_score",
        ),
    )
    for label, mapping, settings, reason in cases:
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
            # 断言具体失配原因，避免用例因为别的检查先失败而"碰巧"通过。
            check_equal(f"{label} 失配原因", error.details["reason"], reason)
        else:
            fail_check(
                label,
                actual="静默复用既有索引",
                expected="抛出 memory_index_mapping_invalid",
            )


def test_structural_index_error_blocks_worker_startup() -> None:
    """结构不兼容必须阻断启动，暂时不可用与未知异常仍允许继续。"""
    # 继续启动会让 dispatcher 向被污染的索引写入并确认 outbox 行，把错误映射固化；
    # 这类故障不会自行痊愈，必须由运维显式重建索引。
    fatal = DataAgentError(
        "memory_index_mapping_invalid",
        "memory_index_setup",
        "既有记忆索引与当前配置不一致",
        http_status=500,
    )
    transient = DataAgentError(
        "memory_index_unavailable",
        "memory_index_setup",
        "索引暂时不可用",
        http_status=503,
    )
    check_equal("结构不兼容阻断启动", is_fatal_index_error(fatal), True)
    check_equal("业务性暂时故障不阻断", is_fatal_index_error(transient), False)
    check_equal(
        "连接类异常不阻断",
        is_fatal_index_error(ConnectionError("refused")),
        False,
    )
