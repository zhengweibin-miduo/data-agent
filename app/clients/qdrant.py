"""全局异步 Qdrant 客户端。"""

from qdrant_client import AsyncQdrantClient

from app.conf.app import AppConfig


# 复用全局应用配置，避免重复读取 YAML 文件。
qdrant = AsyncQdrantClient(url=AppConfig.qdrant.url, api_key=AppConfig.qdrant.api_key)
