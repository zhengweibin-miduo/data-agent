"""OpenAI 兼容聊天模型生命周期管理。"""

import os
from typing import ClassVar

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr

from app.conf.app_config import app_config


class _CapabilityProbe(BaseModel):
    """结构化输出能力探针。"""

    ok: bool


class LlmClientManager:
    """管理 OpenAI 兼容的异步聊天模型。"""

    _client: ClassVar[ChatOpenAI | None] = None

    @classmethod
    def initialize(cls) -> ChatOpenAI:
        """使用仅来自环境变量的密钥创建聊天模型。"""
        if cls._client is None:
            api_key = os.environ.get("DATA_AGENT_LLM_API_KEY")
            if not api_key:
                raise RuntimeError("缺少环境变量 DATA_AGENT_LLM_API_KEY")
            cls._client = ChatOpenAI(
                model=app_config.llm.model,
                base_url=app_config.llm.base_url,
                api_key=SecretStr(api_key),
                timeout=app_config.llm.request_timeout_seconds,
                max_retries=app_config.llm.max_retries,
                temperature=0,
            )
        return cls._client

    @classmethod
    def get_client(cls) -> ChatOpenAI:
        """返回已初始化的聊天模型。"""
        if cls._client is None:
            raise RuntimeError(
                "LLM 客户端尚未初始化，请先调用 LlmClientManager.initialize()"
            )
        return cls._client

    @classmethod
    async def check_structured_output_capability(cls) -> None:
        """实际验证配置的结构化输出方法，不做文本降级。"""
        runnable = cls.get_client().with_structured_output(
            _CapabilityProbe,
            method=app_config.llm.structured_output_method,
        )
        result = await runnable.ainvoke(
            "Return JSON with ok=true. Do not include any other fields."
        )
        if not isinstance(result, _CapabilityProbe) or not result.ok:
            raise RuntimeError("LLM 端点不支持配置的结构化输出方法")

    @classmethod
    async def close(cls) -> None:
        """释放模型持有的异步 HTTP 客户端。"""
        client = cls._client
        cls._client = None
        if client is not None:
            await client.root_async_client.close()
