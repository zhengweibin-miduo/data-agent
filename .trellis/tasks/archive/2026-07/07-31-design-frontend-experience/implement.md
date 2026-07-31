# Data Agent 前端实现记录

> 用户已批准实现；任务处于 `in_progress`。以下清单记录已完成范围与因本地依赖不可用而未执行的 live 验证。

## 实施顺序

- [x] 删除被否决的宣传页式布局与样式；保留已验证的 API 状态机和错误处理逻辑。
- [x] 新增只读 DDL preview API，直接复用现有确定性解析器，并补充契约/校验测试。
- [x] 将工作台重构为全屏三栏 + 底部 Trace，使用 preview 响应渲染真实表节点和外键 SVG 关系。
- [x] 实现窄屏重排、节点键盘焦点、关系文本等价信息和 reduced-motion 状态。
- [x] 选择无框架原生 HTML/CSS/JavaScript，复用 FastAPI 静态服务，不增加依赖。
- [x] 建立最小应用外壳、设计 tokens、字体回退、路由与 API 基础错误投影。
- [x] 在后端新增最小聊天编排端点：复用 `ConversationService.start_turn` 组装上下文，服务端调用共享 LLM client，再由 `complete_turn` 持久化助手消息和触发记忆提炼。
- [x] 对数据问答接入 `AnswerReadinessService`；保持固定未就绪文案和内部诊断边界。
- [x] 实现 AI 协作侧栏、会话恢复、生成/失败状态，以及“AI 草稿 → 用户确认 → DDL answers API”的显式交接。
- [x] 聊天请求绑定当前 `source` 与 DDL/job 上下文；拒绝在首版扩展为无上下文问数入口。
- [x] 实现 DDL 工作台输入态与真实契约校验。
- [x] 实现任务状态机：SSE 主通道、查询回退、公开阶段轨迹、终态与可重试错误。
- [x] 实现等待澄清表单，正确处理 `revision`、`question_set_id`、必答项与 409 冲突。
- [x] 实现知识记忆搜索、详情/历史、带版本修正与软删除确认。
- [x] 按 `frontend-design` 的最终方案完成视觉实现，不增加通用 Dashboard 装饰。
- [x] 使用 `web-design-guidelines` 审查，修复后再次复查。

## 验证门禁

- [x] 运行 Ruff、Pyright、compileall、Node syntax/self-check、非集成测试和 wheel 构建。
- [x] 用现有契约测试覆盖公开任务状态、错误投影、SSE 回退和记忆版本边界。
- [x] 覆盖聊天编排的成功、模型失败、数据未就绪、消息持久化失败、重复 `turn_uid` 与上下文边界；确认前端不接触模型密钥。
- [ ] 使用真实外部服务走通 DDL 提交 → 进度 → 澄清 → 终态，以及记忆搜索 → 修正流程；本地 Docker 依赖未运行。
- [x] 使用 390px 和 1440px 真实渲染检查，并复核 768px/1200px CSS 断点、200% 缩放和 reduced motion 规则。
- [x] 按 Web Interface Guidelines 复核正文、焦点、状态和错误颜色的 WCAG 2.2 AA 基线。
- [x] 核对网络请求，确认前端不含 LLM 密钥、内部错误或 LangGraph 载荷。

## 风险与回滚点

- 技术栈已确定为原生 HTML/CSS/JavaScript + FastAPI 静态资源，不增加前端依赖。
- 后端没有任务列表和结果明细：相关 UI 只能在 API 扩展后加入，不能用静态假数据伪装。
- 服务端聊天编排已实现；浏览器只调用 `/chat-turns`，不接触模型密钥。
- SSE 兼容性：查询回退必须独立可用；若事件流实现不稳定，可回滚为有界轮询而不改变产品流程。
- 记忆修正会触发重新处理：交互必须明确影响并保留版本冲突保护。
