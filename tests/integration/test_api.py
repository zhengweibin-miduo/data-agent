"""本地 FastAPI 任务边界检查。"""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from redis.exceptions import RedisError

from data_agent.application import create_app
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.memory.application.snapshots import (
    MetadataSnapshotService,
)
from data_agent.ddl_metadata.models.jobs import DDLJobRequest
from data_agent.ddl_metadata.models.memory import (
    MemoryKind,
    SemanticDecisionContent,
)
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import APISettings, AppSettings, app_config
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)
from tests.helpers.factories import cleanup_schema, ensure_schema, semantic_for


class _UnavailableJobs:
    """注入提交时 Redis 故障。"""

    async def submit(self, request: DDLJobRequest) -> object:
        del request
        raise RedisError("unavailable")


async def _cleanup(store: DDLJobStore, job_id: str, source: str) -> None:
    """只清理当前 API 测试任务。"""
    redis = store._redis
    await redis.execute_command("DEL", store._job_key(job_id))
    await redis.execute_command("DEL", store._source_key(source))
    await redis.execute_command("ZREM", store.cleanup_key, job_id)
    await redis.execute_command(
        "ZREM",
        store.dispatch_key,
        f"{job_id}:0",
    )


async def _test_api() -> None:
    """验证 202/404/409/422/503、回环默认值和 CORS。"""
    app = create_app()
    check_equal(
        "_test_api 检查点 1",
        app_config.api.host,
        "127.0.0.1",
    )
    invalid_api_config = app_config.api.model_dump(mode="json")
    invalid_api_config["cors_origins"] = ["https://frontend.example.com"]
    try:
        APISettings.model_validate(invalid_api_config)
    except ValidationError as captured_error:
        check_exception("_test_api 捕获预期异常", captured_error, ValidationError)
        pass
    else:
        fail_check(
            "_test_api",
            actual="未抛出预期异常",
            expected="CORS 配置必须拒绝非本机 Origin",
        )
    invalid_lease_config = app_config.model_dump(mode="json")
    invalid_lease_config["memory"]["source_lease_seconds"] = 1
    try:
        AppSettings.model_validate(invalid_lease_config)
    except ValidationError as captured_error:
        check_exception("_test_api 捕获预期异常", captured_error, ValidationError)
        pass
    else:
        fail_check(
            "_test_api",
            actual="未抛出预期异常",
            expected="来源租约必须覆盖 worker 和等待超时",
        )
    invalid_database_config = app_config.model_dump(mode="json")
    invalid_database_config["memory"]["database"] = "invalid-database"
    try:
        AppSettings.model_validate(invalid_database_config)
    except ValidationError as captured_error:
        check_exception("_test_api 捕获预期异常", captured_error, ValidationError)
        pass
    else:
        fail_check(
            "_test_api",
            actual="未抛出预期异常",
            expected="记忆数据库必须是严格 MySQL 标识符",
        )
    same_database_config = app_config.model_dump(mode="json")
    same_database_config["memory"]["database"] = "meta"
    try:
        AppSettings.model_validate(same_database_config)
    except ValidationError as captured_error:
        check_exception("_test_api 捕获预期异常", captured_error, ValidationError)
        pass
    else:
        fail_check(
            "_test_api",
            actual="未抛出预期异常",
            expected="记忆数据库不能使用 Meta 默认数据库",
        )
    missing_default_database_config = app_config.model_dump(mode="json")
    missing_default_database_config["mysql"]["url"] = (
        "mysql+asyncmy://data_agent:data_agent@localhost:3306"
    )
    try:
        AppSettings.model_validate(missing_default_database_config)
    except ValidationError as captured_error:
        check_exception("_test_api 捕获预期异常", captured_error, ValidationError)
        pass
    else:
        fail_check(
            "_test_api",
            actual="未抛出预期异常",
            expected="MySQL URL 必须提供 Meta 默认数据库",
        )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            allowed = await client.options(
                "/api/v1/metadata/ddl-jobs",
                headers={
                    "Origin": "http://127.0.0.1:3000",
                    "Access-Control-Request-Method": "POST",
                },
            )
            check_equal("_test_api 检查点 2", allowed.status_code, 200)
            check_equal(
                "_test_api 检查点 3",
                allowed.headers["access-control-allow-origin"],
                "http://127.0.0.1:3000",
            )
            denied = await client.options(
                "/api/v1/metadata/ddl-jobs",
                headers={
                    "Origin": "http://evil.example",
                    "Access-Control-Request-Method": "POST",
                },
            )
            check_condition(
                "_test_api 检查点 4",
                "access-control-allow-origin" not in denied.headers,
                expected="原断言条件成立",
            )

            invalid = await client.post(
                "/api/v1/metadata/ddl-jobs",
                json={"source": "contains space", "ddl": ""},
            )
            check_equal("_test_api 检查点 5", invalid.status_code, 422)

            missing = await client.get("/api/v1/metadata/ddl-jobs/missing")
            check_equal("_test_api 检查点 6", missing.status_code, 404)
            check_equal(
                "_test_api 检查点 7",
                missing.json()["error"]["code"],
                "job_not_found",
            )

            source = f"api_{uuid4().hex}"
            response = await client.post(
                "/api/v1/metadata/ddl-jobs",
                json={
                    "source": source,
                    "dialect": "mysql",
                    "ddl": "CREATE TABLE fact_api (id BIGINT PRIMARY KEY)",
                },
            )
            check_equal("_test_api 检查点 8", response.status_code, 202)
            accepted = response.json()
            job_id = accepted["job_id"]
            try:
                status_response = await client.get(accepted["status_url"])
                check_equal(
                    "_test_api 检查点 9",
                    status_response.status_code,
                    200,
                )
                check_equal(
                    "_test_api 检查点 10",
                    status_response.json()["status"],
                    "pending",
                )

                conflict = await client.post(
                    f"/api/v1/metadata/ddl-jobs/{job_id}/answers",
                    json={
                        "revision": 0,
                        "question_set_id": "wrong",
                        "answers": [
                            {
                                "question_id": "q",
                                "answer": "answer",
                            }
                        ],
                    },
                )
                check_equal("_test_api 检查点 11", conflict.status_code, 409)
                check_equal(
                    "_test_api 检查点 12",
                    conflict.json()["error"]["code"],
                    "stale_answer",
                )
            finally:
                await _cleanup(app.state.jobs, job_id, source)

            jobs = app.state.jobs
            app.state.jobs = _UnavailableJobs()
            unavailable = await client.post(
                "/api/v1/metadata/ddl-jobs",
                json={
                    "source": f"api_{uuid4().hex}",
                    "ddl": "CREATE TABLE x (id INT)",
                },
            )
            app.state.jobs = jobs
            check_equal(
                "_test_api 检查点 13",
                unavailable.status_code,
                503,
            )
            check_equal(
                "_test_api 检查点 14",
                unavailable.json()["error"]["code"],
                "redis_unavailable",
            )


@pytest.mark.integration
async def test_ddl_metadata_api() -> None:
    """运行 API 检查。"""
    await _test_api()


@pytest.mark.integration
async def test_memory_api() -> None:
    """验证受约束的记忆读取、修正、删除与 MySQL 降级检索。"""
    await ensure_schema()
    source = f"memory_api_{uuid4().hex}"
    schema = await parse_ddl(
        source,
        "CREATE TABLE dim_api (id BIGINT PRIMARY KEY, name VARCHAR(64))",
    )
    metadata = semantic_for(schema, fact=False)
    await MetadataSnapshotService().persist(schema, metadata, [], [], [])
    target = SemanticDecisionContent(
        kind=MemoryKind.SEMANTIC_DECISION,
        table=metadata.tables[0],
    )
    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                search = await client.get(
                    "/api/v1/metadata/memories/search",
                    params={
                        "query": schema.tables[0].id,
                        "source": source,
                        "kind": MemoryKind.SEMANTIC_DECISION.value,
                    },
                )
                check_equal("test_memory_api 检查点 1", search.status_code, 200)
                items = search.json()["items"]
                check_equal("test_memory_api 检查点 2", len(items), 1)
                memory_uid = items[0]["memory"]["uid"]

                detail = await client.get(
                    f"/api/v1/metadata/memories/{memory_uid}"
                )
                check_equal("test_memory_api 检查点 3", detail.status_code, 200)
                history = await client.get(
                    f"/api/v1/metadata/memories/{memory_uid}/history"
                )
                check_equal(
                    "test_memory_api 检查点 4",
                    [event["event_type"] for event in history.json()["items"]],
                    ["ADD"],
                )

                corrected = target.model_copy(
                    update={
                        "table": target.table.model_copy(
                            update={"description": "用户确认的维表说明"}
                        )
                        if target.table is not None
                        else None
                    }
                )
                patch_body = {
                    "content": corrected.model_dump(mode="json"),
                }
                updated = await client.patch(
                    f"/api/v1/metadata/memories/{memory_uid}",
                    json=patch_body,
                )
                check_equal("test_memory_api 检查点 5", updated.status_code, 200)
                repeated = await client.patch(
                    f"/api/v1/metadata/memories/{memory_uid}",
                    json=patch_body,
                )
                check_equal("test_memory_api 检查点 6", repeated.status_code, 409)
                check_equal(
                    "test_memory_api 检查点 7",
                    repeated.json()["error"]["code"],
                    "unchanged_update",
                )

                deleted = await client.delete(
                    f"/api/v1/metadata/memories/{memory_uid}"
                )
                check_equal("test_memory_api 检查点 8", deleted.status_code, 200)
                absent = await client.get(
                    "/api/v1/metadata/memories/search",
                    params={"query": schema.tables[0].id, "source": source},
                )
                check_equal("test_memory_api 检查点 9", absent.json()["items"], [])
                add = await client.post(
                    "/api/v1/metadata/memories",
                    json=patch_body,
                )
                check_equal("test_memory_api 检查点 10", add.status_code, 404)
    finally:
        try:
            await ensure_schema()
            await cleanup_schema(schema)
        finally:
            await MySQLDatabase.close()
