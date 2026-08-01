# 评估 Superpowers TDD 测试指导

## Goal

评估 `obra/superpowers@test-driven-development` 是否适合作为本项目的测试开发指导，并明确它与仓库现有测试规范、Trellis 工作流及质量门禁之间应采用替换、补充还是不引入的关系。

## Background

- 用户提出的候选安装命令为 `npx skills add obra/superpowers@test-driven-development -g -y`。
- 用户反馈当前测试过于臃肿，并且 CI 经常出现问题；评估必须以减轻测试维护成本和提高 CI 稳定性为核心，而不是把“测试数量更多”当作成功。
- 本轮先完成评估；用户随后明确授权按推荐组合进行项目级安装，但未授权全局安装、修改项目规范、提交、推送或创建 PR。

## Confirmed Facts

- 项目使用 pytest 与 pytest-asyncio；CI 还强制执行 Ruff、Pyright、compileall、配置校验和 Docker 服务初始化，TDD skill 不能替代这些门禁。
- 当前仓库约有 366 个测试函数、66 个 `test_*.py` 文件；约 92.6% 为单元测试、7.4% 为集成测试。臃肿信号主要来自约 616 次自定义 `check_*` 调用、重复 mock/setup，以及少数超过 100 行的集成测试场景，而非单纯“集成测试数量太多”。
- 最近 50 次 `CI` workflow 运行中 48 次成功、2 次失败。一次失败于 Pyright 类型检查；另一次为已有 CORS 集成测试抓到行为回归（396 通过、1 失败）。两者都不是“缺少测试优先流程”可直接解决的问题。
- 候选 skill 是严格的 RED-GREEN-REFACTOR 行为指导，要求生产代码之前必须先有失败测试，并倾向于每个行为/函数都有测试；它不是 pytest、CI 或项目测试框架的替代品。
- `-g` 会安装到用户级目录并跨项目生效；`-y` 会跳过所有确认提示。该作用域不适合未经裁剪地承载本项目专属测试规则。
- 截至 2026-08-01，`mattpocock/skills@tdd` 所在仓库约 198.6K GitHub stars、skill 约 539.5K installs，仓库于 2026-07-31 仍有推送，TDD skill 最近一次提交为 2026-07-03。它要求只在预先确认的公共 seam 上测试行为，并采用纵向小切片，较适合控制本项目测试数量和实现耦合。
- `obra/superpowers@verification-before-completion` 所在仓库约 264.7K GitHub stars、skill 约 163.4K installs，仓库于 2026-07-31 仍有推送，该 skill 最近一次提交为 2026-07-24。它要求完成前运行完整、最新的验证命令，较适合防止 Pyright 或相关测试未在提交前运行。
- `obra/superpowers@systematic-debugging` 约 202.6K installs，最近一次提交为 2026-07-24；它适合对 CI 失败做根因分类，但不直接约束测试套件规模。
- `mattpocock/skills@qa` 已位于仓库的 `skills/deprecated/qa/`，不应因历史安装量而推荐。

## Requirements

- 基于仓库内真实的测试配置、测试目录、开发规范和现有 skills 进行判断。
- 核验候选 TDD skill 的实际内容与安装作用域，不仅依据名称推断。
- 区分“指导测试开发流程”与“替换测试框架、测试命令或项目质量门禁”。
- 判断候选 skill 是否能直接缓解测试臃肿与 CI 不稳定；如不能，指出问题错配并提出更合适的指导方向。
- 给出明确建议、适用边界、潜在冲突和安全的采用方式。
- 推荐候选时优先选择高 GitHub 收藏、近期仍有更新且来源可信的 skill，并记录核验日期与数据来源。
- 按用户确认的推荐组合，以项目级、仅 Codex 的作用域安装 `mattpocock/skills@tdd` 与 `obra/superpowers@verification-before-completion`。
- 新增 skill 不得覆盖现有项目 skill；与 AGENTS.md、Trellis workflow 或 `.trellis/spec/` 冲突时，项目规则优先。
- 在根目录 `AGENTS.md` 的 Trellis 管理块之外说明两个 skill 的触发条件、组合顺序、测试精简原则及项目规则优先级。
- 按用户决定只调整当前测试规范、不重构现有测试：更新后端质量规范，使新增或修改测试优先覆盖已确认的公共 seam 与可观察行为，并按风险选择测试层级。
- 测试规范不得要求为每个内部函数机械增加测试；mock 仅用于不可避免的系统边界，不验证内部协作者调用细节。
- 自定义 `tests.helpers.checks` 不再作为所有新测试的强制写法；新测试优先使用 pytest 原生断言与异常工具，只有确需统一可观察输出的场景才复用 `check_*`。

## Acceptance Criteria

- [x] 列出现有项目测试技术栈、主要命令和当前测试指导来源。
- [x] 说明候选 TDD skill 能否直接替换现有测试指导，以及不能替换的内容。
- [x] 明确全局安装对项目仓库的影响及是否建议执行该命令。
- [x] 用仓库与近期 CI 证据区分测试规模、测试设计和 CI 基础设施/环境问题，避免把所有失败归因于缺少 TDD。
- [x] 如建议采用，给出最小化接入方案；如不建议，说明理由和替代方案。
- [x] 至少比较 2 个与测试精简、测试质量或 CI 稳定性相关的候选，列出 GitHub stars、最近更新时间、skill 热度及项目适配度。
- [x] `npx skills ls -a codex` 能列出两个新增的项目级 skill，且未出现对应的全局安装。
- [x] 检查实际落盘文件、Git diff 和 skill 内容，确认只新增预期文件且来源内容完整。
- [x] `AGENTS.md` 明确 `tdd` 仅用于测试优先的功能、修复和测试重构，先确认公共 seam，再按纵向小切片编写行为测试。
- [x] `AGENTS.md` 明确完成前使用 `verification-before-completion` 获取新鲜完整证据，并且两个 skill 均不替代 Trellis、项目 spec 或 CI 门禁。
- [x] `.trellis/spec/backend/quality-guidelines.md` 明确公共 seam、行为测试、纵向小切片、风险分层和避免内部 mock/机械覆盖。
- [x] `.trellis/spec/backend/quality-guidelines.md` 取消对所有新测试强制使用 `check_*` 的要求，同时不要求本任务重构既有测试。

## Out of Scope

- 不执行全局 skill 安装。
- 不修改测试代码、测试框架、CI 或其他项目级 skill；允许更新根目录 `AGENTS.md` 与后端测试质量规范，但不在本任务中迁移既有测试。
- 不提交、推送或创建 Pull Request。
