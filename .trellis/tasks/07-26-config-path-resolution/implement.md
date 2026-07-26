# 执行计划

- [x] 1 `settings.py`：新增 `CONFIG_PATH_ENV`、`resolve_config_path()`，
      按四级优先级解析并在全部缺失时抛 `config_not_found`（含候选绝对路径）。
- [x] 2 `settings.py`：`from_yaml` 的默认实参改为 `None` 并委托 `resolve_config_path`。
- [x] 3 `settings.py`：新增 `get_settings()` 进程内缓存与 `reset_settings()`；
      `app_config` 改为 `get_settings()` 的结果，保留 `AppSettings` 类型标注。
- [x] 4 测试：新增 `tests/unit/test_settings_loading.py` 覆盖环境变量命中、
      显式指定缺失不回退、工作目录优先、全部缺失的报错内容、缓存与重置。
- [x] 5 README：补充 `DATA_AGENT_CONFIG` 与查找顺序说明。
- [x] 6 spec：在配置约定处记录解析顺序与"显式指定不回退"的理由。
- [x] 7 全量门禁 + 提交 + journal。

验证：`uv run pytest tests/unit -q`，随后 README 基础门禁全量。

## 回滚点

单文件为主，`git revert` 即可；`app_config` 语义与类型保持不变，回滚不影响调用点。
