"""前后端分离后的 API-only 边界检查。"""

from pathlib import Path

from httpx import ASGITransport, AsyncClient

from application import create_app
from tests.helpers.checks import (
    check_condition,
    check_equal,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_static_host_examples_fallback_spa_deep_links() -> None:
    """生产静态服务器配置必须让工作台与知识页深链接回退到入口。"""
    nginx = (REPOSITORY_ROOT / "frontend/deploy/nginx.conf").read_text()
    caddy = (REPOSITORY_ROOT / "frontend/deploy/Caddyfile").read_text()

    check_condition(
        "Nginx SPA fallback",
        "listen 127.0.0.1:80;" in nginx
        and "try_files $uri $uri/ /index.html;" in nginx,
        actual=nginx,
        expected="仅监听回环地址，且前端深链接回退到 /index.html",
    )
    check_condition(
        "Caddy SPA fallback",
        caddy.startswith("http://127.0.0.1:80 {")
        and "handle /api/* {\n        reverse_proxy 127.0.0.1:8000\n    }"
        in caddy
        and "handle {\n        try_files {path} /index.html\n        file_server\n    }"
        in caddy,
        actual=caddy,
        expected="仅监听回环地址，且 API 代理与 SPA fallback 位于互斥路由中",
    )


async def test_backend_does_not_serve_frontend_files() -> None:
    """后端只提供 API、OpenAPI 与健康检查。"""
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
    check_equal(
        "健康检查响应",
        health.json(),
        {"status": "ok", "capabilities": {"ddl_submission_idempotency": True}},
    )


async def test_cors_allows_vite_origin_and_rejects_unknown_origin() -> None:
    """跨源开发只允许配置中的 Vite 本机 Origin。"""
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
