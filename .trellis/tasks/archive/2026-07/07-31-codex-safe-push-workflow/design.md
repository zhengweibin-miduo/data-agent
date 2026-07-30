# 设计：受干预 PR 分支的安全提交与推送

## 背景

PR #71 的同一 head 分支会被本地 Codex、`codex-cloud[bot]` 和仓库的
GitHub 委派工作流连续更新。现有规则把远端 head 与任务开始时的
`Expected head` 不一致视为无条件停止，因此即使远端只是增加了一个线性
后继提交，本地任务也无法继续提交和推送。

问题不是 Git 缺少保护，而是工作流没有区分“可吸收的远端线性前进”和
“需要人工判断的真实分叉”。普通 `git push` 已经能在最后一步拒绝
non-fast-forward 竞态，不需要 force-push。

## 设计原则

1. 授权和安全分离：先确认 commit / push 授权，再进行历史安全判定。
2. 以提交图为事实来源：用 fetch 后的 SHA 和祖先关系判断，不以 actor
   名称或“看起来像 Codex”作为安全依据。
3. 只自动处理可证明属于本任务的未发布工作；不改写共享历史。
4. 每次同步后重新做范围检查和相关验证。
5. 最终以普通 push 的 fast-forward 拒绝作为竞态保护；拒绝后重新判定，
   不升级为 force。

## 状态快照

进入 commit / push 阶段时记录：

- `task_base_sha`：本任务开始修改前的本地基线。
- `local_head_sha`：当前本地 HEAD。
- `remote_head_sha`：fetch 后 PR head。
- `local_only_commits`：`remote_head_sha..local_head_sha` 中尚未发布的提交。
- `remote_only_commits`：`local_head_sha..remote_head_sha` 中新出现的提交。
- `task_paths`：已核验属于本任务的改动路径。
- `unrecognized_paths`：不属于或无法确认属于本任务的脏文件。

## 判定矩阵

| 状态 | 判断 | 允许动作 |
| --- | --- | --- |
| 远端等于预期 head | 无干预 | 完成范围/验证门禁后正常 commit、普通 push |
| 本地 HEAD 是远端祖先，且没有本地提交 | 远端线性前进 | `merge --ff-only` 到远端，重新核验 diff 和验证 |
| 任务基线是远端祖先，本地只有已核验、从未发布的本任务提交 | 双方工作都源于同一任务基线 | 将这些未发布提交重放到最新远端；遇到冲突立即停止；成功后重新验证 |
| 工作区只有已核验的本任务未提交改动，远端线性前进 | 可先形成未发布任务提交 | 按已批准 commit plan 提交，再按上一行处理；不得自动 stash |
| 远端再次前进但仍满足上述安全条件 | 可恢复竞态 | 有界地重新 fetch、判定、同步和验证，再普通 push |
| 本地或远端无法证明祖先关系，或存在非本任务本地提交 | 真实分叉或归属不明 | 停止并报告 SHA、分叉点和阻塞原因 |
| 同步/重放产生冲突，存在未识别脏文件重叠，或验证失败 | 无法自动保证语义 | 停止；不清理、不 reset、不强推 |
| 只能通过改写已共享历史或 force 才能推送 | 不安全 | 停止 |

“重放未发布提交”只适用于从未出现在远端 PR head 历史中的本任务提交；
它不是对共享分支执行一般性 rebase 的许可。

## 操作协议

1. 校验已有授权、目标 repo、remote、PR base/head 和实际分支名。
2. `git fetch --prune origin`，采集状态快照。
3. 分类工作区文件和本地提交；任何 unrecognized 内容均不自动纳入。
4. 根据判定矩阵选择继续或停止。
5. 同步后重新检查 `git diff`、commit 范围和必要验证。
6. 使用显式目标的普通 push：`git push origin HEAD:<pr-head>`。
7. push 若因远端前进被拒绝，最多重新进入一次完整判定；再次竞态则停止，
   避免自动任务长期互相追逐。
8. 永不使用 `--force` 或 `--force-with-lease` 处理这种干预。

## 影响面

- `.agents/skills/git-pr-rules/SKILL.md`
  - 增加统一的远端干预判定矩阵和操作协议。
- `.trellis/workflow.md`
  - Phase 3.4 接入判定矩阵；区分“已有授权可直接执行”和“尚未授权需确认”。
- `.github/scripts/codex-review-delegation.js`
- `.github/scripts/codex-ci-fix-delegation.js`
- `.github/scripts/codex-conflict-resolution-delegation.js`
  - 委派提示词要求先读项目 Git/PR Skill，不再因 head 单纯变化无条件停止。
  - 保留准备委派时 GitHub API 的陈旧快照检查；Action 不应基于旧问题创建
    新委派，这与委派任务运行后的安全收敛是两个不同边界。
  - 更新脚本内置测试断言。

## 不做的事情

- 不关闭 GitHub 委派自动化。
- 不修改 Actions 的 `contents: read` 权限。
- 不允许多个任务静默覆盖同一路径的语义冲突。
- 不引入自动 force-push、reset、stash 或改写已发布提交。
- 不创建新 PR，也不改变 PR #71 的 base/head。
