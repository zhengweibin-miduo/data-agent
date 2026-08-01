# 实施计划

## 修改步骤

1. 在 `AGENTS.md` 的前端组合与完整知识地图组合之间新增“项目架构与职责边界组合技能”。
2. 写明四个 Skill 的按需触发条件、多个环节同时适用时的顺序、真实职责和停止点，区分读取 `CONTEXT.md` 与主动使用 `domain-modeling`，并保留 `improve-codebase-architecture` 的临时 HTML 交付物。
3. 写明 `frontend/`、`src/data_agent/` 和可选 `contracts/` 的所有权，禁止跨根源码导入。
4. 写明后端 DDD/Ports and Adapters 的职责、向内依赖、bounded context 与组合根规则，并注明渐进式适用。
5. 写明前端轻量 feature-first 的应用壳、feature、共享 API、共享 UI 和状态所有权原则。
6. 写明与技术方案、前端设计审查、完整知识地图和 Trellis 工作流的协同关系。

## 验证

```bash
git diff --check
git diff -- AGENTS.md
rg -n "architecture-blueprint-generator|architecture-patterns|domain-modeling|improve-codebase-architecture|frontend/|src/data_agent/|feature-first|Ports and Adapters" AGENTS.md
python ./.trellis/scripts/task.py validate 08-01-project-architecture-rules
```

## 风险与回退点

- 风险：把目标架构写成当前完成状态。通过“新建或实质重构时”和“渐进迁移”措辞规避。
- 风险：与现有组合技能重复触发。通过独立触发条件和协同声明规避。
- 风险：目录规则与 PR #76 的前端迁移并行变化。规则只依赖稳定根目录与职责，不固化具体组件文件。
- 回退：删除新增的 `AGENTS.md` 段落；不涉及运行时或数据回退。

## 启动前检查

- 用户审阅并批准 PRD、设计和实施计划。
- Trellis 上下文清单包含本任务需要的规范和研究证据。
- `task.py start` 后再修改 `AGENTS.md`。
