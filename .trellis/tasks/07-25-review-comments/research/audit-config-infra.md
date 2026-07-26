# 配置与基础设施分区审查

## 审查文件清单

- `src/data_agent/settings.py`
- `src/data_agent/infrastructure/__init__.py`, `checkpoint_store.py`, `elasticsearch.py`, `llm_client.py`, `mysql.py`, `qdrant.py`, `redis.py`, `tei_embeddings.py`
- `src/data_agent/application.py`, `src/data_agent/logging.py`, `src/data_agent/__init__.py`
- `tests/unit/test_settings.py`, `tests/unit/infrastructure/test_logging.py`, `tests/unit/infrastructure/test_logging_lifecycle.py`
- `conf/app_config.yaml`、`pyproject.toml`、`.github/workflows/ci.yml`、`docs/docker/docker-compose.yml`、`docs/docker/elasticsearch/Dockerfile`

排除：SQL `COMMENT`（按任务要求不审查 SQL 业务模块）、conversation、ddl_metadata 及其测试；Trellis 历史/工具目录不属于产品范围。

## P0/P1 候选

无。逐文件核对 Docstring 与签名、实现、调用方及测试，未发现会阻塞发布或导致核心功能必然错误的注释/Docstring 语义矛盾。

## 非阻塞维护候选

1. `pyproject.toml:6` 原文：`# ponytail: asyncmy 0.2.11 lacks a Windows Python 3.14 wheel; lift when available.`
   - 证据：同文件 `requires-python = ">=3.13,<3.14"`（7 行），依赖约束 `asyncmy>=0.2.11`（10 行）；项目仅在 CI 中声明 Python 版本文件（`.github/workflows/ci.yml:55-58`）。
   - 影响：该备注依赖外部 wheel 发布状态，且“ponytail”未在仓库定义；若 wheel 已发布，版本上限与备注会过期；离线无法确认当前 PyPI wheel 状态，不能升级为缺陷。
   - 最小建议：在确认 asyncmy 对 Windows/Python 3.14 的 wheel 后同步更新注释与 `requires-python`；否则将备注改为可核验的 issue/链接并说明复核条件。

## 确认无问题项

- `settings.py` 字段说明与 validators 一致：`cors_origins` 仅允许 localhost/loopback（133-150 行）；跨配置约束校验来源租约、数据库边界及向量维度（306-322 行）；对应越界测试在 `tests/unit/test_settings.py:145-173`。
- 基础设施生命周期 Docstring 与实现一致：MySQL session 明确提交/回滚/关闭（`infrastructure/mysql.py:50-65`）；Redis、Qdrant、Elasticsearch、TEI、checkpoint 的初始化/获取/关闭语义均与代码一致。
- LLM 结构化输出能力探针“不做文本降级”与实现（`llm_client.py:48-58`）一致；日志 JSON 单行、异常堆栈截断及字段白名单均有对应测试（`tests/unit/infrastructure/test_logging.py:62-267`）。
- `application.py` lifespan 的初始化顺序和 finally 关闭顺序与调用实现一致（27-65 行）；错误处理器仅映射声明的异常类型（67-106 行）。
- `conf/app_config.yaml`、Docker Compose、Elasticsearch Dockerfile 无人工注释或 TODO；CI 中 setup-uv SHA 注释（`.github/workflows/ci.yml:61`）格式正确，但 SHA 与 v8.1.0 的对应关系需外部核验，未据此提出缺陷。

## 无法核验的外部事实

- `pyproject.toml:6` 所述 asyncmy 0.2.11 Windows Python 3.14 wheel 可用性。
- `.github/workflows/ci.yml:61` 的 commit SHA 是否确实对应 `astral-sh/setup-uv` v8.1.0。

## 验证透明度

本分区完成源码、测试、配置和 Docker 文件逐文件语义阅读；未修改产品文件。未在本代理运行 Ruff/pytest，统一验证由主代理执行。
