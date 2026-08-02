"""DDL Worker LangGraph 任务事件映射检查。"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TypedDict

from arq import Retry
from langgraph.graph import END, START, StateGraph

from ddl_metadata.worker.job_runner import _task_start_stage, run_ddl_job
from models.jobs import JobEventStage, JobRecord, JobStatus
from settings import app_config
from tests.helpers.checks import check_equal, fail_check


def test_task_start_stage_maps_only_stable_public_phases() -> None:
    """只识别 task-start 名称，不读取或转发节点输入和结果。"""
    check_equal(
        "解析节点公开阶段",
        _task_start_stage(
            {
                "type": "tasks",
                "data": {
                    "id": "task-1",
                    "name": "parse_ddl",
                    "input": {"ddl": "CREATE TABLE secret(id INT)"},
                    "triggers": ["start:parse_ddl"],
                },
            }
        ),
        JobEventStage.PARSING,
    )
    check_equal(
        "持久化节点公开阶段",
        _task_start_stage(
            {
                "type": "tasks",
                "data": {
                    "id": "task-2",
                    "name": "persist_snapshot",
                    "input": {},
                    "triggers": ["branch"],
                },
            }
        ),
        JobEventStage.PERSISTING,
    )
    check_equal(
        "task result 不发布进度",
        _task_start_stage(
            {
                "type": "tasks",
                "data": {
                    "id": "task-1",
                    "name": "parse_ddl",
                    "error": None,
                    "interrupts": [],
                    "result": {"ddl": "secret"},
                },
            }
        ),
        None,
    )


class _ProbeState(TypedDict):
    """LangGraph task-start 形状探针状态。"""

    value: int


async def test_installed_langgraph_v2_tasks_shape_is_supported() -> None:
    """用真实 astream 锁定当前 LangGraph v2 task-start 载荷。"""

    async def parse_ddl(state: _ProbeState) -> _ProbeState:
        """返回简单状态以同时产生 task start 和 result。"""
        return {"value": state["value"] + 1}

    builder = StateGraph(_ProbeState)
    builder.add_node("parse_ddl", parse_ddl)
    builder.add_edge(START, "parse_ddl")
    builder.add_edge("parse_ddl", END)
    graph = builder.compile()
    stages = [
        stage
        async for event in graph.astream(
            {"value": 0},
            stream_mode="tasks",
            version="v2",
        )
        if (stage := _task_start_stage(event)) is not None
    ]
    check_equal("真实 task-start 阶段", stages, [JobEventStage.PARSING])
    check_equal(
        "等待节点不直接发布",
        _task_start_stage(
            {
                "type": "tasks",
                "data": {
                    "id": "task-3",
                    "name": "await_metric_answers",
                    "input": {},
                    "triggers": ["branch"],
                },
            }
        ),
        None,
    )


def _running_record() -> JobRecord:
    """构造与当前图版本匹配的运行中任务。"""
    now = datetime(2026, 7, 26, tzinfo=UTC)
    return JobRecord(
        job_id="job-retry",
        source="source-retry",
        status=JobStatus.RUNNING,
        revision=1,
        attempt=1,
        created_at=now,
        updated_at=now,
    )


class _Jobs:
    """提供 Retry 传播路径所需的最小任务仓储行为。"""

    def __init__(self) -> None:
        """初始化任务记录与终态写入计数。"""
        self.record = _running_record()
        self.terminal_writes = 0
        self.heartbeats = 0

    async def get(self, job_id: str) -> JobRecord:
        """返回测试任务。"""
        del job_id
        return self.record

    async def renew_source_lease(self, source: str, job_id: str) -> bool:
        """模拟成功续租。"""
        del source, job_id
        return True

    async def graph_version(self, job_id: str) -> str:
        """按 worker 内部读取路径返回与当前配置一致的图版本。"""
        del job_id
        return app_config.llm.graph_version

    async def heartbeat(self, job_id: str) -> None:
        """记录激活开始时的活动索引刷新。"""
        del job_id
        self.heartbeats += 1

    async def mark_terminal(self, *args: object, **kwargs: object) -> None:
        """记录不应发生的终态写入。"""
        del args, kwargs
        self.terminal_writes += 1


class _RetryingGraph:
    """在图流开始时抛出指定 arq Retry。"""

    def __init__(self, retry: Retry) -> None:
        """保存需要原样传播的 Retry 对象。"""
        self.retry = retry

    async def aget_state(self, config: object) -> SimpleNamespace:
        """返回需要继续执行的非终态快照。"""
        del config
        return SimpleNamespace(values={"status": "running"}, tasks=(), next=("node",))

    async def astream(
        self,
        *args: object,
        **kwargs: object,
    ) -> AsyncGenerator[object]:
        """在产生公开事件前请求 arq 重试。"""
        del args, kwargs
        raise self.retry
        yield


async def test_run_ddl_job_preserves_graph_retry_control_flow() -> None:
    """图节点抛出的 arq Retry 原样传播，且不会把任务写成失败终态。"""
    jobs = _Jobs()
    expected_retry = Retry(defer=3)
    graph = _RetryingGraph(expected_retry)

    try:
        await run_ddl_job(
            {"jobs": jobs, "graph": graph},
            jobs.record.job_id,
            jobs.record.revision,
        )
    except Retry as error:
        check_equal("worker 原样传播图节点 Retry", error is expected_retry, True)
    else:
        fail_check(
            "worker 传播图节点 Retry",
            actual="未抛出 Retry",
            expected="传播原 Retry 对象",
        )
    check_equal("Retry 不写失败终态", jobs.terminal_writes, 0)
