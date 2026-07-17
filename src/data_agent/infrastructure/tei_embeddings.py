"""Hugging Face TEI 异步 embedding 客户端生命周期管理。"""

from typing import ClassVar

from huggingface_hub import AsyncInferenceClient
from langchain_huggingface import HuggingFaceEndpointEmbeddings

from data_agent.settings import app_config

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class TEIEmbeddings(HuggingFaceEndpointEmbeddings):
    """为 BGE query 添加检索指令。"""

    async def aembed_query(self, text: str) -> list[float]:
        """异步生成带中文检索指令的查询向量。"""
        return (await self.aembed_documents([f"{QUERY_INSTRUCTION}{text}"]))[0]


class TEIEmbeddingClient:
    """管理全局 TEI embedding 客户端。"""

    _client: ClassVar[TEIEmbeddings | None] = None

    @classmethod
    def initialize(cls) -> TEIEmbeddings:
        """初始化并返回客户端，重复调用时复用现有实例。"""
        if cls._client is None:
            endpoint = f"{app_config.tei.url.rstrip('/')}/embed"
            cls._client = TEIEmbeddings.model_construct(
                client=None,
                async_client=AsyncInferenceClient(model=endpoint),
                model=endpoint,
                repo_id=endpoint,
                task="feature-extraction",
                model_kwargs={"normalize": True, "truncate": True},
                huggingfacehub_api_token=None,
            )

        return cls._client

    @classmethod
    def get_client(cls) -> TEIEmbeddings:
        """返回已初始化的客户端。"""
        if cls._client is None:
            raise RuntimeError(
                "TEI embedding 客户端尚未初始化，请先调用 "
                "TEIEmbeddingClient.initialize()"
            )

        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭客户端并清除当前实例。"""
        if cls._client is None:
            return

        await cls._client.async_client.close()
        cls._client = None
