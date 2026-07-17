"""OpenAI 兼容模型客户端配置与能力检查。"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app.client.llm_client_manager import (
    LlmClientManager,
    _CapabilityProbe,
)
from app.conf.app_config import app_config


async def _test_manager() -> None:
    """验证环境密钥、复用、结构化能力探针和关闭。"""
    await LlmClientManager.close()
    with patch.dict(
        "os.environ",
        {"DATA_AGENT_LLM_API_KEY": "test-only-key"},
    ):
        client = LlmClientManager.initialize()
        assert LlmClientManager.initialize() is client
        assert LlmClientManager.get_client() is client
        assert client.model_name == app_config.llm.model
        assert str(client.openai_api_base).rstrip("/") == (
            app_config.llm.base_url.rstrip("/")
        )
    await LlmClientManager.close()

    runnable = Mock()
    runnable.ainvoke = AsyncMock(return_value=_CapabilityProbe(ok=True))
    fake_client = Mock()
    fake_client.with_structured_output.return_value = runnable
    with patch.object(LlmClientManager, "_client", fake_client):
        await LlmClientManager.check_structured_output_capability()
        fake_client.with_structured_output.assert_called_once_with(
            _CapabilityProbe,
            method=app_config.llm.structured_output_method,
        )
        runnable.ainvoke.assert_awaited_once()

    with patch.dict("os.environ", {}, clear=True):
        try:
            LlmClientManager.initialize()
        except RuntimeError as error:
            assert "DATA_AGENT_LLM_API_KEY" in str(error)
        else:
            raise AssertionError("缺少模型密钥时必须失败")


def test_llm_client_manager() -> None:
    """运行不访问模型端点的管理器检查。"""
    asyncio.run(_test_manager())


if __name__ == "__main__":
    test_llm_client_manager()
