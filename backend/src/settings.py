"""应用配置模型及全局配置实例。"""

import os
import re
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import make_url

from errors import DataAgentError


class SettingsModel(BaseModel):
    """配置模型基类，禁止传入未定义的配置字段。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class FileLoggingSettings(SettingsModel):
    """文件日志配置。"""

    enable: bool = Field(description="是否启用文件日志输出。")
    level: str = Field(description="写入日志文件的最低日志级别。")
    format: Literal["text", "json"] = Field(
        description="文件日志的输出格式，可选纯文本或 JSON。"
    )
    path: Path = Field(description="日志文件的写入目录。")
    rotation: str = Field(description="单个日志文件触发轮转的大小条件。")
    retention: str = Field(description="轮转后日志文件的保留时长。")


class ConsoleLoggingSettings(SettingsModel):
    """控制台日志配置。"""

    enable: bool = Field(description="是否启用控制台日志输出。")
    level: str = Field(description="输出到控制台的最低日志级别。")
    format: Literal["text", "json"] = Field(
        description="控制台日志的输出格式，可选纯文本或 JSON。"
    )


class LoggingSettings(SettingsModel):
    """日志配置。"""

    service_name: str = Field(description="写入日志上下文的服务名称。")
    deployment_environment: str = Field(description="写入日志上下文的部署环境名称。")
    file: FileLoggingSettings = Field(description="文件日志输出配置。")
    console: ConsoleLoggingSettings = Field(description="控制台日志输出配置。")


class QdrantSettings(SettingsModel):
    """Qdrant 连接配置。"""

    url: str = Field(description="Qdrant 服务的 HTTP 地址。")
    api_key: str | None = Field(
        default=None,
        description="Qdrant 启用身份认证时使用的可选 API 密钥。",
    )
    memory_collection: str = Field(
        min_length=1,
        description="存储长期记忆向量的 Qdrant 集合名称。",
    )
    metadata_collection: str = Field(
        min_length=1,
        description="存储 Meta 表、字段和指标语义的 Qdrant 集合名称。",
    )
    vector_size: int = Field(gt=0, description="Qdrant 记忆向量的维度。")
    distance: Literal["Cosine", "Dot", "Euclid"] = Field(
        description="Qdrant 记忆向量检索使用的距离度量。"
    )
    top_k: int = Field(
        gt=0,
        le=100,
        description="每次 Qdrant 向量检索返回的候选数量。",
    )
    timeout_seconds: int = Field(
        gt=0,
        description="单次 Qdrant 请求允许等待响应的超时秒数。",
    )


class ElasticsearchSettings(SettingsModel):
    """Elasticsearch 连接配置。"""

    url: str = Field(description="Elasticsearch 服务的 HTTP 地址。")
    api_key: str | None = Field(
        default=None,
        description="Elasticsearch 启用身份认证时使用的可选 API 密钥。",
    )
    memory_index: str = Field(
        min_length=1,
        description="存储长期记忆全文索引的 Elasticsearch 索引名称。",
    )
    metadata_value_index: str = Field(
        min_length=1,
        description="存储 Meta 字段业务值的 Elasticsearch 索引名称。",
    )
    analyzer: str = Field(
        min_length=1,
        description="长期记忆全文索引使用的 Elasticsearch 分词器。",
    )
    top_k: int = Field(
        gt=0,
        le=100,
        description="每次 Elasticsearch 全文检索返回的候选数量。",
    )
    request_timeout_seconds: float = Field(
        gt=0,
        description="单次 Elasticsearch 请求允许等待响应的超时秒数。",
    )
    max_retries: int = Field(
        ge=0,
        description="单次 Elasticsearch 请求失败后的最大重试次数。",
    )


class TEISettings(SettingsModel):
    """Text Embeddings Inference 连接配置。"""

    url: str = Field(description="Text Embeddings Inference 服务的 HTTP 地址。")
    vector_size: int = Field(gt=0, description="TEI 模型输出的向量维度。")
    request_timeout_seconds: float = Field(
        gt=0,
        description="单次 Text Embeddings Inference 请求的超时秒数。",
    )


class MySQLSettings(SettingsModel):
    """MySQL 连接配置。"""

    url: str = Field(description="SQLAlchemy asyncmy 使用的 MySQL 连接地址。")


class QuerySettings(SettingsModel):
    """自然语言查询的专用只读 DW 连接与流式预算。"""

    read_url: str = Field(description="仅授予 DW SELECT 权限的 asyncmy 连接地址。")
    timeout_seconds: float = Field(
        default=10,
        gt=0,
        le=60,
        description="EXPLAIN 或业务查询允许占用的最长秒数。",
    )
    fetch_batch_rows: int = Field(
        default=500,
        gt=0,
        le=500,
        description="单个 NDJSON 结果批次允许包含的最大行数。",
    )
    max_batch_bytes: int = Field(
        default=1_048_576,
        ge=4096,
        le=16_777_216,
        description="单个 NDJSON 结果批次允许占用的最大字节数。",
    )

    @field_validator("read_url")
    @classmethod
    def validate_read_url(cls, value: str) -> str:
        """校验查询连接使用 asyncmy 且显式选择数据库。"""
        url = make_url(value)
        if url.drivername != "mysql+asyncmy" or url.database is None:
            raise ValueError("query.read_url 必须是包含数据库名的 mysql+asyncmy 地址")
        return value


class DataSyncSourceSettings(SettingsModel):
    """命名 MySQL 业务数据源配置。"""

    url: str = Field(description="仅在服务端使用的源 MySQL 连接地址。")
    server_id: int = Field(
        gt=0,
        le=4_294_967_295,
        description="读取该数据源 Binlog 时使用的唯一复制客户端编号。",
    )

    @field_validator("url")
    @classmethod
    def validate_mysql_url(cls, value: str) -> str:
        """校验源连接使用受支持的异步 MySQL 驱动且包含数据库名。"""
        url = make_url(value)
        if url.drivername != "mysql+asyncmy" or url.database is None:
            raise ValueError(
                "data_sync.sources.*.url 必须是包含数据库名的 mysql+asyncmy 地址"
            )
        return value


class DataSyncSettings(SettingsModel):
    """DW 结构与 MySQL Binlog 数据同步配置。"""

    database: str = Field(
        default="data_sync",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="保存同步任务、事件、位点和冲突状态的 MySQL 数据库名称。",
    )
    dw_database: str = Field(
        default="dw",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="保存同步后业务行数据的 DW MySQL 数据库名称。",
    )
    sources: dict[str, DataSyncSourceSettings] = Field(
        min_length=1,
        description="按 DDL source 键选择的命名 MySQL 业务数据源。",
    )
    claim_lease_seconds: int = Field(
        gt=0,
        description="同步任务单次领取在数据库中保持有效的秒数。",
    )
    generation_lock_timeout_seconds: int = Field(
        gt=0,
        le=300,
        description="发布或执行同一 DW generation 前等待共享命名锁的最大秒数。",
    )
    retry_base_seconds: int = Field(
        gt=0,
        description="同步任务首次失败后的退避秒数。",
    )
    retry_max_seconds: int = Field(
        gt=0,
        description="同步任务指数退避允许达到的最大秒数。",
    )
    max_attempts: int = Field(
        gt=0,
        description="同步任务转入死信状态前允许的最大失败次数。",
    )
    backfill_batch_size: int = Field(
        gt=0,
        le=10_000,
        description="历史回填按主键每批最多读取和写入的业务行数。",
    )
    backfill_interval_seconds: float = Field(
        ge=0,
        le=60,
        description="历史回填相邻批次之间主动暂停的秒数。",
    )
    event_buffer_limit: int = Field(
        gt=0,
        le=1_000_000,
        description="单个同步任务允许暂存的未确认 Binlog 行事件上限。",
    )
    event_cleanup_batch_size: int = Field(
        gt=0,
        le=10_000,
        description="每次清理已确认 Binlog 暂存事件的最大数量。",
    )
    poll_interval_seconds: float = Field(
        gt=0,
        le=60,
        description="专用 CDC 进程在没有可执行任务时的轮询间隔秒数。",
    )
    source_connect_timeout_seconds: int = Field(
        gt=0,
        description="建立源 MySQL 或 Binlog 连接允许等待的秒数。",
    )
    source_read_timeout_seconds: int = Field(
        gt=0,
        description="源 MySQL 查询或 Binlog 读取允许等待响应的秒数。",
    )

    @field_validator("sources")
    @classmethod
    def validate_sources(
        cls,
        sources: dict[str, DataSyncSourceSettings],
    ) -> dict[str, DataSyncSourceSettings]:
        """校验来源键和复制客户端编号在忽略大小写后唯一。"""
        source_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
        normalized: set[str] = set()
        server_ids: set[int] = set()
        for name, source in sources.items():
            folded = name.casefold()
            if not source_pattern.fullmatch(name) or folded in normalized:
                raise ValueError("data_sync.sources 的来源名称无效或重复")
            if source.server_id in server_ids:
                raise ValueError("data_sync.sources 的 server_id 必须唯一")
            normalized.add(folded)
            server_ids.add(source.server_id)
        return sources


class APISettings(SettingsModel):
    """本地 HTTP API 配置。"""

    host: Literal["127.0.0.1"] = Field(description="HTTP API 监听的本机回环地址。")
    port: int = Field(ge=1, le=65535, description="HTTP API 监听端口。")
    cors_origins: list[HttpUrl] = Field(
        description="允许访问 HTTP API 的浏览器 Origin 精确列表。"
    )
    allow_remote_cors_origins: bool = Field(
        default=False,
        description="是否显式允许 cors_origins 包含非本机 Origin。",
    )
    max_ddl_bytes: int = Field(gt=0, description="单次请求允许提交的 DDL 最大字节数。")
    max_tables: int = Field(gt=0, description="单次 DDL 解析允许包含的最大表数量。")
    max_columns: int = Field(gt=0, description="单次 DDL 解析允许包含的最大字段总数。")
    sse_heartbeat_seconds: float = Field(
        gt=0,
        le=300,
        description="DDL 任务 SSE 空闲心跳和 Redis 阻塞读取的间隔秒数。",
    )

    @model_validator(mode="after")
    def validate_cors_origins(self) -> "APISettings":
        """默认限制本机 Origin，显式部署开关允许精确的远端 Origin。"""
        if self.allow_remote_cors_origins:
            return self
        origins = self.cors_origins
        # 步骤一：逐个解析 Origin 主机，仅允许 localhost 或 IP 回环地址通过。
        for origin in origins:
            host = origin.host or ""
            if host == "localhost":
                continue
            try:
                is_loopback = ip_address(host.strip("[]")).is_loopback
            except ValueError:
                is_loopback = False
            if not is_loopback:
                raise ValueError("api.cors_origins 只能配置本机 Origin")
        # 步骤二：返回保留原始顺序与类型的已校验 Origin 列表。
        return self


class RedisSettings(SettingsModel):
    """Redis、任务队列和恢复配置。"""

    url: str = Field(description="Redis 服务连接地址。")
    key_prefix: str = Field(min_length=1, description="应用写入 Redis 键时使用的前缀。")
    socket_timeout_seconds: float = Field(
        gt=0,
        description="单次 Redis 命令等待响应的套接字超时秒数，必须大于 SSE 心跳间隔。",
    )
    socket_connect_timeout_seconds: float = Field(
        gt=0,
        description="建立 Redis 连接允许等待的套接字超时秒数。",
    )
    health_check_interval_seconds: float = Field(
        gt=0,
        description="Redis 连接空闲后复用前执行健康检查的间隔秒数。",
    )
    checkpoint_retention_seconds: int = Field(
        gt=0,
        description="LangGraph 检查点在 Redis 中的保留秒数。",
    )
    result_retention_seconds: int = Field(
        gt=0,
        description="DDL 任务结果及状态在 Redis 中的保留秒数。",
    )
    event_stream_max_events: int = Field(
        gt=0,
        le=10000,
        description="每个 DDL 任务事件 Stream 近似保留的最大事件数。",
    )
    waiting_timeout_seconds: int = Field(
        gt=0,
        description="DDL 任务等待用户补充信息的超时秒数。",
    )
    worker_concurrency: int = Field(
        gt=0, description="arq 工作进程允许并发执行的任务数量。"
    )
    worker_job_timeout_seconds: int = Field(
        gt=0,
        description="单个 arq 任务允许执行的最长秒数。",
    )
    job_stall_grace_seconds: int = Field(
        gt=0,
        description="判定任务停滞时在单次执行超时之上额外等待的秒数。",
    )
    max_job_attempts: int = Field(
        gt=0,
        description="停滞任务被重新激活的累计尝试次数上限。",
    )


class LLMSettings(SettingsModel):
    """OpenAI 兼容模型配置。"""

    base_url: str = Field(description="OpenAI 兼容模型服务的基础地址。")
    model: str = Field(min_length=1, description="生成 DDL 元数据时调用的模型名称。")
    request_timeout_seconds: float = Field(gt=0, description="单次模型请求的超时秒数。")
    semantic_confidence_threshold: float = Field(
        ge=0,
        le=1,
        description="语义元数据通过自动校验所需的最低置信度。",
    )
    structured_output_method: Literal["json_schema", "function_calling"] = Field(
        description="模型生成结构化结果时使用的 OpenAI 兼容调用方式。"
    )
    max_concurrency: int = Field(gt=0, description="语义元数据模型请求的最大并发数。")
    max_retries: int = Field(ge=0, description="单次模型请求失败后的最大重试次数。")
    prompt_version: str = Field(
        min_length=1, description="生成语义元数据所用提示词的版本标识。"
    )
    graph_version: str = Field(
        min_length=1, description="DDL 元数据工作流图的版本标识。"
    )


class MemorySettings(SettingsModel):
    """长期语义记忆配置。"""

    database: str = Field(
        default="data_agent",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="存储权威长期记忆及索引发件箱的 MySQL 数据库名称。",
    )
    content_version: str = Field(
        min_length=1, description="长期记忆内容结构的版本标识。"
    )
    ddl_semantic_content_version: str = Field(
        min_length=1, description="DDL 语义记忆内容结构的版本标识。"
    )
    projection_version: str = Field(
        min_length=1, description="长期记忆派生索引结构的版本标识。"
    )
    source_lease_seconds: int = Field(
        gt=0,
        description="同一 DDL 来源保持任务独占租约的秒数。",
    )
    rebuild_batch_size: int = Field(
        gt=0,
        le=1000,
        description="重建派生记忆索引时每批读取的记忆数量。",
    )
    outbox_batch_size: int = Field(
        gt=0,
        le=1000,
        description="每轮处理记忆索引发件箱的记录数量。",
    )
    outbox_max_backoff_seconds: int = Field(
        gt=0,
        description="记忆索引发件箱失败重试的最大退避秒数。",
    )
    outbox_claim_lease_seconds: int = Field(
        gt=0,
        description="记忆索引发件箱单批领取的租约秒数，超时后未确认项自动可重领。",
    )
    outbox_max_attempts: int = Field(
        gt=0,
        description="记忆索引发件箱单项转入死信前允许的最大失败次数。",
    )
    retrieval_timeout_seconds: float = Field(
        gt=0,
        description="单个长期记忆检索后端允许响应的超时秒数。",
    )
    rrf_constant: int = Field(
        gt=0, description="融合全文与向量检索排名时使用的 RRF 常量。"
    )
    search_limit: int = Field(
        gt=0,
        le=100,
        description="一次长期记忆搜索最多返回的结果数量。",
    )


class MetadataIndexSettings(SettingsModel):
    """Meta 语义和值派生索引配置。"""

    projection_version: str = Field(min_length=1, description="Meta 索引投影版本。")
    value_top_n: int = Field(gt=0, le=10_000, description="每字段保留的高频值上限。")
    value_scan_batch_size: int = Field(
        gt=0,
        le=10_000,
        description="字段值初始扫描每个工作单元最多读取的 DW 行数。",
    )
    value_bulk_batch_size: int = Field(
        gt=0,
        le=500,
        description="字段值发布或清理每个工作单元最多处理的文档数。",
    )
    dispatch_batch_size: int = Field(gt=0, le=1000, description="每轮领取任务数。")
    claim_lease_seconds: int = Field(gt=0, description="索引任务领取租约秒数。")
    retry_max_seconds: int = Field(gt=0, description="索引任务最大退避秒数。")
    max_attempts: int = Field(gt=0, description="索引任务死信前最大失败次数。")
    debounce_seconds: int = Field(ge=0, le=3600, description="表级值刷新合并窗口秒数。")
    search_limit: int = Field(gt=0, le=100, description="内部检索默认返回数。")


class ConversationSettings(SettingsModel):
    """永久文本会话与异步记忆提炼配置。"""

    max_message_chars: int = Field(
        gt=0,
        le=65535,
        description="单条用户或助手文本消息允许的最大字符数。",
    )
    context_message_limit: int = Field(
        gt=0,
        le=100,
        description="组装模型上下文时最多读取的最近消息数量。",
    )
    context_max_chars: int = Field(
        gt=0,
        description="组装模型上下文时最近消息允许占用的最大字符数。",
    )
    summary_max_chars: int = Field(
        gt=0,
        description="会话摘要允许保存和返回的最大字符数。",
    )
    extraction_batch_size: int = Field(
        gt=0,
        le=100,
        description="每轮异步提炼领取的完成对话轮次数量。",
    )
    extraction_lease_seconds: int = Field(
        gt=0,
        description="对话记忆提炼任务的领取租约秒数。",
    )
    turn_lease_seconds: int = Field(
        gt=0,
        description="活动对话轮次未完成时保持门禁独占的租约秒数。",
    )


class AppSettings(SettingsModel):
    """从 YAML 加载的应用根配置。"""

    logging: LoggingSettings = Field(description="应用日志配置。")
    qdrant: QdrantSettings = Field(description="Qdrant 向量索引配置。")
    elasticsearch: ElasticsearchSettings = Field(
        description="Elasticsearch 全文索引配置。"
    )
    tei: TEISettings = Field(description="Text Embeddings Inference 向量化服务配置。")
    mysql: MySQLSettings = Field(description="MySQL 持久化连接配置。")
    query: QuerySettings = Field(description="自然语言查询的专用只读 DW 配置。")
    data_sync: DataSyncSettings = Field(description="DW 结构与 Binlog 数据同步配置。")
    api: APISettings = Field(description="本地 HTTP API 配置。")
    redis: RedisSettings = Field(description="Redis、任务队列和恢复配置。")
    llm: LLMSettings = Field(description="OpenAI 兼容模型调用配置。")
    memory: MemorySettings = Field(description="长期语义记忆配置。")
    metadata_index: MetadataIndexSettings = Field(description="Meta 派生索引配置。")
    conversation: ConversationSettings = Field(
        description="永久文本会话与异步长期记忆提炼配置。"
    )

    @model_validator(mode="after")
    def validate_source_lease_window(self) -> "AppSettings":
        """校验来源租约和跨数据库边界。"""
        # 步骤一：租约只在激活开始时续期，此后可能先执行满 worker 超时再进入人工
        # 等待，两段占用串联而非互斥，因此租约必须覆盖它们的和。
        minimum = (
            self.redis.waiting_timeout_seconds + self.redis.worker_job_timeout_seconds
        )
        if self.memory.source_lease_seconds < minimum:
            raise ValueError(
                "memory.source_lease_seconds 不能短于 worker 超时与等待超时之和"
            )
        # 步骤二：确认连接地址含 Meta 默认库，并让三个应用数据库彼此独立。
        mysql_database = make_url(self.mysql.url).database
        if mysql_database is None:
            raise ValueError("mysql.url 必须包含默认 Meta 数据库")
        databases = (
            mysql_database,
            self.memory.database,
            self.data_sync.database,
            self.data_sync.dw_database,
        )
        if len({database.casefold() for database in databases}) != len(databases):
            raise ValueError(
                "mysql 默认库、memory.database、data_sync.database 和 "
                "data_sync.dw_database 必须彼此不同"
            )
        query_url = make_url(self.query.read_url)
        if query_url.database is None or (
            query_url.database.casefold() != self.data_sync.dw_database.casefold()
        ):
            raise ValueError("query.read_url 必须连接 data_sync.dw_database")
        if query_url.username == make_url(self.mysql.url).username:
            raise ValueError("query.read_url 必须使用独立于应用写连接的数据库账号")
        # 步骤三：校验向量生成与向量存储维度一致，避免运行时写入失败。
        if self.qdrant.vector_size != self.tei.vector_size:
            raise ValueError("qdrant.vector_size 必须与 tei.vector_size 一致")
        # 步骤四：Meta 与长期记忆索引必须使用不同目标，防止重建误删另一业务投影。
        if (
            self.qdrant.metadata_collection.casefold()
            == self.qdrant.memory_collection.casefold()
        ):
            raise ValueError("qdrant.metadata_collection 必须与 memory_collection 不同")
        if (
            self.elasticsearch.metadata_value_index.casefold()
            == self.elasticsearch.memory_index.casefold()
        ):
            raise ValueError(
                "elasticsearch.metadata_value_index 必须与 memory_index 不同"
            )
        # 步骤五：SSE 事件流用 xread 阻塞满一个心跳间隔属于正常空闲行为，而
        # socket_timeout 是单次命令的读取超时；两者相等或更小会把正常心跳变成
        # 套接字超时并打断事件流，因此必须严格大于心跳间隔。
        if self.redis.socket_timeout_seconds <= self.api.sse_heartbeat_seconds:
            raise ValueError(
                "redis.socket_timeout_seconds 必须大于 api.sse_heartbeat_seconds"
            )
        # 步骤六：返回完成跨配置约束校验的根配置实例。
        return self

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> "AppSettings":
        """从 YAML 文件加载并校验应用配置。

        Args:
            path: YAML 配置文件路径；为 None 时按 `resolve_config_path` 顺序解析。

        Returns:
            校验后的应用配置。
        """
        # 步骤一：先确定唯一目标文件，使"配置来自哪里"始终可解释。
        resolved = resolve_config_path(path)
        # 步骤二：以 UTF-8 打开明确目标文件，让 YAML 解析异常原样传播。
        with resolved.open(encoding="utf-8") as file:
            # 步骤三：解析 YAML 后一次性交给 Pydantic 完成字段与跨配置约束校验。
            return cls.model_validate(yaml.safe_load(file))


CONFIG_PATH_ENV = "DATA_AGENT_CONFIG"

_CONFIG_FILENAME = Path("conf") / "app_config.yaml"
_SOURCE_TREE_CONFIG = Path(__file__).parents[1] / _CONFIG_FILENAME


def _config_not_found(candidates: list[Path]) -> DataAgentError:
    """构造列出全部候选路径的配置缺失错误。"""
    searched = ", ".join(str(candidate.absolute()) for candidate in candidates)
    return DataAgentError(
        "config_not_found",
        "settings",
        f"未找到应用配置文件，已查找：{searched}；可用 {CONFIG_PATH_ENV} 指定路径",
        http_status=500,
        details={"searched": searched, "env": CONFIG_PATH_ENV},
    )


def resolve_config_path(path: str | Path | None = None) -> Path:
    """按固定优先级解析应用配置文件位置。

    顺序为：显式实参、`DATA_AGENT_CONFIG` 环境变量、当前工作目录下的
    `conf/app_config.yaml`、源码树相对位置。工作目录先于源码树，使安装后的部署
    目录配置优先于恰好可见的源码树配置。

    Args:
        path: 调用方显式指定的配置路径。

    Returns:
        存在的配置文件绝对路径。

    Raises:
        DataAgentError: 显式指定或环境变量指向的文件不存在，或全部候选都不存在。
    """
    # 步骤一：显式实参与环境变量都是明确意图，指向的文件不存在必须直接失败；
    # 静默回退会让调用方以为换了配置而实际仍加载旧文件，比启动失败更难排查。
    explicit = path if path is not None else os.environ.get(CONFIG_PATH_ENV)
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise _config_not_found([candidate])
        return candidate.resolve()
    # 步骤二：其余候选按顺序探测，命中即返回；工作目录优先于源码树。
    candidates = [Path.cwd() / _CONFIG_FILENAME, _SOURCE_TREE_CONFIG]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    # 步骤三：全部缺失时列出查找过的绝对路径，避免只报一个裸文件名。
    raise _config_not_found(candidates)


_settings: AppSettings | None = None


def get_settings(path: str | Path | None = None) -> AppSettings:
    """返回进程内共享的应用配置，首次调用时解析并校验。

    已有缓存时直接返回缓存，`path` 不参与缓存键；需要改用其它配置文件时先调用
    `reset_settings()`，使"重新加载"始终是一个显式动作。

    Args:
        path: 首次加载时使用的配置路径；为 None 时按解析顺序确定。

    Returns:
        进程内共享的应用配置。
    """
    global _settings
    # 步骤一：缓存命中直接复用，保证所有组件共享同一份校验结果。
    if _settings is None:
        # 步骤二：首次加载才解析文件并执行跨配置约束校验。
        _settings = AppSettings.from_yaml(path)
    return _settings


def reset_settings() -> None:
    """清除进程内配置缓存，使下次 `get_settings` 重新解析。"""
    global _settings
    _settings = None


# 步骤一：模块导入时只加载一次配置，供所有应用组件共享同一校验结果。后续把
# import-time 求值点改为惰性访问时，替换点就是这里而不是各个调用方。
app_config: AppSettings = get_settings()
