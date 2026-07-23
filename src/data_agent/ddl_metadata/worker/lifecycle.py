"""DDL 元数据 worker 资源生命周期与图装配。"""

from typing import Any

from data_agent.runtime import RuntimeHandle, RuntimeRole, start, stop


async def startup(ctx: dict[Any, Any]) -> None:
    """通过统一运行时装配初始化 Worker 的全部长生命周期依赖。"""
    if "_runtime_handle" in ctx:
        raise RuntimeError("Worker 运行时 handle 已存在，不能重复启动")
    ctx["_runtime_handle"] = await start(
        RuntimeRole.DDL_METADATA_WORKER,
        ctx,
    )


async def shutdown(ctx: dict[Any, Any]) -> None:
    """取回并关闭 Worker 的统一运行时 handle。"""
    handle = ctx.pop("_runtime_handle", None)
    if not isinstance(handle, RuntimeHandle):
        raise RuntimeError("Worker 运行时 handle 缺失或无效")
    await stop(handle)
