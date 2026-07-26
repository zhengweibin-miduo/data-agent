"""DDL Worker LangGraph 任务事件映射检查。"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from data_agent.ddl_metadata.worker.job_runner import _task_start_stage
from data_agent.models.jobs import JobEventStage
from tests.helpers.checks import check_equal


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
