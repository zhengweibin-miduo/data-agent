"""API 与 DDL 元数据 Worker 的进程运行时装配。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from loguru import logger
from starlette.datastructures import State

from data_agent.conversation.extraction import ConversationMemoryExtractor
from data_agent.conversation.service import ConversationService
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.memory.application.context import MemoryContextLoader
from data_agent.ddl_metadata.memory.application.service import MemoryService
from data_agent.ddl_metadata.memory.application.snapshots import (
    MetadataSnapshotService,
)
from data_agent.ddl_metadata.memory.indexing.elasticsearch import (
    MemoryElasticsearchIndex,
)
from data_agent.ddl_metadata.memory.indexing.qdrant import MemoryQdrantIndex
from data_agent.ddl_metadata.worker.maintenance import (
    cleanup_checkpoints,
    dispatch_pending,
)
from data_agent.ddl_metadata.workflow.contracts import DDLGraphDependencies
from data_agent.ddl_metadata.workflow.graph import build_ddl_metadata_graph
from data_agent.ddl_metadata.workflow.llm_metadata_generator import (
    LLMMetadataGenerator,
)
from data_agent.infrastructure.checkpoint_store import CheckpointStore
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.llm_client import LLMClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.redis import RedisClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.logging import setup_logging

RuntimeTarget = State | MutableMapping[str, Any]
_Initialize = Callable[["_RuntimeContext"], Awaitable[None]]
_Close = Callable[["_RuntimeContext"], Awaitable[None]]
_MISSING = object()


class RuntimeRole(StrEnum):
    """标识需要装配的进程运行角色。"""

    API = "api"
    DDL_METADATA_WORKER = "ddl_metadata_worker"


@dataclass(frozen=True, slots=True)
class _Action:
    """描述一个私有、有序运行时动作。"""

    name: str
    initialize: _Initialize
    close: _Close | None = None


@dataclass(slots=True)
class _RuntimeContext:
    """保存一次装配期间的私有对象和发布目标。"""

    role: RuntimeRole
    target: RuntimeTarget
    values: dict[str, Any] = field(default_factory=dict)
    publications: list[tuple[str, object]] = field(default_factory=list)
    lifecycle_started: bool = False


@dataclass(slots=True, init=False)
class RuntimeHandle:
    """记录一次成功装配已完成的动作与关闭状态。

    该对象只用于传回 :func:`stop`，不会暴露具体基础设施客户端，也不允许调用方
    注册或重排资源动作。
    """

    _TOKEN: ClassVar[object] = object()

    role: RuntimeRole
    _context: _RuntimeContext = field(repr=False)
    _completed: tuple[_Action, ...] = field(repr=False)
    _closed: bool = field(default=False, repr=False)
    _token: object = field(repr=False)

    @classmethod
    def _create(
        cls,
        context: _RuntimeContext,
        completed: list[_Action],
    ) -> RuntimeHandle:
        """由运行时装配器创建有效 handle。"""
        handle = object.__new__(cls)
        handle.role = context.role
        handle._context = context
        handle._completed = tuple(completed)
        handle._closed = False
        handle._token = cls._TOKEN
        return handle


def _publish(context: _RuntimeContext, key: str, value: Any) -> None:
    """向当前角色的状态适配器发布业务对象。"""
    if isinstance(context.target, MutableMapping):
        previous = context.target.get(key, _MISSING)
        context.publications.append((key, previous))
        context.target[key] = value
        return
    previous = getattr(context.target, key, _MISSING)
    context.publications.append((key, previous))
    setattr(context.target, key, value)


async def _restore_publications(context: _RuntimeContext) -> None:
    """逆序恢复本次启动覆盖或新增的全部状态键。"""
    while context.publications:
        key, previous = context.publications.pop()
        if isinstance(context.target, MutableMapping):
            if previous is _MISSING:
                context.target.pop(key, None)
            else:
                context.target[key] = previous
            continue
        if previous is _MISSING:
            try:
                delattr(context.target, key)
            except AttributeError:
                pass
        else:
            setattr(context.target, key, previous)


async def _setup_logging(context: _RuntimeContext) -> None:
    """配置当前进程的共享日志 sink。"""
    del context
    setup_logging()


async def _close_logging(context: _RuntimeContext) -> None:
    """在最终生命周期事件之后排空 Loguru 队列。"""
    try:
        if context.lifecycle_started:
            component, operation, worker_role, message = {
                RuntimeRole.API: (
                    "application.api",
                    "serve_api",
                    "api",
                    "API 服务已停止",
                ),
                RuntimeRole.DDL_METADATA_WORKER: (
                    "application.worker",
                    "run_worker",
                    "ddl_metadata",
                    "DDL 元数据 worker 已停止",
                ),
            }[context.role]
            logger.bind(
                component=component,
                event_name="application.lifecycle.stopped",
                operation=operation,
                outcome="stopped",
                worker_role=worker_role,
            ).info(message)
    finally:
        await logger.complete()


async def _initialize_redis(context: _RuntimeContext) -> None:
    """初始化并保存 Redis 客户端。"""
    context.values["redis"] = RedisClient.initialize()


async def _initialize_mysql(context: _RuntimeContext) -> None:
    """初始化 MySQL 引擎。"""
    del context
    MySQLDatabase.initialize()


async def _initialize_elasticsearch(context: _RuntimeContext) -> None:
    """初始化并保存 Elasticsearch 客户端。"""
    context.values["elasticsearch"] = ElasticsearchClient.initialize()


async def _initialize_qdrant(context: _RuntimeContext) -> None:
    """初始化并保存 Qdrant 客户端。"""
    context.values["qdrant"] = QdrantClient.initialize()


async def _initialize_tei(context: _RuntimeContext) -> None:
    """初始化 TEI 嵌入客户端。"""
    del context
    TEIEmbeddingClient.initialize()


async def _construct_jobs(context: _RuntimeContext) -> None:
    """构造并发布作业存储。"""
    jobs = DDLJobStore(context.values["redis"])
    context.values["jobs"] = jobs
    _publish(context, "jobs", jobs)


async def _construct_api_services(context: _RuntimeContext) -> None:
    """构造并发布 API 专属服务。"""
    _publish(context, "memories", MemoryService(context.values["jobs"]))
    _publish(context, "conversations", ConversationService())


async def _setup_worker_indexes(context: _RuntimeContext) -> None:
    """独立设置两个可延后初始化的记忆索引。"""
    for target, setup in (
        (
            "ELASTICSEARCH",
            MemoryElasticsearchIndex(context.values["elasticsearch"]).setup,
        ),
        ("QDRANT", MemoryQdrantIndex(context.values["qdrant"]).setup),
    ):
        try:
            await setup()
        except Exception as error:
            logger.bind(
                trace_id="-",
                component="ddl_metadata.worker",
                event_name="ddl_metadata.memory.index_initialization_deferred",
                operation="setup_memory_index",
                outcome="deferred",
                stage=target.lower(),
                error_type=type(error).__name__,
                retryable=True,
            ).warning("记忆索引初始化延后")


async def _initialize_llm(context: _RuntimeContext) -> None:
    """初始化并保存结构化模型客户端。"""
    context.values["model"] = LLMClient.initialize()


async def _check_llm_capability(context: _RuntimeContext) -> None:
    """验证模型端点支持配置的结构化输出。"""
    del context
    await LLMClient.check_structured_output_capability()


async def _initialize_checkpointer(context: _RuntimeContext) -> None:
    """初始化并保存 LangGraph checkpoint store。"""
    context.values["checkpointer"] = await CheckpointStore.initialize()


async def _construct_worker_services(context: _RuntimeContext) -> None:
    """构造并发布 Worker 专属服务与图。"""
    _publish(
        context,
        "conversation_extractor",
        ConversationMemoryExtractor(context.values["model"]),
    )
    _publish(
        context,
        "graph",
        build_ddl_metadata_graph(
            DDLGraphDependencies(
                model=LLMMetadataGenerator(),
                memory_context=MemoryContextLoader(),
                snapshot=MetadataSnapshotService(),
            ),
            context.values["checkpointer"],
        ),
    )


async def _run_worker_maintenance(context: _RuntimeContext) -> None:
    """在 Worker 接收任务前派发积压作业并清理 checkpoint。"""
    if not isinstance(context.target, dict):
        raise RuntimeError("Worker 运行时目标必须是字典上下文")
    await dispatch_pending(context.target)
    await cleanup_checkpoints(context.target)


async def _emit_started(context: _RuntimeContext) -> None:
    """发出保持兼容的进程启动事件。"""
    component, operation, worker_role, message = {
        RuntimeRole.API: (
            "application.api",
            "serve_api",
            "api",
            "API 服务已启动",
        ),
        RuntimeRole.DDL_METADATA_WORKER: (
            "application.worker",
            "run_worker",
            "ddl_metadata",
            "DDL 元数据 worker 已启动",
        ),
    }[context.role]
    logger.bind(
        component=component,
        event_name="application.lifecycle.started",
        operation=operation,
        outcome="started",
        worker_role=worker_role,
    ).info(message)
    context.lifecycle_started = True


async def _close_redis(context: _RuntimeContext) -> None:
    """关闭 Redis 客户端。"""
    del context
    await RedisClient.close()


async def _close_mysql(context: _RuntimeContext) -> None:
    """关闭 MySQL 引擎。"""
    del context
    await MySQLDatabase.close()


async def _close_elasticsearch(context: _RuntimeContext) -> None:
    """关闭 Elasticsearch 客户端。"""
    del context
    await ElasticsearchClient.close()


async def _close_qdrant(context: _RuntimeContext) -> None:
    """关闭 Qdrant 客户端。"""
    del context
    await QdrantClient.close()


async def _close_tei(context: _RuntimeContext) -> None:
    """关闭 TEI 嵌入客户端。"""
    del context
    await TEIEmbeddingClient.close()


async def _close_llm(context: _RuntimeContext) -> None:
    """关闭结构化模型客户端。"""
    del context
    await LLMClient.close()


async def _close_checkpointer(context: _RuntimeContext) -> None:
    """关闭 LangGraph checkpoint store。"""
    del context
    await CheckpointStore.close()


_SHARED_PLAN = (
    _Action("logging", _setup_logging, _close_logging),
    _Action("redis", _initialize_redis, _close_redis),
    _Action("mysql", _initialize_mysql, _close_mysql),
    _Action("elasticsearch", _initialize_elasticsearch, _close_elasticsearch),
    _Action("qdrant", _initialize_qdrant, _close_qdrant),
    _Action("tei", _initialize_tei, _close_tei),
)
_API_PLAN = (
    _Action("jobs", _construct_jobs),
    _Action("api_services", _construct_api_services),
    _Action("started", _emit_started),
)
_WORKER_PLAN = (
    _Action("memory_indexes", _setup_worker_indexes),
    _Action("llm", _initialize_llm, _close_llm),
    _Action("llm_capability", _check_llm_capability),
    _Action("checkpointer", _initialize_checkpointer, _close_checkpointer),
    _Action("jobs", _construct_jobs),
    _Action("worker_services", _construct_worker_services),
    _Action("maintenance", _run_worker_maintenance),
    _Action("started", _emit_started),
)
_PLANS = {
    RuntimeRole.API: _SHARED_PLAN + _API_PLAN,
    RuntimeRole.DDL_METADATA_WORKER: _SHARED_PLAN + _WORKER_PLAN,
}


async def _rollback(
    context: _RuntimeContext,
    completed: list[_Action],
) -> None:
    """逆序尽力回滚已完成动作且不传播回滚错误。"""
    try:
        await _restore_publications(context)
    except BaseException as error:
        _log_rollback_failure(context, "state_publication", error)
    for action in reversed(completed):
        if action.close is None:
            continue
        try:
            await action.close(context)
        except BaseException as error:
            _log_rollback_failure(context, action.name, error)


def _log_rollback_failure(
    context: _RuntimeContext,
    stage: str,
    error: BaseException,
) -> None:
    """记录不包含异常文本的安全回滚失败事件。"""
    try:
        logger.bind(
            trace_id="-",
            component="application.runtime",
            event_name="application.lifecycle.rollback_failed",
            operation="rollback_startup",
            outcome="failed",
            stage=stage,
            error_type=type(error).__name__,
            worker_role=context.role.value,
        ).warning("运行时启动回滚失败")
    except BaseException:
        # 回滚诊断本身不得改变原始启动异常或阻止其余资源清理。
        pass


async def start(role: RuntimeRole, target: RuntimeTarget) -> RuntimeHandle:
    """按角色启动运行时并发布业务状态。

    Args:
        role: API 或 DDL 元数据 Worker 角色。
        target: FastAPI 状态对象或 arq Worker 上下文。

    Returns:
        仅记录本次已完成动作的运行时 handle。

    Raises:
        ValueError: 角色不受支持。
        Exception: 任一初始化动作失败时，在逆序回滚后原样传播。
    """
    if role not in _PLANS:
        raise ValueError(f"不支持的运行时角色: {role!r}")
    context = _RuntimeContext(role=role, target=target)
    completed: list[_Action] = []
    try:
        for action in _PLANS[role]:
            await action.initialize(context)
            completed.append(action)
    except BaseException:
        await _rollback(context, completed)
        raise
    return RuntimeHandle._create(context, completed)


async def stop(handle: RuntimeHandle) -> None:
    """逆序尽力关闭运行时中的全部已完成资源。

    Args:
        handle: :func:`start` 返回的有效且尚未关闭的 handle。

    Raises:
        RuntimeError: handle 无效或已经关闭。
        Exception: 只有一个关闭动作失败时原样传播。
        ExceptionGroup: 多个关闭动作失败时保留全部原始异常。
    """
    if (
        not isinstance(handle, RuntimeHandle)
        or getattr(handle, "_token", None) is not RuntimeHandle._TOKEN
    ):
        raise RuntimeError("运行时 handle 无效，必须使用 start() 的返回值")
    if handle._closed:
        raise RuntimeError("运行时 handle 已关闭，不能重复停止")
    handle._closed = True
    errors: list[BaseException] = []
    try:
        await _restore_publications(handle._context)
    except BaseException as error:
        errors.append(error)
    for action in reversed(handle._completed):
        if action.close is None:
            continue
        try:
            await action.close(handle._context)
        except BaseException as error:
            errors.append(error)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        if all(isinstance(error, Exception) for error in errors):
            raise ExceptionGroup(
                "多个运行时资源关闭失败",
                [error for error in errors if isinstance(error, Exception)],
            )
        raise BaseExceptionGroup("多个运行时资源关闭失败", errors)
