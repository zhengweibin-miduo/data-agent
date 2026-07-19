"""TEI embedding 客户端的 live integration test。"""

from math import isclose

import pytest
from langchain_huggingface import HuggingFaceEndpointEmbeddings

from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from tests.helpers.checks import check_condition, check_equal

EMBEDDING_DIMENSION = 1024


async def _test_tei_embedding_client() -> None:
    client = TEIEmbeddingClient.initialize()
    try:
        documents = await client.aembed_documents(["苹果", "香蕉"])
        query = await client.aembed_query("水果")

        check_condition(
            "_test_tei_embedding_client 检查点 1",
            isinstance(client, HuggingFaceEndpointEmbeddings),
            expected="原断言条件成立",
        )
        check_condition(
            "_test_tei_embedding_client 检查点 2",
            client.client is None,
            expected="原断言条件成立",
        )
        check_equal("_test_tei_embedding_client 检查点 3", len(documents), 2)
        check_condition(
            "_test_tei_embedding_client 检查点 4",
            all(len(vector) == EMBEDDING_DIMENSION for vector in [*documents, query]),
            expected="原断言条件成立",
        )
        check_condition(
            "_test_tei_embedding_client 检查点 5",
            all(
                isclose(sum(value * value for value in vector), 1.0, abs_tol=1e-5)
                for vector in [*documents, query]
            ),
            expected="原断言条件成立",
        )
    finally:
        await TEIEmbeddingClient.close()


@pytest.mark.integration
@pytest.mark.tei
async def test_tei_embedding_client() -> None:
    """验证真实 TEI 的异步向量维度与归一化。"""
    await _test_tei_embedding_client()
