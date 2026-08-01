# 回滚为 Trellis 自主管理工作树：技术设计

## 1. Design Decision

任务创建恢复为单一 Trellis-owned 生命周期。Codex 不再是特殊 worktree owner；所有平台在用户批准后，都由当前主会话按照 Phase 1.0 执行 Git 状态核验、命名冲突检查、`git worktree add`、developer 初始化、任务创建和元数据验证。

本设计是语义回滚，不是提交级 revert。它保留 07-30 之后已经证明有价值且不依赖 host ownership 的行为：显式 PR base、实际 branch/worktree 元数据、未知 task/meta 字段保留、detached HEAD 拒绝，以及父任务元数据完整性门禁。

## 2. Target Lifecycle

1. 主会话取得任务创建授权并确认 developer、PR base、start point、最终分支和目标 `.trellis/worktrees/<MM-DD-slug>` 路径。
2. 主会话确认分支、目录和 Git worktree registry 中不存在冲突。
3. Trellis 执行 `git worktree add -b <branch> <trellis-path> <start-point>`。
4. 后续工具调用固定在新 worktree 根目录，初始化该 worktree 的 `.trellis/.developer`。
5. 执行平台无关的 `task.py create <title> --slug <slug> --base-branch <pr-base>`。
6. `cmd_create` 读取实际分支和 worktree 根，写入 `worktree_owner=trellis` 与 `task_creation_policy=trellis_managed`。
7. 创建后验证 branch、root、task.json、current 和 context manifests，保持 `planning`，等待用户批准后才 `task.py start`。

## 3. Boundary and Contracts

### 3.1 Removed host-specific surface

- 删除 `.agents/skills/trellis-create-task/SKILL.md`。
- `.trellis/workflow.md` 不再含 Codex 专用 `create_thread` 路径、ready/queued thread handoff 或 nested Codex worktree bootstrap。
- `trellis-start` 与 `trellis-brainstorm` 统一引用 Phase 1.0，不再按 Codex/非 Codex 分流。
- `task.py create` 不再接受 `--platform codex` 作为 ownership selector。
- `common/task_store.py` 不再解析 `codex_host_managed`，不再调用 Codex linked-worktree verifier。
- 若全仓引用归零，删除 `.trellis/scripts/common/worktree.py` 及对应 host schema/registry 单元测试。

### 3.2 Preserved and generalized surface

- 保留 `--base-branch`，让 create 原子写入已核验 PR base，避免 create 后再修正元数据。
- 保留当前分支、worktree 根路径、未知 top-level/meta 字段的写入/保留逻辑。
- 新任务统一写 `meta.worktree_owner=trellis` 和 `meta.task_creation_policy=trellis_managed`。
- detached HEAD 继续在任何任务文件写入前失败。
- `--parent` 缺少父 `task.json` 时对所有平台 fail-before-write；成功路径继续写双向父子链接。
- 当前与历史 `task.json` 允许保留旧 Codex owner/policy 值；读取侧不新增迁移，也不因未知/旧值拒绝。

### 3.3 Fail-closed legacy behavior

删除 CLI 的 `--platform` 参数，使旧的 `task.py create ... --platform codex` 在 argparse 阶段失败。不得把旧命令静默解释为 Trellis-managed，因为调用方实际上可能仍处于 Codex host-created worktree，静默降级会制造错误 ownership。

## 4. Affected Files

| Path | Planned change |
| --- | --- |
| `.trellis/workflow.md` | 合并 Phase 1.0 平台分支，所有平台使用 Trellis worktree 流程；保留后续无关 Phase 2/3 修改。 |
| `.agents/skills/trellis-start/SKILL.md` | 删除 Codex 专属 task creation 路由。 |
| `.agents/skills/trellis-brainstorm/SKILL.md` | 无任务时统一回到 Trellis Phase 1.0。 |
| `.agents/skills/trellis-create-task/SKILL.md` | 删除 host adapter。 |
| `.agents/skills/trellis-meta/references/local-architecture/task-system.md` | 更新平台路由、task_store 和变更影响图。 |
| `.codex/hooks/inject-workflow-state.py` | 仅在仍有 host-routed 注释/术语时同步为统一 Phase 1.0；不改变 breadcrumb 解析行为。 |
| `.trellis/scripts/task.py` | 移除 `--platform` 入口和 Codex 用法，保留 `--base-branch`。 |
| `.trellis/scripts/common/task_store.py` | 删除 host policy 分支，固定 Trellis owner/policy，泛化父任务门禁，保留通用元数据逻辑。 |
| `.trellis/scripts/common/worktree.py` | 引用归零后删除 Codex 专属策略与 linked-worktree verifier。 |
| `.trellis/scripts/tests/test_worktree.py` | 用统一 Trellis 创建、元数据、父子门禁、工作流过滤和旧 CLI fail-closed 测试替换 host-managed 断言。 |
| `.trellis/spec/backend/trellis-task-worktree.md` | 重写为 Trellis-only 所有权契约及历史兼容说明。 |
| `.trellis/spec/backend/index.md` | 更新 guide 描述，不删除索引入口。 |

历史 `.trellis/tasks/07-30-adaptive-worktree-management/**`、归档任务和现有 Codex-owned task.json 不在修改范围内。

## 5. Compatibility Strategy

| Consumer/state | Behavior after rollback |
| --- | --- |
| 新建 Codex 任务 | 与其他平台相同，由 Trellis `git worktree add` 创建，owner 为 Trellis。 |
| 新建非 Codex 任务 | 保持现行 Trellis-managed 路径。 |
| 旧自动化传 `--platform codex` | 在解析阶段明确失败，不写任务文件；调用方必须切换到统一命令。 |
| 旧 Codex-owned task.json | 原样可读、可列出、可继续/验证/归档；不迁移 owner。 |
| 父子任务 | 父元数据必须已存在于 child start point；检查从 Codex 特例提升为统一规则。 |
| 当前 Codex worktrees | 不删除、不移动；只影响回滚合入后的新任务创建路由。 |

## 6. Alternatives Rejected

- **直接 revert `8ef5df1`**：会覆盖 07-30 之后同文件中的安全推送、父任务保护和其他改动，范围不可控。
- **只改 workflow/Skill，保留 host runtime**：会留下可执行的 `--platform codex` 后门，规范和测试继续表达两种 ownership，回滚不完整。
- **保留 `--platform` 但忽略其值**：旧调用会在 host-created worktree 中被静默标成 Trellis-owned，违反所有权真实性。
- **迁移历史 owner 元数据**：历史记录描述的是创建时事实，改写会破坏审计价值且没有运行时必要性。

## 7. Validation Design

- 工作流过滤测试：Codex 与至少一个非 Codex 平台的 Phase 1.0 都包含 `git worktree add`，都不包含 `create_thread`、`target.environment.type`、`trellis-create-task`。
- CLI 测试：help 无 `--platform` host 模式；传入旧参数在任何任务文件创建前失败。
- task store 测试：临时注册 Trellis worktree 内写入实际 branch/base/root 与 Trellis owner/policy；重复 create 保留未知字段。
- 父子测试：缺父元数据 fail-before-write；父元数据存在时双向链接。
- 兼容性测试：含旧 Codex meta 的 fixture 可被通用读取/验证路径接受，且不被重写。
- 搜索门禁：非历史任务工件中 `create_thread`、`codex_host_managed`、`trellis-create-task`、`verify_codex_host_worktree` 无活跃引用。
- 完整检查：专项 unittest、全部 `.trellis/scripts/tests`、Python compile/check、`git diff --check`。

## 8. Rollback of the Future Implementation

若实现验证失败，只回退本任务列出的实现文件，不删除任何 worktree，不改写历史 task.json，不使用 reset 或强制推送。恢复时应保证 workflow、CLI、spec 和 tests 同步回到同一 ownership contract，避免出现提示层与运行时分裂。
