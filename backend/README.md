# Data Agent Backend

Python 后端由本目录独立拥有。业务源码直接位于 `src/`，测试位于 `tests/`，默认配置位于 `conf/app_config.yaml`；仓库不再提供 `data_agent` Python 命名空间。

在本目录安装并验证：

```powershell
uv sync --locked
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m settings
uv run pytest -m "not integration"
uv build
```

API 与 CDC CLI 名称仍为 `data-agent-api` 和 `data-agent-cdc`。显式配置覆盖继续使用 `DATA_AGENT_CONFIG`。
