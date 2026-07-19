# 实施计划

## 1. 建立统一检查能力

- [x] 新增 `tests/helpers/checks.py`。
- [x] 实现相等性检查、通用条件检查和显式失败函数。
- [x] 统一 PASS/FAIL 输出格式与 pytest 失败消息。
- [x] 保证公共函数具有类型注解和中文 Google Style Docstring。

## 2. 转换单元测试

- [x] 转换 `tests/unit/infrastructure/` 下全部检查点。
- [x] 转换 `tests/unit/ddl_metadata/` 下全部检查点。
- [x] 保留故障注入、异常传播和日志捕获语义。
- [x] 运行单元测试并使用 `-s` 抽查人工输出。

## 3. 转换集成测试

- [x] 转换 `tests/integration/infrastructure/` 下全部检查点。
- [x] 转换 `tests/integration/persistence/` 下全部检查点。
- [x] 转换 API、worker、memory service 与 DDL metadata flow 检查点。
- [x] 保留 marker、fixture、事务、关闭与清理逻辑。
- [x] 对异步和有副作用表达式确保只执行一次。

## 4. 验证自动失败能力

- [x] 执行辅助函数失败冒烟检查，确认不匹配时进程非零退出。
- [x] 搜索确认 `tests/` 下无裸 `assert` 语句。
- [x] 搜索并人工复核原 `raise AssertionError` 分支，区分测试失败与故障注入。

## 5. 质量门禁

- [x] `uv lock --check`
- [x] `uv run ruff check tests`
- [x] `uv run pyright tests`
- [x] `uv run python -m compileall -q tests`
- [x] `uv run pytest --collect-only`
- [x] `uv run pytest -m "not integration"`
- [x] `uv run pytest -s -m "not integration"`，确认实时输出可读。
- [x] 若 MySQL、Redis 等本地服务可用，运行 `uv run pytest -m "integration and not tei"`；不可用则如实记录。
- [x] `git diff --check`

## 6. 复核与回滚点

- [x] 对照原 228 处断言逐文件核对，没有遗漏检查语义。
- [x] 复核测试中没有敏感值输出。
- [x] 复核未修改 `src/`、基础设施配置和 pytest 默认捕获配置。
- [x] 若某类转换改变控制流，回滚该文件并按显式中间变量方式重新转换。

> 集成检查说明：本机 Docker Desktop Linux Engine 未运行，无法连接
> `npipe:////./pipe/dockerDesktopLinuxEngine`；但本机 MySQL `3306` 与 Redis
> `6379` 服务可达，已执行 `uv run pytest -m "integration and not tei"`，
> 结果为 9 passed、6 deselected。TEI `8080` 不可达，因此未执行 TEI
> 集成测试。
