# 修复配置文件路径解析与加载入口

## Goal

让应用在源码树之外（wheel 安装、独立部署目录）也能找到配置文件，并提供受支持的
配置加载入口，使部署与测试不再依赖"当前进程恰好从仓库根启动"这一隐式前提。

## Background

架构审查问题 P0-8 的一部分。`src/data_agent/settings.py` 的加载存在两个缺陷：

- 默认路径写成 `Path(__file__).parents[2] / "conf" / "app_config.yaml"`。src-layout
  下 `parents[2]` 是仓库根，但一旦以 wheel 安装到 site-packages，该路径指向
  Python 环境目录，配置必然找不到；而 `conf/` 并不在包内（uv_build 只打包
  `src/data_agent`），所以 `pyproject.toml` 声明的入口 `data-agent-api`
  在安装后无法启动，只能以源码树运行。
- 没有任何环境变量覆盖入口，部署时无法指定配置位置，测试也只能把 `app_config`
  dump 成 dict 再改字段重建（见 `tests/integration/test_api.py`）。

失败时的报错还会是一个裸的 `FileNotFoundError`，不告诉运维查找过哪些位置。

## Scope

本任务只修复"配置从哪里来、怎么加载"。审查中记录的另一半问题——导入即读 YAML、
配置值被烘焙进 import-time 常量（表 schema、Pydantic 字段上限、arq 类属性、
logging 默认参数）——需要拆解这些 import-time 求值点，属于独立重构任务，
本任务不做，但为其提供 `get_settings()` 这一落脚点。

## Requirements

- 配置路径按明确优先级解析：显式传入的路径 → `DATA_AGENT_CONFIG` 环境变量 →
  当前工作目录下的 `conf/app_config.yaml` → 源码树相对位置（开发便利回退）。
- 所有候选位置都不存在时，报错必须列出实际查找过的绝对路径，并说明可用
  `DATA_AGENT_CONFIG` 指定；不得抛裸 `FileNotFoundError`。
- `DATA_AGENT_CONFIG` 指向的路径不存在时必须直接失败，不得静默回退到其它候选——
  显式指定被忽略会造成"以为改了配置其实没生效"。
- 提供受支持的加载入口 `get_settings()`，同一进程内缓存单次解析结果；
  提供 `reset_settings()` 供测试重新加载。
- 现有 `app_config` 名称与类型保持不变，所有既有调用点与静态类型检查不受影响。
- README 记录环境变量与查找顺序。

## Constraints

- 不引入新的外部依赖。
- 不改变 `AppSettings` 的字段与校验语义。
- 不把 `app_config` 改成动态属性（模块级 `__getattr__` 会让其类型退化为 `Any`，
  使全仓库配置访问失去静态检查）。

## Acceptance Criteria

- [x] `DATA_AGENT_CONFIG` 指定的配置能被加载；指向不存在的路径时直接失败并
      在报错中给出该路径。
- [x] 未设环境变量时按"工作目录 → 源码树"顺序解析，且工作目录优先。
- [x] 全部候选缺失时的报错列出所有查找过的绝对路径并提示环境变量。
- [x] `get_settings()` 在同一进程内只解析一次；`reset_settings()` 后可重新解析。
- [x] `app_config` 的类型仍是 `AppSettings`，`uv run pyright src tests` 无新增错误。
- [x] README 记录环境变量与查找顺序。
- [x] README 记录的基础质量门禁全部通过。
