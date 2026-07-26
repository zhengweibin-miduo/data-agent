"""arq 队列客户端构造，集中注入 arq 自身不会设置的套接字超时。

API 与 worker 都需要一个 arq 队列连接：API 在受理路径上立即调度激活，worker 用它
领取任务并驱动全部维护 cron。两处都不能用 ``arq.create_pool``：它只把
``RedisSettings.conn_timeout`` 映射到 ``socket_connect_timeout``，从不设置
``socket_timeout``。既有连接半开、或 Redis 收下命令后不再响应时，缺少读取超时的调用
永远不返回——API 侧表现为 HTTP 请求挂死，worker 侧表现为停止领取任务。

两处共用同一个构造函数，避免其中一处补了超时另一处漏掉：这正是本模块出现的原因。
"""

from typing import Any

from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name, expires_extra_ms
from redis.asyncio.retry import Retry
from redis.backoff import ConstantBackoff
from redis.exceptions import ConnectionError as RedisConnectionError

from data_agent.settings import app_config


def _connection_target(settings: RedisSettings) -> dict[str, Any]:
    """解析出互斥的 Unix socket 或 TCP 连接目标。

    ``unix://`` DSN 会被 arq 解析进 ``unix_socket_path``；漏传它会让调用方连回 TCP
    ``localhost:6379``，与另一侧连到不同实例，全部任务领取与维护 cron 失效。
    """
    if settings.unix_socket_path:
        return {"unix_socket_path": settings.unix_socket_path}
    return {"host": settings.host, "port": settings.port}


def _startup_retry(settings: RedisSettings) -> Retry:
    """构造与 ``create_pool`` 等价的有界连接重试。

    ``Worker.main()`` 只在未提供 ``redis_pool`` 时才调用 ``create_pool``，也只有那条
    路径带有界启动重试。提供自建池后，arq 的第一条 Redis 命令是
    ``log_redis_info(self.pool)``，它发生在设置 ``ctx['redis']`` 与调用 ``on_startup``
    **之前**；因此 worker 生命周期里的连通性等待根本来不及执行，worker 与 Redis 同时
    启动、Redis 短暂未就绪时进程会直接退出，任务消费与全部维护 cron 一起不可用。

    redis-py 把 ``connect()`` 与命令/pipeline 执行都包在连接级 ``Retry`` 里，因此在池
    上配置有界重试，等价于"把有界重试放在 arq 的首次 Redis 命令之前"，而且无需依赖
    arq 的内部启动顺序。重试预算沿用 ``RedisSettings`` 的 ``conn_retries`` 与
    ``conn_retry_delay``，与 ``create_pool`` 的启动语义保持一致。
    """
    return Retry(
        ConstantBackoff(settings.conn_retry_delay),
        settings.conn_retries,
    )


def build_queue_pool() -> ArqRedis:
    """构造带显式读取超时与有界连接重试的 arq 队列客户端。

    arq worker 的队列轮询是非阻塞的（``poll_delay`` 默认 0.5 秒，源码中没有
    BLPOP/XREAD 之类的阻塞读取），API 侧只做 ``enqueue_job``，因此统一的读取超时不会
    误伤正常空闲等待。

    Returns:
        已设置 arq 运行期属性的队列客户端。
    """
    # 步骤一：沿用 DSN 解析出的连接参数，只补上 arq 不会设置的读取与健康检查配置。
    settings = RedisSettings.from_dsn(app_config.redis.url)
    # 步骤二：连接失败按有界预算重试，读取超时不重试。读取超时是"服务收下命令却不
    # 响应"，重试只会把单次挂起变成多次挂起；连接失败才是启动竞争与瞬时断连。
    pool = ArqRedis(
        **_connection_target(settings),
        db=settings.database,
        username=settings.username,
        password=settings.password,
        ssl=settings.ssl,
        encoding="utf8",
        max_connections=settings.max_connections,
        retry=settings.retry or _startup_retry(settings),
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
