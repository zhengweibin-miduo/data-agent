"""DDL 任务 Store 确定性边界检查。"""

from datetime import UTC, datetime
from typing import cast

from arq.connections import ArqRedis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from ddl_metadata.jobs.identifiers import question_set_id
from ddl_metadata.jobs.redis.codec import JobCodec
from ddl_metadata.jobs.redis.keys import JobKeys
from ddl_metadata.jobs.redis.scripts import JobScripts
from ddl_metadata.jobs.store import DDLJobStore
from errors import DataAgentError
from models.jobs import DDLJobRequest, JobRecord, JobStatus
from models.semantic import (
    MetricAnswer,
    MetricQuestion,
)
from settings import app_config
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)


def _question() -> MetricQuestion:
    """构造稳定的问题载荷。"""
    return MetricQuestion(
        question_id="q-1",
        prompt="营业额口径？",
        fact_table_id="fact-1",
        column_ids=["amount"],
    )


def test_job_keys_store_preserves_keyspace() -> None:
    """锁定任务键、成员和 arq 任务 ID 格式。"""
    keys = JobKeys("ddl")
    check_equal("任务 Hash 键", keys.job("job-1"), "ddl:job:job-1")
    check_equal(
        "任务事件 Stream 键",
        keys.events("job-1"),
        "ddl:job:job-1:events",
    )
    check_equal("来源租约键", keys.source("source-1"), "ddl:source:source-1")
    check_equal("dispatch 键", keys.dispatch, "ddl:dispatch")
    check_equal("waiting 键", keys.waiting, "ddl:waiting")
    check_equal(
        "checkpoint cleanup 键",
        keys.checkpoint_cleanup,
        "ddl:checkpoint_cleanup",
    )
    check_equal("活动任务索引键", keys.active, "ddl:active")
    check_equal(
        "激活成员",
        keys.activation_member("job-1", 2),
        "job-1:2",
    )
    check_equal(
        "arq 任务 ID",
        keys.arq_job_id("job-1", 2),
        "ddl:job-1:2",
    )


def test_job_codec_store_preserves_canonical_payloads() -> None:
    """锁定问题和回答的规范 JSON 与摘要。"""
    question = _question()
    answer = MetricAnswer(question_id="q-1", answer="SUM(amount)")
    check_equal(
        "问题存储 JSON",
        JobCodec.questions_json([question]),
        (
            '[{"question_id":"q-1","prompt":"营业额口径？",'
            '"fact_table_id":"fact-1","column_ids":["amount"],"required":true}]'
        ),
    )
    check_equal(
        "问题集合 ID",
        JobCodec.question_set_id([question]),
        "sha256:faf7078b5aeafcb8e1b6c0b6dee0bc9de39c448582a1d6e21c6612308c955cb9",
    )
    check_equal(
        "纯标识模块问题集合 ID",
        question_set_id([question]),
        "sha256:faf7078b5aeafcb8e1b6c0b6dee0bc9de39c448582a1d6e21c6612308c955cb9",
    )
    check_equal(
        "回答 JSON 与摘要",
        JobCodec.answers_payload([answer]),
        (
            '[{"answer":"SUM(amount)","question_id":"q-1"}]',
            "2cabdad656d38f3468a125d2f23b004144885bb3b2e8a82467b48e876d58c00e",
        ),
    )


def test_job_codec_store_projects_public_record() -> None:
    """验证 Hash 投影保留公开字段且忽略敏感字段。"""
    record = JobCodec.project(
        {
            "job_id": "job-1",
            "source": "source-1",
            "status": "waiting_input",
            "revision": "2",
            "attempt": "1",
            "question_round": "1",
            "question_set_id": "sha256:test",
            "questions_json": JobCodec.questions_json([_question()]),
            "created_at": "2026-07-19T00:00:00+00:00",
            "updated_at": "2026-07-19T00:01:00+00:00",
            "expires_at": "2026-07-19T00:06:00+00:00",
            "graph_version": "v1",
            "ddl": "CREATE TABLE secret(id INT)",
            "answer_json": '[{"answer":"secret"}]',
        }
    )
    check_equal("公开状态", record.status, JobStatus.WAITING_INPUT)
    check_equal("公开修订", record.revision, 2)
    check_equal("公开问题", record.questions, [_question()])
    check_equal(
        "公开投影字段",
        set(record.model_dump()),
        {
            "job_id",
            "source",
            "status",
            "revision",
            "attempt",
            "question_round",
            "question_set_id",
            "questions",
            "result",
            "error",
            "created_at",
            "updated_at",
            "expires_at",
        },
    )


def test_answer_script_renews_source_lease_only_for_owner() -> None:
    """ANSWER 续期来源租约前必须校验属主，避免延长其他持有者的租约。"""
    lines = [line.strip() for line in JobScripts.ANSWER.splitlines()]
    expire_index = lines.index("redis.call('EXPIRE', KEYS[4], ARGV[12])")
    check_equal(
        "续期语句由属主比较守卫",
        lines[expire_index - 1],
        "if redis.call('GET', KEYS[4]) == redis.call('HGET', KEYS[1], 'job_id') then",
    )


def test_job_scripts_maintain_active_index() -> None:
    """三个原子脚本必须共同维护非终态任务的活动索引。"""
    cases = (
        ("受理写入活动索引", JobScripts.SUBMIT, "ZADD', KEYS[4], submit_time[1]"),
        ("终态摘除活动索引", JobScripts.TRANSITION, "ZREM', KEYS[5], job_id"),
        ("非终态刷新活动索引", JobScripts.TRANSITION, "ZADD', KEYS[5], redis_time[1]"),
        ("回答过期摘除活动索引", JobScripts.ANSWER, "ZREM', KEYS[6]"),
        ("回答受理刷新活动索引", JobScripts.ANSWER, "ZADD', KEYS[6], answer_time[1]"),
    )
    for label, script, fragment in cases:
        check_condition(
            label,
            fragment in script,
            actual=fragment,
            expected="脚本包含活动索引维护语句",
        )


async def test_ddl_job_store_rejects_illegal_transition_without_redis() -> None:
    """非法状态转换应在访问 Redis 前失败。"""
    store = DDLJobStore(cast(Redis, object()))
    try:
        await store.transition(
            "job-1",
            0,
            JobStatus.PENDING,
            JobStatus.SUCCEEDED,
        )
    except ValueError as error:
        check_exception("非法转换异常", error, ValueError)
        check_equal(
            "非法转换消息",
            str(error),
            "非法任务状态转换: pending->succeeded",
        )
    else:
        fail_check(
            "非法转换",
            actual="未抛出异常",
            expected="访问 Redis 前抛出 ValueError",
        )


async def test_ddl_job_store_bounds_size_before_redis() -> None:
    """字符快拒绝与有界 UTF-8 精确检查保持同一业务错误。"""
    store = DDLJobStore(cast(Redis, object()))
    limit = app_config.api.max_ddl_bytes
    cases = (
        ("ASCII 字符数越界", "a" * (limit + 1)),
        ("多字节精确字节数越界", "界" * (limit // 3 + 1)),
    )
    for label, ddl in cases:
        try:
            await store.submit(
                DDLJobRequest(
                    source="bounded_size",
                    ddl=ddl,
                )
            )
        except DataAgentError as error:
            check_exception(f"{label} 捕获预期异常", error, DataAgentError)
            check_equal(f"{label} 错误码", error.code, "ddl_too_large")
            check_equal(f"{label} 阶段", error.stage, "submit")
            check_equal(f"{label} HTTP 状态", error.http_status, 422)
        else:
            fail_check(
                label,
                actual="未抛出预期异常",
                expected="Redis 访问前拒绝为 ddl_too_large",
            )


def test_answer_script_clears_questions_but_keeps_idempotency_keys() -> None:
    """受理回答后清除问题列表，但保留幂等判定所需的键。"""
    success = JobScripts.ANSWER.split("return -1\nend\n", 1)[1]
    check_condition(
        "受理分支清除问题列表与等待截止时间",
        "HDEL', KEYS[1], 'questions_json', 'expires_at', 'expires_at_epoch'"
        in success,
        actual=success,
        expected="受理分支 HDEL questions_json 与截止时间字段",
    )
    check_condition(
        "受理分支不清除 question_set_id",
        "'question_set_id'" not in success,
        actual=success,
        expected="保留 question_set_id 供重复提交的幂等判定使用",
    )
    check_condition(
        "受理分支不清除 answer_hash",
        "HDEL" not in success.replace(
            "HDEL', KEYS[1], 'questions_json', 'expires_at', 'expires_at_epoch'",
            "",
        ),
        actual=success,
        expected="仅有一处 HDEL，不触及 answer_hash",
    )


def test_transition_script_increments_attempt_atomically() -> None:
    """尝试次数必须由脚本内 HINCRBY 递增，而不是调用方读改写。"""
    check_condition(
        "转换脚本使用 HINCRBY 递增尝试次数",
        "HINCRBY', KEYS[1], 'attempt', 1" in JobScripts.TRANSITION,
        actual=JobScripts.TRANSITION,
        expected="脚本内包含 HINCRBY attempt",
    )


async def test_mark_running_requests_atomic_attempt_increment() -> None:
    """mark_running 不再先读旧尝试次数，而是请求脚本内原子递增。"""
    recorded: list[bool] = []

    class FakeStateStore:
        """记录转换请求的原子递增标记。"""

        async def get(self, job_id: str) -> JobRecord:
            """读取权威记录属于旧读改写路径，不应被调用。"""
            message = "mark_running 不应为递增尝试次数而预读权威记录"
            raise AssertionError(message)

        async def transition(
            self,
            job_id: str,
            revision: int,
            expected: JobStatus,
            target: JobStatus,
            *,
            fields: object = None,
            increment_attempt: bool = False,
        ) -> bool:
            """记录是否请求了原子递增。"""
            recorded.append(increment_attempt)
            return False

    store = DDLJobStore(cast(Redis, object()))
    store._state = cast(object, FakeStateStore())  # type: ignore[assignment]

    check_equal("CAS 未胜出时如实返回", await store.mark_running("job-1", 0), False)
    check_equal("请求脚本内原子递增", recorded, [True])


async def test_submit_dispatches_immediately_without_waiting_for_cron() -> None:
    """注入队列时，受理路径必须立即调度激活而不是只写 outbox。"""
    dispatched: list[tuple[str, int]] = []

    class FakeStateStore:
        """按最小行为支撑受理路径。"""

        async def submit(self, job_id: str, request: object, now: object) -> bool:
            """报告受理成功。"""
            return True

        async def get(self, job_id: str) -> JobRecord:
            """返回受理后的公开投影。"""
            return JobRecord(
                job_id=job_id,
                source="dw",
                status=JobStatus.PENDING,
                revision=0,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

    class FakeOutbox:
        """记录立即调度请求。"""

        async def dispatch_one(
            self,
            queue: object,
            job_id: str,
            revision: int,
        ) -> bool:
            """记录被立即调度的激活。"""
            dispatched.append((job_id, revision))
            return True

    class BrokenOutbox(FakeOutbox):
        """模拟立即调度失败。"""

        async def dispatch_one(
            self,
            queue: object,
            job_id: str,
            revision: int,
        ) -> bool:
            """抛出瞬时 Redis 故障。"""
            raise RedisError("connection reset")

    class FakeEvents:
        """吞掉公开事件发布。"""

        async def publish(self, job_id: str, event_type: object, data: object) -> None:
            """忽略事件发布。"""

    async def _submit(outbox: FakeOutbox) -> JobRecord:
        """用替身装配任务门面并执行一次受理。"""
        store = DDLJobStore(cast(Redis, object()), cast(ArqRedis, object()))
        store._state = cast(object, FakeStateStore())  # type: ignore[assignment]
        store._outbox = cast(object, outbox)  # type: ignore[assignment]
        store._events = cast(object, FakeEvents())  # type: ignore[assignment]
        return await store.submit(
            DDLJobRequest(source="dw", ddl="CREATE TABLE t(id INT)")
        )

    record = await _submit(FakeOutbox())
    check_equal("受理后立即调度当前修订", dispatched, [(record.job_id, 0)])

    # 立即调度只是时延优化，outbox 成员仍是唯一可恢复的调度请求，
    # 因此入队失败不得撤销已经成立的受理承诺。
    accepted = await _submit(BrokenOutbox())
    check_equal("立即调度失败仍报告受理成功", accepted.status, JobStatus.PENDING)


def test_terminal_cleanup_preserves_answer_idempotency_fingerprint() -> None:
    """终态清理不得删除回答幂等判定所依赖的指纹字段。"""
    # 受理回答后会立即调度激活，任务可能在客户端重试到达前就进入终态；此时若指纹
    # 已被删除，ANSWER 的非等待态幂等分支（返回码 2）就会退化为 stale_answer。
    for label, script in (
        ("状态转换终态清理", JobScripts.TRANSITION),
        ("回答超时终态清理", JobScripts.ANSWER),
    ):
        terminal = script[script.index("HDEL") :]
        check_condition(
            f"{label} 删除原始回答载荷",
            "'answer_json'" in terminal,
            actual=terminal[:160],
            expected="HDEL 包含 answer_json",
        )
        check_condition(
            f"{label} 保留 answer_hash",
            "'answer_hash'" not in terminal.split("EXPIRE")[0],
            actual=terminal[:160],
            expected="终态 HDEL 不含 answer_hash",
        )
        check_condition(
            f"{label} 保留 question_set_id",
            "'question_set_id'" not in terminal.split("EXPIRE")[0],
            actual=terminal[:160],
            expected="终态 HDEL 不含 question_set_id",
        )


def test_submit_script_replays_only_matching_client_coordinate() -> None:
    """任务受理重放必须校验客户端坐标对应的原始输入。"""
    check_condition(
        "相同 job、source 与 DDL 返回幂等重放",
        "return 2" in JobScripts.SUBMIT
        and "HGET', KEYS[1], 'submission_hash'" in JobScripts.SUBMIT,
    )
    check_condition(
        "坐标冲突不会覆盖既有任务",
        "return -1" in JobScripts.SUBMIT
        and JobScripts.SUBMIT.index("EXISTS") < JobScripts.SUBMIT.index("HSET"),
    )
