"""DDL 元数据工作流纯路由函数。"""

from langgraph.graph import END

from data_agent.ddl_metadata.workflow.state import DDLGraphState


def after_terminal_guard(state: DDLGraphState) -> str:
    """解析后路由到终态或记忆加载。"""
    return END if state.get("route") == "rejected" else "load_and_validate_memory"


def after_memory(state: DDLGraphState) -> str:
    """按记忆加载结果路由。"""
    if state.get("route") == "rejected":
        return END
    if state.get("route") == "validate_cached":
        return "validate_metadata"
    return "classify_metadata"


def after_classification(state: DDLGraphState) -> str:
    """按语义分类结果路由。"""
    return {
        "repair": "classify_metadata",
        "validate": "validate_metadata",
        "rejected": END,
    }[state.get("route", "rejected")]


def after_metadata_validation(state: DDLGraphState) -> str:
    """按语义校验结果路由。"""
    if state.get("route") == "valid" and state.get("reused_metrics"):
        return "validate_metrics"
    return {
        "repair": "classify_metadata",
        "valid": "plan_metric_questions",
        "rejected": END,
    }[state.get("route", "rejected")]


def after_questions(state: DDLGraphState) -> str:
    """按问题规划结果路由。"""
    return {
        "await": "await_metric_answers",
        "no_metrics": "build_memory_candidates",
        "rejected": END,
    }[state.get("route", "rejected")]


def after_generation(state: DDLGraphState) -> str:
    """按指标生成结果路由。"""
    return {
        "followup": "plan_metric_questions",
        "repair": "generate_metrics",
        "validate": "validate_metrics",
        "rejected": END,
    }[state.get("route", "rejected")]


def after_metric_validation(state: DDLGraphState) -> str:
    """按指标校验结果路由。"""
    return {
        "repair": "generate_metrics",
        "valid": "build_memory_candidates",
        "rejected": END,
    }[state.get("route", "rejected")]
