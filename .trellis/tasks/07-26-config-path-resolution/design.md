# 技术设计

## 解析顺序

```
1. from_yaml(path=...) 的显式实参        （测试与工具的最高优先级入口）
2. 环境变量 DATA_AGENT_CONFIG            （部署入口；指定即必须存在，不回退）
3. Path.cwd() / "conf" / "app_config.yaml"  （安装后从部署目录运行）
4. Path(__file__).parents[2] / "conf" / "app_config.yaml"  （源码树开发回退）
```

第 2 项不回退是刻意的：显式指定被静默忽略会让运维以为改了配置却仍加载旧文件，
这类故障比"直接启动失败"难排查得多。第 3 项排在第 4 项之前，使部署目录里的配置
优先于恰好可见的源码树配置。

失败时抛 `DataAgentError`（code `config_not_found`，stage `settings`），
`details` 只放候选路径这类可公开信息，符合仓库既有的错误契约：message 进日志、
`details` 是有意公开的有界字段。报错列出全部候选绝对路径并提示环境变量名。

## 加载入口

```python
def resolve_config_path(path: str | Path | None = None) -> Path
def get_settings(path: str | Path | None = None) -> AppSettings   # 进程内缓存
def reset_settings() -> None                                      # 清缓存，供测试
app_config: AppSettings = get_settings()
```

缓存用模块级 `_settings: AppSettings | None` 而不是 `functools.lru_cache`：
`reset_settings()` 语义更明确，且避免把 `path` 实参并入缓存键造成"不同实参各缓存
一份"的意外行为——`get_settings(path)` 在已有缓存时应直接返回缓存，显式重载走
`reset_settings()` + `get_settings(path)` 两步，语义不含糊。

`app_config` 仍是 import 时求值的模块级常量，类型标注保持 `AppSettings`。这保证
所有既有调用点与 pyright 检查不受影响；把它改成模块级 `__getattr__` 会让类型退化
为 `Any`，代价远大于收益。

因此本任务**不**解决"导入即读 YAML"：`app_config` 依旧在 import 时加载一次。
真正的惰性化需要先拆掉 import-time 求值点（`memory/mysql/tables.py` 与
`conversation/mysql_tables.py` 的 `Table(schema=...)`、`conversation/models.py`
的 `Field(max_length=...)`、`ddl_metadata/worker/settings.py` 的 arq 类属性、
`logging.py` 的默认参数），属于独立任务。`get_settings()` 是那次重构的落脚点。

## 测试策略

- 环境变量指向真实文件 → 加载成功且取值来自该文件。
- 环境变量指向不存在的路径 → 抛 `config_not_found`，报错含该路径，且**不**回退。
- 无环境变量时工作目录优先于源码树（用 `monkeypatch.chdir` 到临时目录并在其中
  放 `conf/app_config.yaml`）。
- 全部候选缺失 → 报错列出全部候选绝对路径并含环境变量名。
- `get_settings()` 两次调用返回同一对象；`reset_settings()` 后返回新对象。

测试必须在结束时恢复模块缓存，避免污染同进程内其它用例读到的 `app_config`。
