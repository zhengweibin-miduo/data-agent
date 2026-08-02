"""OpenAI 兼容聊天模型生命周期管理。"""

import os
from typing import ClassVar

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr

from settings import app_config


class _CapabilityProbe(BaseModel):
    """结构化输出能力探针。"""

    ok: bool


class LLMClient:
    """管理 OpenAI 兼容的异步聊天模型。"""

    _client: ClassVar[ChatOpenAI | None] = None

    @classmethod
    def initialize(cls) -> ChatOpenAI:
        """使用仅来自环境变量的密钥创建聊天模型。"""
        # 步骤一：以共享实例作为幂等门禁，避免重复创建模型及其 HTTP 客户端。
        if cls._client is None:
            # 步骤二：仅从环境变量读取密钥，并在缺失时于启动阶段明确失败。
            api_key = os.environ.get("DATA_AGENT_LLM_API_KEY")
            if not api_key:
                raise RuntimeError("缺少环境变量 DATA_AGENT_LLM_API_KEY")
            # 步骤三：使用已校验配置创建零温度模型，保留底层异常供调用方分类。
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
            raise RuntimeError("LLM 客户端尚未初始化，请先调用 LLMClient.initialize()")
        return cls._client

    @classmethod
    async def check_structured_output_capability(cls) -> None:
        """实际验证配置的结构化输出方法，不做文本降级。"""
        # 步骤一：按配置方法绑定严格响应模型，禁止绕过类型契约的文本降级。
        runnable = cls.get_client().with_structured_output(
            _CapabilityProbe,
            method=app_config.llm.structured_output_method,
        )
        # 步骤二：向真实端点发送最小探针，验证部署能力而非仅验证本地对象构造。
        result = await runnable.ainvoke(
            "Return JSON with ok=true. Do not include any other fields."
        )
        # 步骤三：同时校验响应类型和值，拒绝声称成功但未履行契约的端点。
        if not isinstance(result, _CapabilityProbe) or not result.ok:
            raise RuntimeError("LLM 端点不支持配置的结构化输出方法")

    @classmethod
    async def close(cls) -> None:
        """释放模型持有的异步 HTTP 客户端。"""
        # 步骤一：先摘除共享引用，使关闭期间的新初始化不会被旧资源覆盖。
        client = cls._client
        cls._client = None
        # 步骤二：只关闭捕获实例持有的异步 HTTP 客户端，重复关闭保持幂等。
        if client is not None:
            await client.root_async_client.close()
