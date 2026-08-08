"""Locking Service 启动能力门禁检查。"""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from pytest import MonkeyPatch

import application
from data_sync import worker as data_sync_worker
from ddl_metadata.worker import lifecycle
from infrastructure.mysql import LockingServiceUnavailableError

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


async def test_api_probe_failure_stops_business_composition(
    monkeypatch: MonkeyPatch,
) -> None:
    """API 缺少 Locking Service functions 时不得装配业务服务。"""
    failure = LockingServiceUnavailableError("locking service missing")
    elasticsearch_initialize = Mock(return_value=object())
    monkeypatch.setattr(application, "setup_logging", Mock())
    monkeypatch.setattr(
        application.RedisClient, "initialize", Mock(return_value=object())
    )
    monkeypatch.setattr(application.MySQLDatabase, "initialize", Mock())
    lock_manager = Mock()
    lock_manager.initialize = AsyncMock()
    lock_manager.check_capability = AsyncMock(side_effect=failure)
    lock_manager.close = AsyncMock()
    monkeypatch.setattr(
        application, "GenerationLockManager", Mock(return_value=lock_manager)
    )
    monkeypatch.setattr(
        application.ElasticsearchClient,
        "initialize",
        elasticsearch_initialize,
    )

    with pytest.raises(LockingServiceUnavailableError) as captured:
        async with application._lifespan(FastAPI()):
            pytest.fail("能力 probe 失败后不得进入 API 服务期")

    assert captured.value is failure
    lock_manager.close.assert_awaited_once_with()
    elasticsearch_initialize.assert_not_called()


async def test_ddl_worker_probe_failure_stops_index_composition(
    monkeypatch: MonkeyPatch,
) -> None:
    """DDL worker 缺少 Locking Service functions 时不得装配索引与图。"""
    failure = LockingServiceUnavailableError("locking service missing")
    elasticsearch_initialize = Mock(return_value=object())
    monkeypatch.setattr(lifecycle, "setup_logging", Mock())
    monkeypatch.setattr(lifecycle, "_wait_for_queue", AsyncMock())
    monkeypatch.setattr(
        lifecycle.RedisClient, "initialize", Mock(return_value=object())
    )
    monkeypatch.setattr(lifecycle.MySQLDatabase, "initialize", Mock())
    lock_manager = Mock()
    lock_manager.initialize = AsyncMock()
    lock_manager.check_capability = AsyncMock(side_effect=failure)
    lock_manager.close = AsyncMock()
    monkeypatch.setattr(
        lifecycle, "GenerationLockManager", Mock(return_value=lock_manager)
    )
    monkeypatch.setattr(
        lifecycle.ElasticsearchClient,
        "initialize",
        elasticsearch_initialize,
    )

    with pytest.raises(LockingServiceUnavailableError) as captured:
        await lifecycle.startup({"redis": object()})

    assert captured.value is failure
    lock_manager.close.assert_awaited_once_with()
    elasticsearch_initialize.assert_not_called()


async def test_data_sync_probe_failure_stops_runtime_composition(
    monkeypatch: MonkeyPatch,
) -> None:
    """Data Sync worker 缺少 Locking Service functions 时不得构造 source/runtime。"""
    failure = LockingServiceUnavailableError("locking service missing")
    source_client = Mock()
    monkeypatch.setattr(data_sync_worker, "setup_logging", Mock())
    monkeypatch.setattr(data_sync_worker.MySQLDatabase, "initialize", Mock())
    lock_manager = Mock()
    lock_manager.initialize = AsyncMock()
    lock_manager.check_capability = AsyncMock(side_effect=failure)
    lock_manager.close = AsyncMock()
    monkeypatch.setattr(
        data_sync_worker, "GenerationLockManager", Mock(return_value=lock_manager)
    )
    monkeypatch.setattr(data_sync_worker, "MySQLSourceClient", source_client)

    with pytest.raises(LockingServiceUnavailableError) as captured:
        await data_sync_worker.run_worker()

    assert captured.value is failure
    lock_manager.close.assert_awaited_once_with()
    source_client.assert_not_called()


def test_fresh_mysql_and_ci_install_locking_service_functions() -> None:
    """Docker fresh volume 与 CI 必须复用同一份 Locking Service bootstrap。"""
    bootstrap_path = (
        _REPOSITORY_ROOT / "docs" / "docker" / "mysql" / "locking_service.sql"
    )
    bootstrap = bootstrap_path.read_text(encoding="utf-8")
    for function_name in (
        "service_get_read_locks",
        "service_get_write_locks",
        "service_release_locks",
    ):
        assert f"DROP FUNCTION IF EXISTS {function_name};" in bootstrap
        assert f"CREATE FUNCTION {function_name} RETURNS INT" in bootstrap
    assert bootstrap.count("SONAME 'locking_service.so';") == 3

    workflow = (_REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "< ../docs/docker/mysql/locking_service.sql" in workflow
