"""TEI embedding 客户端的 live integration test。"""

import asyncio
from math import isclose

from langchain_huggingface import HuggingFaceEndpointEmbeddings

from app.clients.tei_embedding_client_manager import TeiEmbeddingClientManager

EMBEDDING_DIMENSION = 512


async def _test_tei_embedding_client() -> None:
    client = TeiEmbeddingClientManager.initialize()
    try:
        documents = await client.aembed_documents(["苹果", "香蕉"])
        query = await client.aembed_query("水果")

        assert isinstance(client, HuggingFaceEndpointEmbeddings)
        assert client.client is None
        assert len(documents) == 2
        assert all(
            len(vector) == EMBEDDING_DIMENSION for vector in [*documents, query]
        )
        assert all(
            isclose(sum(value * value for value in vector), 1.0, abs_tol=1e-5)
            for vector in [*documents, query]
        )
    finally:
        await TeiEmbeddingClientManager.close()


def test_tei_embedding_client() -> None:
    asyncio.run(_test_tei_embedding_client())


if __name__ == "__main__":
    test_tei_embedding_client()
