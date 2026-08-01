# 回滚为 Trellis 自主管理工作树

## Goal

恢复单一、平台无关的 Trellis 任务工作树生命周期：用户批准创建任务后，由 Trellis 在主项目下创建并管理 `.trellis/worktrees/<MM-DD-task-slug>`，包括 Codex 在内的 Agent 平台不再委托宿主 `create_thread` 创建任务 worktree。

## Background and Confirmed Facts

- 用户原始需求为“回滚为Terrils自己管理工作树”，其中 `Terrils` 按上下文解释为 `Trellis`。
- 当前 host-managed 适配由提交 `8ef5df1ff1e0c78e5bc3913427f60449bc44084e` 引入，核心决策是 Codex 使用 `create_thread`，其他平台继续使用 Trellis `git worktree add`。
- 后续提交 `1735840` 增加了 Codex 子任务必须在 starting state 中包含父任务元数据的保护；`.trellis/workflow.md` 此后还包含与安全推送等无关的后续修改，因此不得直接整体 revert 相关提交。
- 当前 Codex 路由位于 `.trellis/workflow.md`、`trellis-start`、`trellis-brainstorm` 和 `trellis-create-task`；运行时所有权分支位于 `task.py`、`common/task_store.py`、`common/worktree.py`；规范和回归测试分别位于 `.trellis/spec/backend/trellis-task-worktree.md` 与 `.trellis/scripts/tests/test_worktree.py`。
- 当前任务本身由 Codex worktree 引导创建，`task.json` 如实记录 `worktree_owner=codex`；该事实不因本任务规划而改写。

## Requirements

- **R1 — 统一创建路由**：包括 Codex 在内的所有受支持平台都必须使用 Trellis Phase 1.0 的 `git worktree add` 流程创建任务分支和 `.trellis/worktrees/<MM-DD-task-slug>`。
- **R2 — 移除宿主委托**：任务创建流程不得要求或调用 Codex `create_thread`，不得要求加载 `trellis-create-task`，不得把 Codex 主会话与其他平台拆成两种 worktree owner。
- **R3 — 平台无关 CLI**：`task.py create` 不得再通过 `--platform codex` 选择 host-managed 策略；文档化的创建命令必须是平台无关命令，并显式传入已核验的 PR base。
- **R4 — 保留通用元数据改进**：新任务必须继续记录实际当前分支、显式 `base_branch`、规范化 worktree 根路径，以及 `meta.worktree_owner=trellis`、`meta.task_creation_policy=trellis_managed`。
- **R5 — 保留父子任务完整性**：当 `--parent` 指向的父任务元数据在当前 starting point 中不存在时，创建必须在写入子任务文件前失败；该保护应成为平台无关规则。
- **R6 — 历史兼容**：已有任务中 `worktree_owner=codex` 或 `task_creation_policy=codex_host_managed` 的记录必须保持可读，不迁移、不重写；现存 Codex worktree 不删除、不移动。
- **R7 — 同步所有契约面**：工作流、Skills、Trellis 本地架构说明、CLI/脚本、规范索引/正文和测试必须同步，不能只改提示词或只改 Python。
- **R8 — 语义回滚**：只回滚 host-managed 工作树适配，不撤销 07-30 之后与远端安全、任务状态、会话指针或其他 Trellis 行为有关的修改。

## Constraints

- 本阶段仅创建并规划 Trellis 任务；不得运行 `task.py start`，不得修改实现代码，提交、推送或创建 PR。
- 不得运行 `git worktree add`、创建嵌套 worktree、删除或清理任何 worktree。
- 主项目 `D:\projiect\data-agent` 的无关未跟踪文件 `pr71_threads.json` 不得复制、移动、暂存、修改或删除。
- 最终实现分支为 `fix/restore-trellis-worktree-management-20260801`，PR base 为 `master`。

## Out of Scope

- 改变 Codex 产品本身的 worktree/thread 能力。
- 删除 Codex 已创建的任务、线程、分支或 worktree。
- 批量迁移或修正历史/归档任务的 owner 元数据。
- 改动 Phase 2/3 的实现、检查、提交、推送或收尾流程，除非只是移除对 host-managed 创建路径的失效引用。
- 回滚 07-30 任务目录及其研究工件；它们继续作为历史决策证据保留。

## Acceptance Criteria

- [ ] **AC1**：过滤后的 Codex Phase 1.0 指引包含 Trellis `git worktree add` 流程，不包含 `create_thread`、`target.environment.type=worktree` 或 `trellis-create-task`。
- [ ] **AC2**：非 Codex 平台仍使用同一 Trellis-managed 流程，且没有行为回退。
- [ ] **AC3**：`task.py create --help` 不再把 `--platform codex` 暴露为任务创建模式；旧的 host-managed 调用不能在写入任务文件后悄悄降级为 Trellis ownership。
- [ ] **AC4**：在临时注册的 Trellis worktree 中创建任务后，`task.json` 的 `branch`、`base_branch`、`worktree_path` 与 Git 事实一致，owner/policy 分别为 `trellis`/`trellis_managed`。
- [ ] **AC5**：缺失父 `task.json` 的子任务创建在任何平台上都于写文件前失败；存在父元数据时双向链接正常。
- [ ] **AC6**：仓库中不再存在可执行的 Codex host-managed 任务创建入口；删除专属 Skill/校验模块后不存在悬空 import、链接或测试。
- [ ] **AC7**：现有含 Codex owner/policy 的 task.json 无需迁移即可被当前任务列表、current/validate 和归档读取路径处理。
- [ ] **AC8**：针对工作树创建契约的单元测试、Trellis 脚本完整测试、相关静态检查与 `git diff --check` 全部通过。
- [ ] **AC9**：实现 diff 不包含历史任务工件重写、现有 worktree 删除、无关 Trellis 工作流修改或 `pr71_threads.json`。

## Open Questions

无。用户意图和回滚边界已可由原始需求、07-30 任务工件、相关提交及当前代码确定；实现前只需由用户评审并批准本规划。
