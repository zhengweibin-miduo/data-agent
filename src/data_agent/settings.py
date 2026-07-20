"""应用配置模型及全局配置实例。"""

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


class SettingsModel(BaseModel):
    """配置模型基类，禁止传入未定义的配置字段。"""

    model_config = ConfigDict(extra="forbid")


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
    vector_size: int = Field(gt=0, description="Qdrant 记忆向量的维度。")
    distance: Literal["Cosine", "Dot", "Euclid"] = Field(
        description="Qdrant 记忆向量检索使用的距离度量。"
    )
    top_k: int = Field(
        gt=0,
        le=100,
        description="每次 Qdrant 向量检索返回的候选数量。",
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
    analyzer: str = Field(
        min_length=1,
        description="长期记忆全文索引使用的 Elasticsearch 分词器。",
    )
    top_k: int = Field(
        gt=0,
        le=100,
        description="每次 Elasticsearch 全文检索返回的候选数量。",
    )


class TEISettings(SettingsModel):
    """Text Embeddings Inference 连接配置。"""

    url: str = Field(description="Text Embeddings Inference 服务的 HTTP 地址。")
    vector_size: int = Field(gt=0, description="TEI 模型输出的向量维度。")


class MySQLSettings(SettingsModel):
    """MySQL 连接配置。"""

    url: str = Field(description="SQLAlchemy asyncmy 使用的 MySQL 连接地址。")


class APISettings(SettingsModel):
    """本地 HTTP API 配置。"""

    host: Literal["127.0.0.1"] = Field(description="HTTP API 监听的本机回环地址。")
    port: int = Field(ge=1, le=65535, description="HTTP API 监听端口。")
    cors_origins: list[HttpUrl] = Field(
        description="允许访问 HTTP API 的本机浏览器 Origin 列表。"
    )
    max_ddl_bytes: int = Field(gt=0, description="单次请求允许提交的 DDL 最大字节数。")
    max_tables: int = Field(gt=0, description="单次 DDL 解析允许包含的最大表数量。")
    max_columns: int = Field(gt=0, description="单次 DDL 解析允许包含的最大字段总数。")
    sse_heartbeat_seconds: float = Field(
        gt=0,
        le=300,
        description="DDL 任务 SSE 空闲心跳和 Redis 阻塞读取的间隔秒数。",
    )

    @field_validator("cors_origins")
    @classmethod
    def validate_local_origins(
        cls,
        origins: list[HttpUrl],
    ) -> list[HttpUrl]:
        """只允许本机浏览器 Origin。"""
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
        return origins


class RedisSettings(SettingsModel):
    """Redis、任务队列和恢复配置。"""

    url: str = Field(description="Redis 服务连接地址。")
    key_prefix: str = Field(min_length=1, description="应用写入 Redis 键时使用的前缀。")
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


class AppSettings(SettingsModel):
    """从 YAML 加载的应用根配置。"""

    logging: LoggingSettings = Field(description="应用日志配置。")
    qdrant: QdrantSettings = Field(description="Qdrant 向量索引配置。")
    elasticsearch: ElasticsearchSettings = Field(
        description="Elasticsearch 全文索引配置。"
    )
    tei: TEISettings = Field(description="Text Embeddings Inference 向量化服务配置。")
    mysql: MySQLSettings = Field(description="MySQL 持久化连接配置。")
    api: APISettings = Field(description="本地 HTTP API 配置。")
    redis: RedisSettings = Field(description="Redis、任务队列和恢复配置。")
    llm: LLMSettings = Field(description="OpenAI 兼容模型调用配置。")
    memory: MemorySettings = Field(description="长期语义记忆配置。")
    conversation: ConversationSettings = Field(
        description="永久文本会话与异步长期记忆提炼配置。"
    )

    @model_validator(mode="after")
    def validate_source_lease_window(self) -> "AppSettings":
        """校验来源租约和跨数据库边界。"""
        minimum = max(
            self.redis.waiting_timeout_seconds,
            self.redis.worker_job_timeout_seconds,
        )
        if self.memory.source_lease_seconds < minimum:
            raise ValueError("memory.source_lease_seconds 不能短于 worker 或等待超时")
        mysql_database = make_url(self.mysql.url).database
        if mysql_database is None:
            raise ValueError("mysql.url 必须包含默认 Meta 数据库")
        if mysql_database.casefold() == self.memory.database.casefold():
            raise ValueError("memory.database 不能与 mysql.url 的默认数据库相同")
        if self.qdrant.vector_size != self.tei.vector_size:
            raise ValueError("qdrant.vector_size 必须与 tei.vector_size 一致")
        return self

    @classmethod
    def from_yaml(
        cls,
        path: str | Path = Path(__file__).parents[2] / "conf" / "app_config.yaml",
    ) -> "AppSettings":
        """从 YAML 文件加载并校验应用配置。

        Args:
            path: YAML 配置文件路径。

        Returns:
            校验后的应用配置。
        """
        with Path(path).open(encoding="utf-8") as file:
            return cls.model_validate(yaml.safe_load(file))


# 仅加载一次，供所有应用模块共享。
app_config = AppSettings.from_yaml()
