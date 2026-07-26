"""应用配置模型及全局配置实例。"""

import os
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

from data_agent.errors import DataAgentError


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
        # 步骤一：确保来源租约覆盖 worker 执行与人工等待两种最长占用窗口。
        minimum = max(
            self.redis.waiting_timeout_seconds,
            self.redis.worker_job_timeout_seconds,
        )
        if self.memory.source_lease_seconds < minimum:
            raise ValueError("memory.source_lease_seconds 不能短于 worker 或等待超时")
        # 步骤二：确认连接地址含 Meta 默认库，并让权威记忆使用不同数据库。
        mysql_database = make_url(self.mysql.url).database
        if mysql_database is None:
            raise ValueError("mysql.url 必须包含默认 Meta 数据库")
        if mysql_database.casefold() == self.memory.database.casefold():
            raise ValueError("memory.database 不能与 mysql.url 的默认数据库相同")
        # 步骤三：校验向量生成与向量存储维度一致，避免运行时写入失败。
        if self.qdrant.vector_size != self.tei.vector_size:
            raise ValueError("qdrant.vector_size 必须与 tei.vector_size 一致")
        # 步骤四：返回完成跨配置约束校验的根配置实例。
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
_SOURCE_TREE_CONFIG = Path(__file__).parents[2] / _CONFIG_FILENAME


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
