"""Elasticsearch 客户端生命周期管理。"""

from typing import ClassVar

from elasticsearch import AsyncElasticsearch

from app.conf.app_config import app_config


class ElasticsearchClientManager:
    """管理全局异步 Elasticsearch 客户端的初始化、获取与关闭。"""

    _client: ClassVar[AsyncElasticsearch | None] = None

    @classmethod
    def initialize(cls) -> AsyncElasticsearch:
        """初始化并返回 Elasticsearch 客户端，重复调用时复用现有实例。"""
        if cls._client is None:
            cls._client = AsyncElasticsearch(
                hosts=app_config.elasticsearch.url,
                api_key=app_config.elasticsearch.api_key,
            )

        return cls._client

    @classmethod
    def get_client(cls) -> AsyncElasticsearch:
        """返回已初始化的 Elasticsearch 客户端。"""
        if cls._client is None:
            raise RuntimeError(
                "Elasticsearch 客户端尚未初始化，请先调用 "
                "ElasticsearchClientManager.initialize()"
            )

        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭 Elasticsearch 客户端并清除当前实例。"""
        if cls._client is None:
            return

        await cls._client.close()
        cls._client = None
