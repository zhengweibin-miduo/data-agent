"""应用配置模型及全局配置实例。"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


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


class AppConfigModel(ConfigModel):
    """从 YAML 加载的应用根配置。"""

    logging: LoggingConfig
    qdrant: QdrantConfig
    elasticsearch: ElasticsearchConfig
    tei: TeiConfig
    mysql: MysqlConfig

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
