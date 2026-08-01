---
goal: 恢复 Trellis 自主管理所有任务工作树
version: 1.0
date_created: 2026-08-01
last_updated: 2026-08-01
owner: zwb
status: 'Planned'
tags: [trellis, worktree, rollback, workflow]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

本计划以语义回滚方式移除 Codex host-managed 任务 worktree 适配，使所有 Agent 平台恢复统一的 Trellis-managed 创建流程，同时保留通用元数据、父子任务完整性和历史记录兼容性。本文件只定义后续实现步骤；当前阶段不得执行这些步骤。

## 1. Requirements & Constraints

- **REQ-001**: 所有平台统一执行 `.trellis/workflow.md` Phase 1.0 的 Trellis `git worktree add` 创建流程。
- **REQ-002**: 删除 `create_thread`、`trellis-create-task` 和 `codex_host_managed` 的活跃任务创建入口。
- **REQ-003**: 新任务继续原子记录实际 branch、显式 base、worktree root、Trellis owner/policy。
- **REQ-004**: 缺失父任务元数据时，任何 `--parent` 创建都必须在写子任务文件前失败。
- **REQ-005**: 旧 Codex-owned task.json 保持可读且不迁移。
- **CON-001**: 不使用提交级整体 revert，不覆盖 07-30 之后无关的 workflow、session 或 push 安全改动。
- **CON-002**: 不删除、移动或重建任何现有 worktree，不修改历史任务工件。
- **GUD-001**: 工作流、Skills、Python、规范和测试必须在同一提交范围内表达同一 ownership contract。

## 2. Implementation Steps

### Implementation Phase 1: Unify the workflow contract

- **GOAL-001**: 移除 Codex 专属 host 路由，使全部平台进入同一 Trellis Phase 1.0。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | 修改 `.trellis/workflow.md`：删除 Codex `create_thread` 分支，把当前非 Codex 的冲突检查、`git worktree add`、developer 初始化、平台无关 create 与创建后验证流程应用到所有平台；保留 Phase 2/3 后续无关修改。依赖 REQ-001、CON-001。 | | |
| TASK-002 | 修改 `.agents/skills/trellis-start/SKILL.md` 与 `.agents/skills/trellis-brainstorm/SKILL.md`，统一指向 Phase 1.0；删除 `.agents/skills/trellis-create-task/SKILL.md`。依赖 TASK-001、REQ-002。 | | |
| TASK-003 | 修改 `.agents/skills/trellis-meta/references/local-architecture/task-system.md`，并仅在需要时更新 `.codex/hooks/inject-workflow-state.py` 的 host-routed 注释；不得改变 breadcrumb 解析和 session pointer 行为。依赖 TASK-001、GUD-001。 | | |

### Implementation Phase 2: Simplify task creation runtime

- **GOAL-002**: 删除 host ownership 策略，同时保留平台无关的安全元数据与父子任务门禁。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | 修改 `.trellis/scripts/task.py`：移除 `--platform` 参数、Codex 专属 usage/help；保留 `--base-branch` 并把帮助文本改为统一的 reviewed PR target。旧 `--platform codex` 必须由 argparse 在执行 `cmd_create` 前拒绝。依赖 REQ-002、REQ-003。 | | |
| TASK-005 | 修改 `.trellis/scripts/common/task_store.py`：移除 platform/policy 解析与 host verifier import；固定新任务 owner=`trellis`、policy=`trellis_managed`；保留 detached HEAD、actual branch/base/root、unknown-field preservation；把父 `task.json` 存在性检查移到所有 `--parent` 请求的写入前门禁。依赖 TASK-004、REQ-003、REQ-004。 | | |
| TASK-006 | 全仓确认 `.trellis/scripts/common/worktree.py` 仅服务 host-managed 路径后删除该文件；清理非历史代码、文档和测试中的 import/符号引用。不得删除 `.trellis/scripts/tests/__init__.py`，除非独立证明其与测试发现无关且属于本任务必要范围。依赖 TASK-005、CON-002。 | | |

### Implementation Phase 3: Rewrite specifications and regression coverage

- **GOAL-003**: 用可执行测试锁定统一 Trellis ownership 与历史兼容行为。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-007 | 重写 `.trellis/spec/backend/trellis-task-worktree.md` 的 trigger、signature、contracts、error matrix 和 required tests；更新 `.trellis/spec/backend/index.md` 描述，明确历史 Codex meta 只读兼容。依赖 TASK-001、TASK-005、REQ-005。 | | |
| TASK-008 | 重构 `.trellis/scripts/tests/test_worktree.py`：删除 host schema/registry/Skill handoff 断言，新增统一 Phase 1.0、旧 CLI fail-before-write、Trellis metadata、重复 create 字段保留、父任务 fail-before-write/双向链接和旧 meta 可读测试。依赖 TASK-002、TASK-004、TASK-005、TASK-007。 | | |
| TASK-009 | 执行引用清零检查，范围排除 `.trellis/tasks/**` 历史证据；若活跃 Skill、workflow、Python、spec 或测试仍出现 `create_thread`、`trellis-create-task`、`codex_host_managed`、`verify_codex_host_worktree`，返回对应任务修复。依赖 TASK-006、TASK-008。 | | |

### Implementation Phase 4: Validate scope and compatibility

- **GOAL-004**: 证明回滚行为正确、范围最小且不破坏 Trellis 其他阶段。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-010 | 运行 `rtk python -m unittest discover -s .trellis/scripts/tests -p 'test_worktree.py' -v` 和 `rtk python -m unittest discover -s .trellis/scripts/tests -v`；任何失败必须修复后重跑。依赖 TASK-008。 | | |
| TASK-011 | 运行 `rtk python -m compileall -q .trellis/scripts .codex/hooks`、项目要求的相关 lint/type checks，以及 `rtk git diff --check`。依赖 TASK-009。 | | |
| TASK-012 | 审查 `rtk git diff --stat master...HEAD` 与完整 diff，确认没有历史任务重写、worktree 删除、`pr71_threads.json`、无关 Phase 2/3 修改或其他实现范围；核验当前任务状态在用户批准前仍为 planning。依赖 TASK-010、TASK-011、CON-001、CON-002。 | | |

## 3. Alternatives

- **ALT-001**: 直接 revert `8ef5df1`；拒绝，因为后续提交已修改同一文件，会误撤销父任务保护和无关工作流改进。
- **ALT-002**: 仅修改 workflow/Skills；拒绝，因为 `--platform codex` 与 host verifier 仍会形成隐蔽的第二创建路径。
- **ALT-003**: 保留 `--platform` 但忽略 `codex`；拒绝，因为 host-created worktree 会被静默错误标为 Trellis ownership。
- **ALT-004**: 批量迁移历史 Codex owner；拒绝，因为历史值是创建事实，且读取路径不需要迁移。

## 4. Dependencies

- **DEP-001**: Git linked worktree 能力与现有 `.trellis/worktrees/` ignore 规则。
- **DEP-002**: `.trellis/workflow.md` 的平台过滤器与 required-once breadcrumb 合同。
- **DEP-003**: `.trellis/scripts/common/task_store.py` 的现有 task.json unknown-field preservation 和 active-task hook 调用。
- **DEP-004**: 用户在规划评审后明确授权 `task.py start` 和实现；本计划本身不构成实现授权。

## 5. Files

- **FILE-001**: `.trellis/workflow.md` — 统一任务创建工作流。
- **FILE-002**: `.agents/skills/trellis-start/SKILL.md`、`.agents/skills/trellis-brainstorm/SKILL.md`、`.agents/skills/trellis-create-task/SKILL.md` — Skill 路由与 host adapter。
- **FILE-003**: `.agents/skills/trellis-meta/references/local-architecture/task-system.md` 与 `.codex/hooks/inject-workflow-state.py` — 本地架构说明和提示注释。
- **FILE-004**: `.trellis/scripts/task.py`、`.trellis/scripts/common/task_store.py`、`.trellis/scripts/common/worktree.py` — CLI 与 ownership runtime。
- **FILE-005**: `.trellis/scripts/tests/test_worktree.py` — 回归测试。
- **FILE-006**: `.trellis/spec/backend/index.md`、`.trellis/spec/backend/trellis-task-worktree.md` — 可执行规范。

## 6. Testing

- **TEST-001**: Phase 1.0 平台过滤测试证明 Codex 和非 Codex 都只暴露 Trellis `git worktree add` 路径。
- **TEST-002**: CLI 回归测试证明旧 `--platform codex` 在任务文件写入前失败，统一 create help 仍描述显式 base。
- **TEST-003**: 临时 Trellis worktree 测试证明实际 branch/base/root 与 Trellis owner/policy 正确，重复创建保留未知字段。
- **TEST-004**: 父子任务测试证明缺父 fail-before-write、存在父时双向链接。
- **TEST-005**: 历史 Codex meta fixture 的读取/验证测试证明不需要迁移。
- **TEST-006**: 全部 Trellis script unittest、compileall、相关 lint/type checks 和 `git diff --check` 通过。

## 7. Risks & Assumptions

- **RISK-001**: 直接按旧提交反向补丁会覆盖后续工作流内容；通过逐符号语义修改和 master diff 审查控制。
- **RISK-002**: 删除 host Skill 但漏改 breadcrumb/spec/test 会造成提示与运行时分裂；通过活跃范围引用清零与平台过滤测试控制。
- **RISK-003**: 删除 `--platform` 会使旧自动化失败；这是有意 fail-closed，必须在规范和错误预期中明确。
- **RISK-004**: 父任务检查泛化可能暴露此前非 Codex 的宽松行为；这是为保留数据完整性而接受的收紧，测试同时覆盖成功与失败路径。
- **ASSUMPTION-001**: 所有受支持 Agent 平台均能执行 Git 命令并把后续工具调用的 cwd 指向 Trellis 新建 worktree；这是恢复旧 Trellis 管理模式的前提。
- **ASSUMPTION-002**: 现有 task.json 读取方不枚举限制 owner/policy 值；实现时必须以测试验证，若发现限制，仅增加读取兼容，不迁移数据。

## 8. Related Specifications / Further Reading

- `.trellis/spec/backend/trellis-task-worktree.md`
- `.trellis/workflow.md` Phase 1.0
- `.trellis/tasks/07-30-adaptive-worktree-management/prd.md`
- `.trellis/tasks/07-30-adaptive-worktree-management/design.md`
- `.trellis/tasks/08-01-restore-trellis-worktree-management/research/adaptive-worktree-rollback-evidence.md`
