"""可恢复的 LangGraph DDL 元数据工作流装配。"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from data_agent.ddl_metadata.workflow.contracts import DDLGraphDependencies
from data_agent.ddl_metadata.workflow.nodes import DDLWorkflowNodes
from data_agent.ddl_metadata.workflow.routing import (
    after_classification,
    after_generation,
    after_memory,
    after_metadata_validation,
    after_metric_validation,
    after_questions,
    after_terminal_guard,
)
from data_agent.ddl_metadata.workflow.state import DDLGraphState


def build_ddl_metadata_graph(
    dependencies: DDLGraphDependencies,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """构建从物理解析到原子快照提交的可恢复工作流。

    LLM 分类与指标生成均被确定性解析、记忆重校验和结果校验包围；业务
    含义不足时图在回答节点 interrupt，期间不写权威数据。只有语义与指标
    全部验证通过，才能经 ``persist_snapshot`` 这一唯一成功出口落库。
    """
    # 步骤一：绑定不进入 checkpoint 的进程依赖，并注册所有业务节点。
    nodes = DDLWorkflowNodes(dependencies)
    graph = StateGraph(DDLGraphState)
    graph.add_node("parse_ddl", nodes.parse_node)
    graph.add_node("load_and_validate_memory", nodes.load_memory_node)
    graph.add_node("classify_metadata", nodes.classify_node)
    graph.add_node("validate_metadata", nodes.validate_metadata_node)
    graph.add_node("plan_metric_questions", nodes.plan_questions_node)
    graph.add_node("await_metric_answers", nodes.await_answers_node)
    graph.add_node("generate_metrics", nodes.generate_metrics_node)
    graph.add_node("validate_metrics", nodes.validate_metrics_node)
    graph.add_node("build_memory_candidates", nodes.build_memories_node)
    graph.add_node("persist_snapshot", nodes.persist_node)
    # 步骤二：按确定性解析、模型生成、确定性校验和人工问答边界连接条件路由。
    graph.add_edge(START, "parse_ddl")
    graph.add_conditional_edges("parse_ddl", after_terminal_guard)
    graph.add_conditional_edges("load_and_validate_memory", after_memory)
    graph.add_conditional_edges("classify_metadata", after_classification)
    graph.add_conditional_edges("validate_metadata", after_metadata_validation)
    graph.add_conditional_edges("plan_metric_questions", after_questions)
    graph.add_edge("await_metric_answers", "generate_metrics")
    graph.add_conditional_edges("generate_metrics", after_generation)
    graph.add_conditional_edges("validate_metrics", after_metric_validation)
    # 步骤三：只有完成全部校验的候选记忆才能抵达唯一持久化出口并结束工作流。
    graph.add_edge("build_memory_candidates", "persist_snapshot")
    graph.add_edge("persist_snapshot", END)
    return graph.compile(checkpointer=checkpointer)
