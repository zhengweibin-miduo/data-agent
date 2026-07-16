"""元数据同步命令入口。"""

import argparse
import asyncio
import sys
from pathlib import Path

from app.client.elasticsearch_client_manager import ElasticsearchClientManager
from app.client.mysql_client_manager import MysqlClientManager
from app.client.qdrant_client_manager import QdrantClientManager
from app.client.tei_embedding_client_manager import TeiEmbeddingClientManager
from app.conf.meta_config import MetaConfig
from app.core.logging import setup_logging
from app.repository.metadata_repository import MetadataRepository
from app.service.metadata_sync_service import MetadataSyncService


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="同步数据仓库元数据")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        required=True,
        help="元数据 YAML 配置文件路径",
    )
    return parser.parse_args()


async def sync_metadata(config_path: Path) -> None:
    """初始化客户端、执行同步并始终释放资源。"""
    config = MetaConfig.from_yaml(config_path)
    try:
        MysqlClientManager.initialize()
        qdrant = QdrantClientManager.initialize()
        elasticsearch = ElasticsearchClientManager.initialize()
        embeddings = TeiEmbeddingClientManager.initialize()

        async with MysqlClientManager.session() as session:
            repository = MetadataRepository(session, qdrant, elasticsearch)
            await MetadataSyncService(repository, embeddings).sync(config)
    finally:
        close_results = await asyncio.gather(
            MysqlClientManager.close(),
            QdrantClientManager.close(),
            ElasticsearchClientManager.close(),
            TeiEmbeddingClientManager.close(),
            return_exceptions=True,
        )
        close_errors = [
            result for result in close_results if isinstance(result, BaseException)
        ]
        if close_errors:
            if error := sys.exception():
                for close_error in close_errors:
                    error.add_note(f"关闭外部客户端失败: {close_error!r}")
            else:
                raise BaseExceptionGroup("关闭外部客户端失败", close_errors)


def main() -> None:
    """运行元数据同步命令。"""
    setup_logging()
    asyncio.run(sync_metadata(parse_args().config))


if __name__ == "__main__":
    main()
