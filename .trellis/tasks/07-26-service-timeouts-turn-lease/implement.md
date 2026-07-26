# 执行计划

## 组一：配置项

- [x] 1.1 `settings.py`：新增七个配置字段（见 design.md 第三节），带中文描述与约束。
- [x] 1.2 `settings.py`：新增 `socket_timeout_seconds > sse_heartbeat_seconds` 跨字段校验。
- [x] 1.3 `conf/app_config.yaml`：补齐七个键的显式取值。

验证：`uv run pytest tests/unit/test_settings.py -q`

## 组二：客户端超时注入

- [x] 2.1 `infrastructure/redis.py`：注入 socket 超时与健康检查间隔。
- [x] 2.2 `infrastructure/elasticsearch.py`：注入 request_timeout、max_retries、
      retry_on_timeout。
- [x] 2.3 `infrastructure/qdrant.py`：注入 timeout。
- [x] 2.4 `infrastructure/tei_embeddings.py`：注入 AsyncInferenceClient timeout。
- [x] 2.5 测试：新增 `tests/unit/infrastructure/test_client_timeouts.py`，
      从客户端实例读回真实生效值。

验证：`uv run pytest tests/unit/infrastructure -q`

## 组三：会话轮次租约

- [x] 3.1 `conversation/repository.py`：`start_turn` 的活动轮次门禁改为 SQL 端
      可抢占性判定。
- [x] 3.2 测试：扩展会话单测覆盖未超租约 409 与超租约放行，并断言渲染 SQL 使用
      数据库端时间函数。

验证：`uv run pytest tests/unit -q`

## 收尾

- [x] README 基础质量门禁全部通过（82 项非集成测试）。集成测试**未执行**：本机
      Docker daemon 未运行，MySQL/Redis/ES/Qdrant/TEI 均不可用。客户端超时改动已用
      单测从客户端实例读回真实生效值验证；轮次租约改动已用 MySQL 方言渲染 SQL 核对。
- [x] 更新 spec（外部服务超时约定与轮次租约）、提交、记录 journal。

## 回滚点

三组互不依赖；组二依赖组一的配置字段，回滚组一需同时回滚组二。
