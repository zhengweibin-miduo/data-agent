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
        # 步骤一：以共享实例作为幂等门禁，避免重复创建 TEI HTTP 客户端。
        if cls._client is None:
            # 步骤二：规范化自托管端点，并只注入支持 URL 的异步推理客户端。
            endpoint = f"{app_config.tei.url.rstrip('/')}/embed"
            # AsyncInferenceClient 默认不设超时；缺少超时的向量化调用会让索引
            # 调度的整个 cron 周期永久挂起，失败重试由记忆索引 outbox 负责。
            cls._client = TEIEmbeddings.model_construct(
                client=None,
                async_client=AsyncInferenceClient(
                    model=endpoint,
                    timeout=app_config.tei.request_timeout_seconds,
                ),
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
        # 步骤一：未初始化或已关闭时直接返回，保持关闭操作幂等。
        if cls._client is None:
            return

        # 步骤二：关闭底层异步推理客户端后清除共享引用，允许后续重新初始化。
        await cls._client.async_client.close()
        cls._client = None
