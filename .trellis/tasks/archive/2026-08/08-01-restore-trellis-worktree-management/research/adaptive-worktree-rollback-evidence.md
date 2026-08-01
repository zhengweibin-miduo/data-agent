# Codex host-managed worktree 适配回滚证据

## 结论

采用语义回滚，不直接执行 `git revert 8ef5df1`。目标是移除 Codex host-managed 任务创建路由与运行时分支，同时保留后来形成的通用元数据写入、父子任务完整性保护和其他无关工作流修改。

## 历史证据

- `8ef5df1ff1e0c78e5bc3913427f60449bc44084e`（`feat: 让 Codex 托管 Trellis 任务工作树`）修改 21 个文件，新增 Codex 创建 Skill、worktree 校验模块、专项测试和规范，并把 Phase 1.0 拆为 Codex host-managed 与其他平台 Trellis-managed 两条路径。
- `.trellis/tasks/07-30-adaptive-worktree-management/design.md:29-62` 规定主会话调用 `create_thread`，子会话只在宿主已创建的 linked worktree 中执行 `task.py create --platform codex`。
- `.trellis/tasks/07-30-adaptive-worktree-management/design.md:109-130` 明确 owner/policy 元数据区分 Codex 和 Trellis。
- `1735840`（`fix: 阻止 Codex 创建孤立子任务`）在首次适配之后补充父任务元数据保护；这项保护应泛化保留，而不是随 host-managed 路径一起丢失。
- `08f4d7b` 等后续提交也修改 `.trellis/workflow.md`，证明整体 revert 会误伤无关行为。

## 当前路由证据

- `.trellis/workflow.md:316-338`：Codex 主会话加载 `trellis-create-task` 并调用 `create_thread`；Codex 子代理不得自行创建用户任务或嵌套 worktree。
- `.trellis/workflow.md:340-465`：其他平台执行完整 Trellis `git worktree add`、初始化 developer、创建任务及验证流程。
- `.agents/skills/trellis-create-task/SKILL.md`：专属 Codex host adapter，负责项目解析、host schema、ready/queued thread handoff 和 child bootstrap prompt。
- `.agents/skills/trellis-start/SKILL.md:46-60` 与 `.agents/skills/trellis-brainstorm/SKILL.md:30-35`：把 Codex 路由到专属 Skill，把其他平台路由到 Phase 1.0。
- `.agents/skills/trellis-meta/references/local-architecture/task-system.md`：记录同一平台分流架构和影响面。

## 当前运行时证据

- `.trellis/scripts/common/task_store.py:209-240`：根据显式 platform 选择 creation policy；Codex 路径验证 host worktree 并写 owner `codex`，其他路径写 `trellis`。
- `.trellis/scripts/common/task_store.py:242-255`：两条路径共享 detached HEAD 拒绝逻辑，应保留。
- `.trellis/scripts/common/task_store.py:282-295`：缺失父任务元数据的 fail-before-write 当前只限定 Codex，应泛化为所有任务创建。
- `.trellis/scripts/common/task_store.py:367-400`：保留未知字段后写入实际 branch/base/worktree 与 owner/policy；这部分是通用、可保留的改进。
- `.trellis/scripts/common/worktree.py:15-72`：定义 `codex_host_managed`/`trellis_managed` 策略与显式平台解析。
- `.trellis/scripts/common/worktree.py:115-174`：只为 Codex host-managed 路径检查 primary/linked/registry；统一 Trellis 创建后不再需要该专属模块。
- `.trellis/scripts/task.py` 的 create parser 暴露 `--platform` 和 `--base-branch`；回滚后保留显式 base 能力，移除 host platform selector。

## 测试与规范证据

- `.trellis/scripts/tests/test_worktree.py` 当前覆盖 14 个 host-managed 策略、Git registry、元数据、路由与 Skill schema 场景；这些断言必须重写为统一 Trellis ownership 契约，不能原样删除后不补覆盖。
- `.trellis/spec/backend/trellis-task-worktree.md:52-71` 把 `--platform codex`、host worktree 校验与 owner/policy 写成正式契约；规范应改为平台无关的 Trellis ownership，同时保留历史记录兼容说明。
- `.trellis/spec/backend/index.md:58-60` 要求任务创建与 ownership 变更读取上述规范，索引仍可保留，但描述需改成单一 Trellis ownership。
- `.trellis/.template-hashes.json` 不包含 `trellis-create-task`，删除该项目本地 Skill 时不能依赖模板 hash 同步，必须用搜索和测试验证引用已清零。

## 最小回滚边界

1. 将 Phase 1.0 合并为所有平台通用的 Trellis-managed 创建流程，复用当前非 Codex 分支的安全检查与验证。
2. 删除 `trellis-create-task` 及 `create_thread`/host schema/handoff 相关引用。
3. 移除 `--platform codex` 的创建策略分支和专属 `common/worktree.py`；保留 `--base-branch`、实际 Git 元数据写入及 unknown-field preservation。
4. 新任务统一写 `worktree_owner=trellis`、`task_creation_policy=trellis_managed`；历史 Codex 值只读兼容，不迁移。
5. 将父任务元数据存在性检查泛化到所有 `--parent` 创建，并继续保证失败发生在子任务目录写入前。
6. 重写专项测试和规范；保留历史 07-30 任务工件作为审计证据。
