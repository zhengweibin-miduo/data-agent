# 外部事实核验

## asyncmy 0.2.11 与 Python 3.14 Windows wheel

- 最终核验日期：2026-07-26。
- 官方来源：https://pypi.org/project/asyncmy/0.2.11/
- PyPI 元数据声明 `Requires: Python >=3.9`，分类器包含 Python 3.14。
- 发布文件包含 CPython 3.13 的 Windows `win_amd64` 与 `win32` wheel。
- 发布文件中未出现 `cp314` wheel，因此 `pyproject.toml:6` 所述“asyncmy 0.2.11 缺少 Windows Python 3.14 wheel”当前仍然成立。
- 注释中的 `ponytail` 在仓库内没有定义，无法帮助维护者理解 `<3.14` 上限由谁或何种依赖策略维护；这是清晰度维护建议，不是过期兼容缺陷。
