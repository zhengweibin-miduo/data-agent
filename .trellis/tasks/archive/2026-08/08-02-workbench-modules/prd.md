# 深化 Workbench 模块与测试

## Goal

提炼 Workbench feature 内部 module/seam，保持 HTTP/SSE 与状态权威，并替换重复或实现耦合测试。

## Background

- `WorkbenchPage.tsx` 约 522 行并集中多个状态机；外部 props interface 很小，但内部 locality 与测试 seam 不足。
- `WorkbenchPage.test.tsx` 约 706 行/34 tests，部分场景直接驱动 `connectJobEvents.mock.calls` 内部 callback，并与 API/SSE adapter 测试重复机制覆盖。
- 前后端源码根、API-only backend、legacy switch 和 `frontend/src/api` transport seam 已符合规则，不需要重新拆分工程。

## Requirements

- 在 `frontend-design` 确认不改变既有视觉方向后，提炼有真实变化点的 feature-internal modules/hooks；不得为单一 implementation 机械创建接口。
- 保持 URL、React state、refs、session/local storage 与 backend authoritative state 的唯一所有者和恢复语义。
- 保持 HTTP/SSE payload、ApiError、native reconnect/polling、idempotent submission 与 conversation retry contracts。
- API/SSE adapter tests 证明 transport/state-machine 机制；Workbench feature tests 只证明用户可观察 restore/submission/clarification/chat 行为。
- 新 interface 覆盖后删除直接 callback 驱动和重复 adapter 机制测试；使用窄 factory/helper，避免全局超宽 fixture。
- 实现后使用 `web-design-guidelines` 审查并修复可访问性、UX、性能问题，再复查。

## Acceptance Criteria

- [ ] Workbench 的 restore、submission、job subscription、clarification、chat 具有明确 internal module/interface 与单一状态所有者。
- [ ] 用户可观察行为、URL/session recovery、HTTP/SSE 和 backend authority contracts 保持不变。
- [ ] Workbench feature tests 不再从 mocked adapter call history 取得内部 callback 驱动页面。
- [ ] adapter 与 feature 测试职责不重复，新 seam 完成 red-green，被替代测试已删除。
- [ ] `npm run lint`、typecheck、test、build 及相关 Python frontend tests 通过。
- [ ] `web-design-guidelines` 审查、修复和复查完成。

## Out of Scope

- 视觉重设计、新功能、后端契约变更或 legacy frontend 业务开发。
