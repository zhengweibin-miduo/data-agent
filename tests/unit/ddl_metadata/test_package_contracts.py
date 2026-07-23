"""包重构后的运行时兼容契约检查。"""

from fastapi.routing import APIRoute

from data_agent.ddl_metadata.api.jobs import router as jobs_router
from data_agent.ddl_metadata.api.memories import router as memories_router
from data_agent.ddl_metadata.jobs.redis.scripts import JobScripts
from data_agent.ddl_metadata.worker.lifecycle import shutdown, startup
from data_agent.ddl_metadata.worker.settings import WorkerSettings
from data_agent.ddl_metadata.workflow.state import DDLGraphState
from data_agent.settings import app_config
from tests.helpers.checks import check_condition, check_equal


def test_api_route_contract_is_unchanged() -> None:
    """锁定 HTTP 路径、方法、名称、状态码与响应模型。"""
    routes = [
        (
            route.path,
            tuple(sorted(route.methods or set())),
            route.name,
            route.status_code,
            (
                route.response_model.__name__
                if route.response_model is not None
                else None
            ),
        )
        for route in (*jobs_router.routes, *memories_router.routes)
        if isinstance(route, APIRoute)
    ]
    check_equal(
        "DDL API 路由契约",
        routes,
        [
            (
                "/api/v1/metadata/ddl-jobs",
                ("POST",),
                "submit_job",
                202,
                "DDLJobAccepted",
            ),
            (
                "/api/v1/metadata/ddl-jobs/{job_id}",
                ("GET",),
                "get_job",
                None,
                "JobRecord",
            ),
            (
                "/api/v1/metadata/ddl-jobs/{job_id}/events",
                ("GET",),
                "get_job_events",
                None,
                None,
            ),
            (
                "/api/v1/metadata/ddl-jobs/{job_id}/answers",
                ("POST",),
                "answer_job",
                202,
                "JobRecord",
            ),
            (
                "/api/v1/metadata/memories/search",
                ("GET",),
                "search_memories",
                None,
                "MemorySearchResponse",
            ),
            (
                "/api/v1/metadata/memories/{memory_uid}",
                ("GET",),
                "get_memory",
                None,
                "MemoryDetail",
            ),
            (
                "/api/v1/metadata/memories/{memory_uid}/history",
                ("GET",),
                "get_memory_history",
                None,
                "MemoryHistoryPage",
            ),
            (
                "/api/v1/metadata/memories/{memory_uid}",
                ("PATCH",),
                "update_memory",
                None,
                "MemoryUpdateResponse",
            ),
            (
                "/api/v1/metadata/memories/{memory_uid}",
                ("DELETE",),
                "delete_memory",
                None,
                "MemoryDeleteResponse",
            ),
        ],
    )


def test_worker_discovery_contract_is_unchanged() -> None:
    """锁定 arq 函数、周期任务和运行参数。"""
    function = WorkerSettings.functions[0]
    check_equal("arq 任务名", function.name, "run_ddl_job")
    check_equal("arq 任务保留", function.keep_result_s, 0)
    check_equal(
        "arq 任务超时",
        function.timeout_s,
        app_config.redis.worker_job_timeout_seconds,
    )
    check_equal("arq 最大尝试", function.max_tries, 3)
    check_equal(
        "arq 周期任务名",
        [job.name for job in WorkerSettings.cron_jobs],
        [
            "cron:dispatch_pending",
            "cron:expire_waiting",
            "cron:expire_memories",
            "cron:cleanup_checkpoints",
            "cron:dispatch_memory_index_outbox",
            "cron:extract_conversation_memory",
            "cron:purge_user_memories",
        ],
    )
    check_equal(
        "arq 周期秒位",
        [job.second for job in WorkerSettings.cron_jobs],
        [
            {0, 10, 20, 30, 40, 50},
            0,
            1,
            {5, 15, 25, 35, 45, 55},
            {2, 12, 22, 32, 42, 52},
            {4, 14, 24, 34, 44, 54},
            {7, 17, 27, 37, 47, 57},
        ],
    )
    check_equal("worker startup", WorkerSettings.__dict__["on_startup"], startup)
    check_equal("worker shutdown", WorkerSettings.__dict__["on_shutdown"], shutdown)
    check_equal(
        "worker 并发",
        WorkerSettings.max_jobs,
        app_config.redis.worker_concurrency,
    )
    check_equal("worker 结果保留", WorkerSettings.keep_result, 0)


def test_redis_and_graph_static_contracts_are_unchanged() -> None:
    """锁定 Redis Lua 字段与 LangGraph 检查点状态键。"""
    for label, script, fragments in (
        (
            "SUBMIT",
            JobScripts.SUBMIT,
            ("'job_id'", "'revision', '0'", "'graph_version'", "'ddl'"),
        ),
        (
            "TRANSITION",
            JobScripts.TRANSITION,
            ("'status'", "'updated_at'", "'HDEL'", "'EXPIRE'"),
        ),
        (
            "ANSWER",
            JobScripts.ANSWER,
            ("'question_set_id'", "'answer_hash'", "'answer_json'", "'revision'"),
        ),
    ):
        check_condition(
            f"{label} Lua 字段",
            all(fragment in script for fragment in fragments),
            expected="所有兼容字段均存在",
        )
    check_equal(
        "LangGraph 状态键",
        set(DDLGraphState.__annotations__),
        {
            "job_id",
            "source",
            "dialect",
            "ddl",
            "physical_schema",
            "semantic_metadata",
            "memory_capsule",
            "reused_memory",
            "metric_questions",
            "current_questions",
            "metric_answers",
            "metrics",
            "memory_candidates",
            "validation_errors",
            "semantic_attempts",
            "metric_attempts",
            "question_round",
            "status",
            "reused_metrics",
            "route",
            "result",
            "error",
        },
    )
