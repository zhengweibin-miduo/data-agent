# 执行计划

## 影响范围

- `.agents/skills/codebase-onboarding/`：项目级安装 `codebase-onboarding` 及其随附文件。
- `AGENTS.md`：新增“完整项目知识地图”组合技能规则；保留并协调既有技术分析文档与前端组合规则。
- `.trellis/tasks/08-01-add-knowledge-map-skill/`：保留本任务规划与验证记录。

## 执行顺序

1. 在仓库根目录执行已批准的项目级安装命令：
   `npx skills add affaan-m/ECC --skill codebase-onboarding`
2. 检查安装输出与实际文件，确认目标为 `.agents/skills/codebase-onboarding/`，且 `SKILL.md` 可发现。
3. 阅读安装后的 `SKILL.md`，以真实能力边界校准 `AGENTS.md` 中对 `codebase-onboarding` 的职责描述。
4. 在根目录 `AGENTS.md` 的现有组合规则之外新增“完整项目知识地图”章节，写明触发范围、五个技能的建议顺序与协作关系。
5. 运行下面的验证命令并修复发现的问题；不进入提交、推送或 PR 流程。

## 验证命令

```bash
python -c "from pathlib import Path; p=Path('.agents/skills/codebase-onboarding/SKILL.md'); print(p.resolve(), p.is_file()); raise SystemExit(0 if p.is_file() else 1)"
rg -n "完整项目知识地图|codebase-onboarding|domain-modeling|codebase-design|baoyu-diagram|web-design-engineer" AGENTS.md
git diff --check
git status --short
git diff -- AGENTS.md .agents/skills/codebase-onboarding .trellis/tasks/08-01-add-knowledge-map-skill
```

## 审查门禁与回退

- 用户确认本规划后，才可运行 `task.py start` 并进入 Phase 2。
- 若安装命令写入项目范围外、覆盖已有技能或产生额外无关文件，立即停止，不清理或覆盖未知改动，先报告实际路径和 diff。
- 若 `web-design-engineer` 的当前缺失会导致组合规则不可执行，本任务仍不擅自扩大安装范围；按已批准文本保留名称，并在交付说明中明确该既有差异。
- 本任务没有 commit、push 或 PR 授权；即使验证通过也必须停留在本地未提交状态。
