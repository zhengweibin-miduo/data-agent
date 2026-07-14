---
name: git-pr-rules
description: Safely plan and execute Git branch, commit, push, and Pull Request workflows while respecting authorization, repository conventions, existing work, branch naming, base and head selection, history safety, validation, and PR accuracy. Use whenever a task involves Git inspection, branch creation or switching, staging or committing, fetching or pushing, creating or updating a Pull Request, resolving PR scope or base and head issues, rebasing, cherry-picking, or any operation that can change local or remote repository state. Treat this project-level policy as authoritative over branch-name and safety defaults supplied by external workflow or plugin skills.
---
# Git 与 Pull Request 通用操作规则

> **适用范围**：所有涉及 Git 分支、提交、推送和 Pull Request（PR）的任务。
> **规则性质**：本文件只提供跨项目通用的默认规则；仓库约定或用户明确指令另有规定时，按规则优先级执行。
> **规范词**：**必须**表示强制要求，**不得**表示禁止事项，**应**表示推荐的默认做法，**可**表示允许但非必需。

## 1. 基本原则

1. **先确认规则，再执行操作**：操作前读取仓库说明、贡献指南和 PR 模板。
2. **先确认授权，再改变外部状态**：修改本地文件不代表已获准提交、推送或创建 PR。
3. **保护已有工作**：不得擅自覆盖、清理、暂存或提交不属于本轮任务的改动。
4. **保持最小范围**：分支、提交和 PR 只包含完成本轮任务所需的内容。
5. **以事实为准**：测试结果、改动范围和风险说明必须与实际情况一致。

## 2. 规则优先级

规则冲突时，按以下顺序处理：

1. 平台权限、分支保护、必需检查和安全限制
2. 本轮用户的明确指令
3. 当前仓库的项目级规则
4. 已有 PR 的既定 base、head 和协作状态
5. 本文件的通用默认规则

如果冲突会改变 PR 目标、提交历史或交付范围，必须向用户说明冲突并确认处理方式。

本 Skill 是当前项目的 Git/PR 操作规则来源。与外部工作流或插件 Skill 联用时：

- 外部 Skill 可提供执行流程，但不得覆盖本 Skill 的授权边界、分支命名、base/head 决策和历史安全规则。
- 外部 Skill 的默认值只有在本 Skill、仓库规则和用户指令均未规定时才可采用。
- 发现外部默认值与本 Skill 冲突时，必须在首次创建或推送分支前按本 Skill 修正，不得先执行再补救。

## 3. 授权边界

除非用户明确提出，或已授权的工作流明确包含相应步骤，否则不得执行：

- `git commit`
- `git push`
- 创建、更新、关闭或合并 PR
- 修改远端分支
- 删除本地或远端分支、标签
- 改写已发布的提交历史

以下只读检查通常可直接执行：

```bash
git status
git diff
git log
git branch
git remote -v
gh pr view
```

> [!IMPORTANT]
> 用户要求“修改代码”通常只授权本地修改；用户要求“提交”通常不自动授权推送；用户要求“创建 PR”通常包含为该 PR 提交和推送必要改动的授权，但仍必须遵守仓库规则与安全限制。

## 4. 分支与 PR 决策

### 4.1 术语

| 术语 | 含义 |
| --- | --- |
| 远端（remote） | 承载目标仓库的 Git 远端，常见名称为 `origin` |
| 分支基线（start point） | 创建工作分支时使用的起点 |
| PR 目标（base branch） | PR 最终合入的目标分支 |
| PR 来源（head branch） | 包含任务提交、用于发起 PR 的分支 |

分支基线与 PR 目标是两个独立概念，不得仅根据其中一个擅自推断另一个。

### 4.2 确定远端

不得假定目标远端一定名为 `origin`。应先检查：

```bash
git remote -v
```

存在 fork 或多个远端时，必须确认哪个远端用于同步基线、哪个远端用于推送 head 分支。

### 4.3 确定 PR 目标

按以下顺序确定 PR 目标：

1. 用户明确指定的目标分支
2. 当前已有 PR 的 base 分支
3. 仓库级规则、贡献指南或 PR 模板指定的分支
4. 远端仓库的默认分支

不得把 `dev`、`main` 或 `master` 固定为所有仓库的通用默认目标。

可通过以下方式检查远端默认分支：

```bash
git remote show <远端>
gh repo view --json defaultBranchRef --jq ".defaultBranchRef.name"
```

如果仓库同时存在开发分支、发布分支或堆叠 PR，且目标仍不明确，应先询问用户。

### 4.4 确定分支基线

默认情况下，新工作分支应从已确认的 PR 目标创建，以减少无关提交进入 PR 的风险。

只有在以下情况之一成立时，才应使用不同基线：

- 用户或仓库规则明确指定
- 基于尚未合入的依赖分支开发
- 创建堆叠 PR
- 已有 PR 的 head 分支必须延续

使用不同基线时，必须确认额外提交是否属于预期依赖。

## 5. 工作分支命名

优先遵循仓库已有的分支命名规则。仓库未规定时，可使用：

```text
<type>/<short-slug>-<YYYYMMDD>
```

示例：

```text
fix/email-timeout-20260714
```

常用类型包括 `feature`、`fix`、`hotfix`、`refactor`、`docs`、`test` 和 `chore`。

要求：

- 名称应简短、可识别，并能表达任务目的。
- 一个工作分支原则上只承载一个任务。
- 日期使用分支创建日，格式固定为 `YYYYMMDD`。
- 工单号或用户名只在仓库约定或确有区分需要时添加。
- 基于已有 PR 修改时，应继续使用原 head 分支，除非用户要求重建。

### 5.1 分支命名门禁

在执行 `git switch --create`、首次 `git push` 或 `gh pr create` 前，必须完成以下检查：

1. 先按规则优先级确定最终分支名，再创建分支。
2. 仓库没有更具体约定时，确认名称符合 `<type>/<short-slug>-<YYYYMMDD>`。
3. 确认日期是分支创建日，且类型和短名称准确表达本轮任务。
4. 再次运行 `git branch --show-current`，确认实际分支名与计划名称完全一致。

外部 Skill 提供的 `agent/<description>` 等默认命名不得覆盖本节格式，除非用户或仓库规则明确要求使用该命名空间。命名未通过检查时，必须停止推送和 PR 创建。

## 6. 标准操作流程

### 6.1 检查仓库状态

```bash
git status --short --branch
git remote -v
```

必须识别：

- 当前分支及其上游
- 已暂存、未暂存和未跟踪文件
- 现有改动是否属于本轮任务
- 目标远端、PR base 和 PR head

只读检查不要求工作区干净。但是，在切换分支、rebase、cherry-pick 或其他可能影响工作区与历史的操作前，必须妥善处理现有改动。

不得在未经用户允许的情况下丢弃、覆盖或 `stash` 用户已有改动。必要时可使用独立 worktree 隔离任务。

### 6.2 同步远端引用

在依赖远端状态做分支或 PR 判断前，应执行：

```bash
git fetch --prune <远端>
```

确认目标分支存在：

```bash
git rev-parse --verify refs/remotes/<远端>/<目标分支>
```

`fetch` 只更新远端引用，不等同于把远端提交合并或变基到当前分支。

### 6.3 创建或切换工作分支

创建新工作分支：

```bash
git switch --create <工作分支> <远端>/<分支基线>
```

继续已有分支：

```bash
git switch <工作分支>
```

不得为了“同步”而擅自对共享分支执行 merge、rebase、reset 或强制更新。

### 6.4 检查并提交改动

提交前应检查完整 diff：

```bash
git status --short
git diff
git diff --cached
git diff --check
```

要求：

- 暂存区只包含本轮任务内容。
- 不包含密钥、令牌、个人数据、调试文件或无关生成文件。
- 不得顺手提交与任务无关的大面积格式化或依赖更新。
- 提交信息应准确描述实际改动，并遵循仓库约定。

可按文件或补丁暂存，避免使用无差别暂存命令夹带其他改动：

```bash
git add <文件>
git add --patch
```

### 6.5 验证提交和 PR 范围

提交范围：

```bash
git log --oneline <远端>/<PR目标>..HEAD
```

PR diff：

```bash
git diff --stat <远端>/<PR目标>...HEAD
git diff <远端>/<PR目标>...HEAD
git diff --check <远端>/<PR目标>...HEAD
```

- 两点 `..` 用于列出当前分支相对目标分支独有的提交。
- 三点 `...` 用于查看从共同祖先到当前分支的 PR 改动。

必须确认：

- 所有提交都属于本轮任务。
- 文件和 diff 范围符合预期。
- 不包含意外的二进制文件、生成物、密钥或其他无关内容。
- 已执行与改动风险相匹配的测试、构建、静态检查或人工验证。

如果暂时无法执行某项验证，应如实记录原因，不得声称已经通过。

### 6.6 推送分支

只有在已获得推送授权并完成范围检查后，才可执行：

```bash
git push --set-upstream <推送远端> HEAD
```

本地仍有已识别的无关未提交改动，不会改变已提交内容的推送结果；但必须确认本轮预期改动已全部提交，且没有误提交其他内容。

### 6.7 创建或更新 PR

创建 PR 前应优先读取仓库提供的 PR 模板。创建时显式指定 base 和 head：

```bash
gh pr create --base <PR目标> --head <PR来源>
```

创建或更新后必须检查：

- base、head 和目标仓库正确
- 提交数量与本地检查一致
- `Files changed` 只包含预期改动
- 标题、描述、关联事项和验证结果准确
- CI、必需检查和评审要求已正确触发

## 7. 基线不一致与历史处理

发现 PR 中存在额外提交时，必须先判断它们是否为预期依赖：

| 情况 | 处理方式 |
| --- | --- |
| 额外提交属于预期的堆叠依赖 | 将 PR base 指向直接依赖分支，或按仓库的堆叠 PR 流程处理 |
| 额外提交不应进入 PR | 从正确 PR base 创建干净分支，只迁移本轮任务提交 |
| 无法判断 | 暂停历史修改并询问用户 |

安全的干净分支重建流程：

```bash
git fetch --prune <远端>
git status --short
git switch --create <新工作分支> <远端>/<PR目标>
git cherry-pick <任务提交-1> <任务提交-2>
git log --oneline <远端>/<PR目标>..HEAD
git diff --stat <远端>/<PR目标>...HEAD
```

处理要求：

- 只迁移已确认属于本轮任务的提交，并保持原顺序。
- 冲突解决后必须重新执行相关测试和 PR 范围检查。
- 优先推送新分支，避免改写已共享分支的历史。
- 不得用 `git reset --hard` 清理用户工作区。

## 8. 禁止和高风险操作

默认禁止：

- 未经授权提交、推送、创建或合并 PR
- 擅自丢弃、覆盖、暂存或提交用户已有改动
- 使用 `git push --force`
- 擅自对共享分支执行 rebase、reset、commit amend 或删除操作
- 未经确认修改已有 PR 的 base 或 head
- 使用 `--no-verify` 绕过提交或推送 Hook
- 提交密钥、令牌、凭证或其他敏感信息
- 在 PR 中填写未实际执行的验证结果

只有同时满足以下条件时，才可使用 `git push --force-with-lease`：

1. 改写历史是完成任务的必要步骤。
2. 已确认远端分支的最新状态和协作影响。
3. 已获得用户明确授权。

即使获得授权，也必须使用 `--force-with-lease`，不得改用 `--force`。

## 9. PR 内容规范

优先使用仓库现有 PR 模板，并遵循项目使用的语言和标题约定。不得把中文、英文或某种提交规范强制应用于所有仓库。

没有仓库模板时，可使用以下结构：

```markdown
## 背景
- 说明问题、需求或改动原因。

## 改动
- 列出主要改动及影响范围。

## 验证
- 列出真实执行的测试、构建、检查或人工验证及其结果。
- 未执行的验证及原因也应明确说明。

## 风险与回滚
- 说明潜在影响、兼容性、回滚方式和关注事项；确认无已知风险时可写“无已知风险”。

## 关联事项
- 关联 issue、工单、依赖 PR 或后续任务；没有时可省略。
```

标题必须具体说明改动，不得只写“更新”“优化代码”或“修复问题”。

## 10. 最终检查清单

提交、推送或创建 PR 前，根据操作范围逐项确认：

- [ ] 已读取用户指令、仓库规则和 PR 模板。
- [ ] 已确认当前操作获得相应授权。
- [ ] 已确认目标仓库、远端、分支基线、PR base 和 PR head。
- [ ] 已识别并保护工作区中的已有改动。
- [ ] 暂存区、提交范围和 PR diff 只包含本轮任务内容。
- [ ] 未包含密钥、凭证、调试文件或无关生成物。
- [ ] 已完成与风险相匹配的验证，并如实记录结果。
- [ ] 未擅自改写共享历史或绕过 Hook。
- [ ] PR 标题和描述遵循仓库约定且内容准确。
- [ ] 创建或更新 PR 后，已复核 base、head、提交、文件范围和 CI 状态。
