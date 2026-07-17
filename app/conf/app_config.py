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


class ConfigModel(BaseModel):
    """配置模型基类，禁止传入未定义的配置字段。"""

    model_config = ConfigDict(extra="forbid")


class LogFileConfig(ConfigModel):
    """文件日志配置。"""

    # 是否启用文件日志。
    enable: bool
    # 文件日志的最低级别。
    level: str
    # 日志文件写入目录。
    path: Path
    # 单个日志文件触发轮转的最大大小。
    rotation: str
    # 轮转日志的保留时长。
    retention: str


class LogConsoleConfig(ConfigModel):
    """控制台日志配置。"""

    # 是否启用控制台日志。
    enable: bool
    # 控制台日志的最低级别。
    level: str


class LoggingConfig(ConfigModel):
    """日志配置。"""

    file: LogFileConfig
    console: LogConsoleConfig


class QdrantConfig(ConfigModel):
    """Qdrant 连接配置。"""

    # Qdrant HTTP 地址。
    url: str
    # 启用身份认证时使用的可选 API 密钥。
    api_key: str | None = None


class ElasticsearchConfig(ConfigModel):
    """Elasticsearch 连接配置。"""

    # Elasticsearch HTTP 地址。
    url: str
    # 启用身份认证时使用的可选 API 密钥。
    api_key: str | None = None


class TeiConfig(ConfigModel):
    """Text Embeddings Inference 连接配置。"""

    # TEI HTTP 地址。
    url: str


class MysqlConfig(ConfigModel):
    """MySQL 连接配置。"""

    # SQLAlchemy asyncmy 连接地址。
    url: str


class ApiConfig(ConfigModel):
    """本地 HTTP API 配置。"""

    host: Literal["127.0.0.1"]
    port: int = Field(ge=1, le=65535)
    cors_origins: list[HttpUrl]
    max_ddl_bytes: int = Field(gt=0)
    max_tables: int = Field(gt=0)
    max_columns: int = Field(gt=0)

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


class RedisConfig(ConfigModel):
    """Redis、任务队列和恢复配置。"""

    url: str
    key_prefix: str = Field(min_length=1)
    checkpoint_retention_seconds: int = Field(gt=0)
    result_retention_seconds: int = Field(gt=0)
    waiting_timeout_seconds: int = Field(gt=0)
    worker_concurrency: int = Field(gt=0)
    worker_job_timeout_seconds: int = Field(gt=0)


class LlmConfig(ConfigModel):
    """OpenAI 兼容模型配置。"""

    base_url: str
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0)
    semantic_confidence_threshold: float = Field(ge=0, le=1)
    structured_output_method: Literal["json_schema", "function_calling"]
    max_concurrency: int = Field(gt=0)
    max_retries: int = Field(ge=0)
    prompt_version: str = Field(min_length=1)
    graph_version: str = Field(min_length=1)


class MemoryConfig(ConfigModel):
    """长期 LLM 记忆配置。"""

    database: str = Field(
        default="data_agent",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    content_version: str = Field(min_length=1)
    payload_version: str = Field(min_length=1)
    source_lease_seconds: int = Field(gt=0)
    rebuild_batch_size: int = Field(gt=0, le=1000)


class AppConfigModel(ConfigModel):
    """从 YAML 加载的应用根配置。"""

    logging: LoggingConfig
    qdrant: QdrantConfig
    elasticsearch: ElasticsearchConfig
    tei: TeiConfig
    mysql: MysqlConfig
    api: ApiConfig
    redis: RedisConfig
    llm: LlmConfig
    memory: MemoryConfig

    @model_validator(mode="after")
    def validate_source_lease_window(self) -> "AppConfigModel":
        """校验来源租约和跨数据库边界。"""
        minimum = max(
            self.redis.waiting_timeout_seconds,
            self.redis.worker_job_timeout_seconds,
        )
        if self.memory.source_lease_seconds < minimum:
            raise ValueError(
                "memory.source_lease_seconds 不能短于 worker 或等待超时"
            )
        mysql_database = make_url(self.mysql.url).database
        if mysql_database is None:
            raise ValueError("mysql.url 必须包含默认 Meta 数据库")
        if mysql_database.casefold() == self.memory.database.casefold():
            raise ValueError(
                "memory.database 不能与 mysql.url 的默认数据库相同"
            )
        return self

    @classmethod
    def from_yaml(
        cls,
        path: str | Path = Path(__file__).parents[2] / "conf" / "app_config.yaml",
    ) -> "AppConfigModel":
        """从 YAML 文件加载并校验应用配置。

        参数：
            path: YAML 配置文件路径。

        返回：
            校验后的应用配置。
        """
        with Path(path).open(encoding="utf-8") as file:
            return cls.model_validate(yaml.safe_load(file))


# 仅加载一次，供所有应用模块共享。
app_config = AppConfigModel.from_yaml()


if __name__ == "__main__":
    assert app_config.logging.file.path == Path("logs")
    assert app_config.qdrant.url == "http://localhost:6333"
    assert app_config.elasticsearch.url == "http://localhost:9200"
    assert app_config.tei.url == "http://localhost:8080"
    assert app_config.mysql.url.startswith("mysql+asyncmy://")
    assert app_config.memory.database == "data_agent"
    assert app_config.api.host == "127.0.0.1"
    assert app_config.redis.url.startswith("redis://")
    assert app_config.llm.structured_output_method in {
        "json_schema",
        "function_calling",
    }
