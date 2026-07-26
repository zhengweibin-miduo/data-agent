# 注释、Docstring 与审查工具规范

## 规范来源（可核验）

- `AGENTS.md:25-27`：所有 AI 审查必须读取根目录 `code_review.md`，该文件是审查意见及修复回复唯一规范来源。
- `code_review.md:5-13`：审查使用简体中文；标识符、路径、命令、日志和错误原文保留英文；仅报告已核验、可复现或由控制流/数据流证明的问题；发布前核查实现、调用方、测试、注释和设计约束；每条问题一个根因；证据含准确 `文件路径:行号`、触发条件与可观察后果；验证结果必须如实。
- `code_review.md:15-20`：仅确认的 P0/P1 可阻塞合并。P0 是广泛不可用、严重安全、不可逆数据损坏或发布完全受阻且无规避；P1 是现实触发下重要功能错误、可靠性/安全显著下降、数据不一致或严重性能退化。需综合影响范围、触发可能性、可规避性、恢复成本；P2、样式/nit 不发布阻塞意见。
- `code_review.md:22-40`：inline 模板要求独立段落：`**[P0/P1] 标题**`、`风险`、`证据`、`修复建议`；无阻塞问题时固定回复“未发现需要阻止合并的 P0/P1 问题。”
- `code_review.md:42-89`：修复回复必须在原 thread，且只选“已修复/部分修复/不采纳”；需写修改说明、验证命令及结果、剩余风险、提交信息等，禁止只写“已处理”。
- `.trellis/spec/backend/quality-guidelines.md:12-17`：运行时和测试包要求显式类型注解与中文 Google Style Docstrings；Ruff 强制公共包、模块、类、函数、方法、fixture、测试的 Docstrings；AI 审查使用简体中文，技术事实保留英文。
- `.trellis/spec/backend/quality-guidelines.md:63-75`：Docstring 遵循 PEP 257 + Google sections，中文 prose；`Args/Returns/Yields/Raises` 保留英文；不重复注解中的类型；简单公共对象可单行；复杂对象记录非显然参数、结果、异常、副作用、事务、并发、生命周期；inline comment 只解释 rationale/invariants，不复述可见行为；中文可豁免英语祈使/句末标点规则，但缺失公共对象检查必须保留。
- 历史设计约束 `.trellis/tasks/archive/2026-07/07-18-python-structure-naming-docstrings/design.md:196-213` 补充：`TODO` 应包含 owner/issue 与具体移除或完成条件；删除注释掉的代码和历史变更叙述；Ruff `D` 规则使用 Google convention，仅忽略 `D400,D401,D415`。
- `.trellis/spec/backend/quality-guidelines.md:140-158`：审查核对每个公共运行时/测试对象有有意义 Docstring（不能用“X class.”等重述糊弄）；缺失/占位 Docstring 属于 forbidden pattern；未执行或不可用服务检查不得声称通过。

## 工具配置及可发现范围

- `pyproject.toml:41-50`：Ruff 目标 Python 3.13，源码范围 `src/tests`；启用 `E,F,I,D`，仅忽略 `D400,D401,D415`，Docstring convention 为 Google。可自动发现格式/导入/F 类静态错误，以及 pydocstyle 缺失/结构违规；不能判断文档是否准确描述业务、注释是否解释了真正 rationale/invariant、中文内容是否有意义，也不能证明触发路径或风险等级。
- `pyproject.toml:62-65`：Pyright `src/tests`，Python 3.13，`basic` 模式。可发现部分类型不一致；不能发现运行时数据流、并发/生命周期语义、注释与实现不一致。
- `pyproject.toml:52-60`：pytest 收集 `tests/`，importlib，async auto，集成标记 `integration/tei`。测试可验证行为，但覆盖取决于具体用例；工具不会自动证明所有注释承诺、TODO 完成条件或安全/隐私边界。
- `.trellis/spec/backend/quality-guidelines.md:76-109` 列出基线验证：`uv sync --locked`、`uv lock --check`、`uv run ruff check src tests`、`uv run pyright src tests`、compileall、settings module、pytest、docker compose config、`git diff --check`。审查报告必须区分实际执行、未执行及原因。
- `fastctx grep` 全仓 TODO/FIXME 等命中目前仅在归档设计和 Trellis skill 文档，未发现生产源码 TODO/FIXME。该检索只能发现字面标记，不能识别无标签的临时注释、过时说明或语义上的占位文档。

## 正式审查判定标准

1. 先按 `code_review.md` 对每条候选问题建立准确文件行号、真实触发条件、控制/数据流证据和后果；核查调用方、测试、注释与设计约束，排除误报。
2. 注释/Docstring 问题只有在造成可证明的公共 API 误用、错误异常/副作用/并发或生命周期理解，进而达到 P0/P1 影响时才阻塞；单纯措辞、中文/英文偏好、标点、格式或缺少低风险说明属于非阻塞反馈。
3. Ruff D 规则违规本身是自动化信号，不自动等同 P1；需证明其导致现实功能、可靠性、安全、数据一致性或严重性能后果，或团队明确把该检查作为合并门禁（仍应按规范的 P0/P1 门槛报告）。
4. 若无可证明 P0/P1，最终使用固定无阻塞句，不发布 P2/nit。验证命令及结果必须据实填写。

## 需主代理/用户决定的边界

- 是否把本轮纯 Docstring/注释缺失作为阻塞条件：规范明确要求质量门禁，但审查发布仅允许确认 P0/P1；需主代理按实际影响和项目门禁决定是否升级。
- 对历史 TODO（若新增）是否具备 owner/issue、完成条件，需结合任务上下文判断，不能由 grep 自动决定。
- 中文技术术语、协议名、标识符是否应保留英文，按 `quality-guidelines.md` 与具体领域约束裁决；Ruff 无法判断语义语言质量。
- 工具未执行、依赖服务不可用或测试覆盖不足时，只能报告限制和剩余风险，不得推断通过。
