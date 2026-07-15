# 完善 MySQL 异步 Session 管理

## Goal

在保留现有全局异步引擎生命周期接口的基础上，为 MySQL 增加连接健康策略和按异步任务隔离的 Session 创建能力，使后续 SQLAlchemy ORM 与事务代码能够安全复用统一配置。

## Background

- `app/clients/mysql_client_manager.py:13-40` 当前只管理一个 `AsyncEngine`，创建引擎时只传入 `mysql.url`。
- `app_test/clients/test_mysql_client_manager.py:11-21` 当前只通过 `AsyncEngine.connect()` 验证 `SELECT 1`，未覆盖 `AsyncSession`、事务作用域或关闭后的 Session 工厂状态。
- 本项目使用 SQLAlchemy 2.0.51、`mysql+asyncmy` 和 MySQL 8.4。
- 本地运行时默认连接池参数为 `pool_size=5`、`max_overflow=10`、`pool_timeout=30`、`pool_recycle=-1`、`pool_pre_ping=False`。

## Requirements

- **R1 — 引擎健康策略**：MySQL 异步引擎必须启用 `pool_pre_ping`，并配置有限的 `pool_recycle` 时间，避免复用已被 MySQL 或网络关闭的连接。
- **R2 — Session 工厂**：管理器必须基于全局 `AsyncEngine` 初始化并复用一个 `async_sessionmaker[AsyncSession]`，且使用 `expire_on_commit=False`。
- **R3 — Session 隔离**：管理器必须提供 `session()` 异步上下文管理器；每次进入上下文都创建新的 `AsyncSession`，不得把单个 Session 保存为全局实例或跨异步任务共享。
- **R4 — 事务封装**：`session()` 必须封装完整事务生命周期——正常退出时自动提交，异常退出时自动回滚，并在两种路径上都关闭 Session。业务代码只写 `async with MysqlClientManager.session() as session:`，不再显式调用 `begin()`、`commit()` 或 `rollback()`。
- **R5 — 管理器生命周期**：重复初始化必须复用同一个引擎和 Session 工厂；关闭时必须清除 Session 工厂、释放引擎连接池并允许后续重新初始化。
- **R6 — 兼容性**：保留现有 `initialize()`、`get_client()` 和 `close()` 行为，避免破坏已有调用方；未初始化时获取引擎或 Session 能力必须给出明确错误。
- **R7 — 配置范围**：继续以 `mysql.url` 为唯一必填连接配置；连接池容量、溢出量、等待时间、连接超时、日志和隔离级别暂不新增配置，沿用 SQLAlchemy/asyncmy 默认值。
- **R8 — 验证**：测试必须覆盖引擎参数、Session 类型与独立性、Session 执行 `SELECT 1`、自动提交、异常回滚、重复初始化、关闭和重新初始化。

## Technical Notes

- `pool_recycle` 的初始值采用 SQLAlchemy MySQL 文档示例值 `3600` 秒，显著低于 MySQL 默认八小时空闲断连时间。
- Session 工厂是可复用的全局对象，具体 `AsyncSession` 及其事务仅存在于单次 `session()` 上下文内。

## Acceptance Criteria

- [x] **AC1（R1）**：初始化后的引擎可观察到 `pool_pre_ping=True`、`pool_recycle=3600`。
- [x] **AC2（R2、R3）**：管理器能创建 `AsyncSession`，连续两次获取得到不同实例，且 Session 使用同一全局引擎并设置 `expire_on_commit=False`。
- [x] **AC3（R4）**：业务代码使用 Session 时不需要显式事务 API；上下文正常退出后数据已提交，发生异常后数据已回滚，Session 均已关闭。
- [x] **AC4（R6）**：现有引擎接口及其未初始化错误行为保持兼容；未初始化时进入 `session()` 抛出带初始化指引的 `RuntimeError`。
- [x] **AC5（R5）**：关闭后旧引擎已执行 `dispose()`，内部引擎和 Session 工厂均清空，再次初始化可得到新实例。
- [x] **AC6（R8）**：真实 MySQL 集成检查能分别通过引擎连接和 Session 执行 `SELECT 1`，并验证自动提交与异常回滚。
- [x] **AC7（R7）**：`MysqlConfig` 与 `conf/app.yaml` 不新增缺乏容量依据的可调参数。

## Out of Scope

- ORM 实体、Repository/DAO、数据库迁移和表结构。
- 自动重试业务 SQL 或失败事务。
- 读写分离、多租户、多个 MySQL 引擎和 `async_scoped_session`。
- 根据尚未确定的并发量调优 `pool_size`、`max_overflow` 或 `pool_timeout`。

## Notes

- 这是单文件管理器加对应集成测试的轻量任务，PRD-only 足够，不创建独立 `design.md` / `implement.md`。
- 用户已确认事务行为也必须封装，业务代码不负责显式事务控制。
