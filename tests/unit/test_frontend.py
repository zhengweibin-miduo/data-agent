"""前后端分离后的 API-only 与旧入口兼容检查。"""

from httpx import ASGITransport, AsyncClient

from data_agent.application import create_app
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)


async def test_api_only_is_default_without_frontend_files(monkeypatch) -> None:
    """默认应用只提供 API、OpenAPI 与健康检查。"""
    monkeypatch.delenv("ENABLE_LEGACY_FRONTEND", raising=False)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        workbench = await client.get("/workbench")
        asset = await client.get("/assets/app.js")
        openapi = await client.get("/openapi.json")
        health = await client.get("/api/v1/health")

    check_equal("API-only 根路径状态", root.status_code, 404)
    check_equal("API-only 工作台路径状态", workbench.status_code, 404)
    check_equal("API-only 静态资源路径状态", asset.status_code, 404)
    check_equal("OpenAPI 状态", openapi.status_code, 200)
    check_condition(
        "OpenAPI 保留版本化业务契约",
        "/api/v1/metadata/ddl-jobs" in openapi.json()["paths"],
        actual=list(openapi.json()["paths"]),
        expected="包含 DDL jobs 路由",
    )
    check_equal("健康检查响应", health.json(), {"status": "ok"})


async def test_legacy_frontend_requires_explicit_switch(monkeypatch) -> None:
    """显式兼容开关可在迁移窗口内恢复旧入口。"""
    monkeypatch.setenv("ENABLE_LEGACY_FRONTEND", "true")
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workbench = await client.get("/workbench")
        script = await client.get("/assets/app.js")

    check_equal("兼容工作台状态", workbench.status_code, 200)
    check_equal("兼容静态脚本状态", script.status_code, 200)
    check_condition(
        "兼容入口保留迁移提示内容",
        "Schema Trace" in workbench.text,
        actual=workbench.text[:200],
        expected="旧 Schema Loom 页面",
    )


async def test_cors_allows_vite_origin_and_rejects_unknown_origin(
    monkeypatch,
) -> None:
    """跨源开发只允许配置中的 Vite 本机 Origin。"""
    monkeypatch.delenv("ENABLE_LEGACY_FRONTEND", raising=False)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.options(
            "/api/v1/health",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        rejected = await client.options(
            "/api/v1/health",
            headers={
                "Origin": "http://127.0.0.1:4173",
                "Access-Control-Request-Method": "GET",
            },
        )

    check_equal("允许 Origin 预检状态", allowed.status_code, 200)
    check_equal(
        "允许 Origin 响应头",
        allowed.headers.get("access-control-allow-origin"),
        "http://127.0.0.1:5173",
    )
    check_equal("拒绝未知 Origin 预检状态", rejected.status_code, 400)
    check_equal(
        "拒绝响应不授予 Origin",
        rejected.headers.get("access-control-allow-origin"),
        None,
    )


def test_legacy_frontend_switch_rejects_ambiguous_value(monkeypatch) -> None:
    """无效兼容开关应在应用启动前明确失败。"""
    monkeypatch.setenv("ENABLE_LEGACY_FRONTEND", "sometimes")
    try:
        create_app()
    except ValueError as error:
        check_exception("无效兼容开关", error, ValueError)
        check_condition(
            "无效开关错误指向配置名",
            "ENABLE_LEGACY_FRONTEND" in str(error),
            actual=str(error),
            expected="包含 ENABLE_LEGACY_FRONTEND",
        )
    else:
        fail_check(
            "无效兼容开关",
            actual="应用被创建",
            expected="创建前抛出 ValueError",
        )
