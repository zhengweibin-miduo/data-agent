"""arq 队列客户端构造，集中注入 arq 自身不会设置的套接字超时。

API 与 worker 都需要一个 arq 队列连接：API 在受理路径上立即调度激活，worker 用它
领取任务并驱动全部维护 cron。两处都不能用 ``arq.create_pool``：它只把
``RedisSettings.conn_timeout`` 映射到 ``socket_connect_timeout``，从不设置
``socket_timeout``。既有连接半开、或 Redis 收下命令后不再响应时，缺少读取超时的调用
永远不返回——API 侧表现为 HTTP 请求挂死，worker 侧表现为停止领取任务。

两处共用同一个构造函数，避免其中一处补了超时另一处漏掉：这正是本模块出现的原因。
只有连接重试预算按角色区分，见 ``build_queue_pool`` 的 ``connect_retries``。
"""

from typing import TYPE_CHECKING, Any, cast

from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name, expires_extra_ms
from redis.asyncio.retry import Retry
from redis.backoff import ConstantBackoff
from redis.exceptions import ConnectionError as RedisConnectionError

if TYPE_CHECKING:
    from redis.exceptions import RedisError

from settings import app_config

# 连接阶段的可重试异常。redis-py 的 `AbstractConnection.connect()` 把 `_connect()`
# 包在 `retry.call_with_retry` 里，但 `OSError -> ConnectionError` 与
# `asyncio.TimeoutError -> TimeoutError` 两处转换都发生在 `call_with_retry`
# **之外**。因此重试判定看到的是**原始** `OSError`（连接被拒、连接超时），而 `Retry`
# 默认的 `supported_errors` 只有 redis 自己的 `ConnectionError`/`TimeoutError`——
# 默认配置下连接阶段一次都不会重试。必须显式把 `OSError` 纳入判定。
#
# 刻意不含 redis 的 `TimeoutError`：那是**读取**超时，即服务已收下命令后静默，重试
# 只会把一次挂起变成多次挂起。命令路径另有 `retry_on_error` 门闩兜第二层。
# `socket.timeout` 与 `asyncio.TimeoutError` 都是 `OSError` 子类，已被覆盖。
_CONNECT_PHASE_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    RedisConnectionError,
)


def _connection_target(settings: RedisSettings) -> dict[str, Any]:
    """解析出互斥的 Unix socket 或 TCP 连接目标。

    ``unix://`` DSN 会被 arq 解析进 ``unix_socket_path``；漏传它会让调用方连回 TCP
    ``localhost:6379``，与另一侧连到不同实例，全部任务领取与维护 cron 失效。
    """
    if settings.unix_socket_path:
        return {"unix_socket_path": settings.unix_socket_path}
    return {"host": settings.host, "port": settings.port}


def _connect_retry(settings: RedisSettings, retries: int) -> Retry:
    """构造只覆盖连接阶段的有界重试。

    Args:
        settings: DSN 解析出的 arq 连接设置，提供退避间隔。
        retries: 连接失败后的额外尝试次数，0 表示只尝试一次。

    Returns:
        仅对连接阶段异常生效的有界重试策略。
    """
    # redis-py 把 `supported_errors` 标注为 `tuple[type[RedisError], ...]`，但运行期只
    # 用它做 `isinstance` 判定，而连接阶段必须匹配的恰好是 redis 异常体系之外的原始
    # `OSError`。标注比真实契约窄，因此在此显式收窄类型而不是改变行为。
    return Retry(
        ConstantBackoff(settings.conn_retry_delay),
        retries,
        supported_errors=cast(
            "tuple[type[RedisError], ...]",
            _CONNECT_PHASE_ERRORS,
        ),
    )


def build_queue_pool(*, connect_retries: int) -> ArqRedis:
    """构造带显式读取超时与按角色定预算的连接重试的 arq 队列客户端。

    arq worker 的队列轮询是非阻塞的（``poll_delay`` 默认 0.5 秒，源码中没有
    BLPOP/XREAD 之类的阻塞读取），API 侧只做 ``enqueue_job``，因此统一的读取超时不会
    误伤正常空闲等待。

    Args:
        connect_retries: 连接失败后的额外尝试次数。两侧取值不同是因为兜底手段不同：

            - worker 传 ``RedisSettings.conn_retries``，复刻 ``create_pool`` 的启动
              重试语义。``Worker.main()`` 只在未提供 ``redis_pool`` 时才调用
              ``create_pool``，也只有那条路径带有界启动重试；提供自建池后 arq 的第一条
              Redis 命令是 ``log_redis_info(self.pool)``，它发生在设置 ``ctx['redis']``
              与调用 ``on_startup`` **之前**，因此 worker 生命周期里的连通性等待根本
              来不及执行。worker 与 Redis 同时启动、Redis 短暂未就绪时进程会直接退出，
              任务消费与全部维护 cron 一起不可用——没有任何兜底。
            - API 传 ``0``，即只尝试一次。立即调度失败会被
              ``DDLJobStore._activate_now_safely()`` 吞掉并退回 dispatch cron，受理承诺
              不受影响；在这里花掉整个连接预算只会把 HTTP 请求先挂死一遍，比快速失败
              后由 cron 重放严格更差。

    Returns:
        已设置 arq 运行期属性的队列客户端。
    """
    # 步骤一：沿用 DSN 解析出的连接参数，只补上 arq 不会设置的读取与健康检查配置。
    settings = RedisSettings.from_dsn(app_config.redis.url)
    # 步骤二：连接失败按角色预算重试，读取超时不重试。`retry_on_error` 是命令路径上的
    # 第二层门闩：`Redis._disconnect_raise` 会把不在其中的异常直接抛出，因此读取超时
    # 即使被 `Retry` 捕获也不会被重试。
    pool = ArqRedis(
        **_connection_target(settings),
        db=settings.database,
        username=settings.username,
        password=settings.password,
        ssl=settings.ssl,
        encoding="utf8",
        max_connections=settings.max_connections,
        retry=settings.retry or _connect_retry(settings, connect_retries),
        retry_on_timeout=settings.retry_on_timeout,
        retry_on_error=settings.retry_on_error or [RedisConnectionError],
        socket_connect_timeout=app_config.redis.socket_connect_timeout_seconds,
        socket_timeout=app_config.redis.socket_timeout_seconds,
        health_check_interval=app_config.redis.health_check_interval_seconds,
    )
    # 步骤三：补齐 create_pool 会设置的运行期属性，保持队列语义与 arq 默认一致。
    pool.job_serializer = None
    pool.job_deserializer = None
    pool.default_queue_name = default_queue_name
    pool.expires_extra_ms = expires_extra_ms
    return pool


def worker_connect_retries() -> int:
    """返回 worker 侧复刻 ``create_pool`` 启动语义所需的连接重试次数。"""
    return RedisSettings.from_dsn(app_config.redis.url).conn_retries
