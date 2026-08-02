"""OpenAI 兼容模型客户端配置与能力检查。"""

from unittest.mock import AsyncMock, Mock, patch

from infrastructure.llm_client import (
    LLMClient,
    _CapabilityProbe,
)
from settings import app_config
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)


async def _test_llm_client() -> None:
    """验证环境密钥、复用、结构化能力探针和关闭。"""
    await LLMClient.close()
    with patch.dict(
        "os.environ",
        {"DATA_AGENT_LLM_API_KEY": "test-only-key"},
    ):
        client = LLMClient.initialize()
        check_condition(
            "_test_llm_client 检查点 1",
            LLMClient.initialize() is client,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_llm_client 检查点 2",
            LLMClient.get_client() is client,
            expected="原断言条件成立",
        )
        check_equal(
            "_test_llm_client 检查点 3",
            client.model_name,
            app_config.llm.model,
        )
        check_equal(
            "_test_llm_client 检查点 4",
            str(client.openai_api_base).rstrip("/"),
            app_config.llm.base_url.rstrip("/"),
        )
    await LLMClient.close()

    runnable = Mock()
    runnable.ainvoke = AsyncMock(return_value=_CapabilityProbe(ok=True))
    fake_client = Mock()
    fake_client.with_structured_output.return_value = runnable
    with patch.object(LLMClient, "_client", fake_client):
        await LLMClient.check_structured_output_capability()
        fake_client.with_structured_output.assert_called_once_with(
            _CapabilityProbe,
            method=app_config.llm.structured_output_method,
        )
        runnable.ainvoke.assert_awaited_once()

    with patch.dict("os.environ", {}, clear=True):
        try:
            LLMClient.initialize()
        except RuntimeError as error:
            check_exception("_test_llm_client 捕获预期异常", error, RuntimeError)
            check_condition(
                "_test_llm_client 检查点 5",
                "DATA_AGENT_LLM_API_KEY" in str(error),
                expected="原断言条件成立",
            )
        else:
            fail_check(
                "_test_llm_client",
                actual="未抛出预期异常",
                expected="缺少模型密钥时必须失败",
            )


async def test_llm_client() -> None:
    """运行不访问真实模型端点的 LLM 客户端检查。"""
    await _test_llm_client()
