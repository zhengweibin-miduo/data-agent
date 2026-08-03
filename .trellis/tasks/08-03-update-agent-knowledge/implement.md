# Implementation Plan

- [x] 精简并重组 `docs/agent-knowledge.html`，保留现有页面壳、目录和无依赖脚本。
- [x] 用当前路径与代码事实写入七类核心知识，删除已失真的旧描述。
- [x] 生成 `docs/diagram/agent-knowledge/agent-system.svg` 与 @2x PNG，并在 HTML 中引用 SVG。
- [x] 机械检查所有代码路径、关键状态和旧术语。
- [x] 浏览器渲染 HTML，视觉检查桌面与窄屏布局及 SVG。
- [x] 运行 `git diff --check`，复核只修改任务元数据和文档资产。
- [x] 将四角展开入口叠放到图片右上角，增加以视口中心为锚点的滚轮与 `− / +` 缩放，并验证边界和窄屏平移。
- [x] 使用原生 Pointer Events 增加鼠标/触摸抓取拖拽，并保留可聚焦查看区域与方向键平移。
- [x] 按“问题、示例、机制、边界、代码定位”重写七个章节的解释层，不删除已有准确技术事实。
- [x] 新增 Chat 时序、DDL Job 生命周期、Snapshot 异步收敛、Conversation/Memory 四张专题 SVG 与 @2x PNG。
- [x] 将单图查看器改为所有架构图共用的原生 `<dialog>`，保留中心缩放、抓取平移、键盘与焦点行为。
- [x] 在桌面与 390px 窄屏逐张检查正文、图注、SVG 可读性、展开/缩放/拖拽和页面溢出。
- [x] 使用 Web Interface Guidelines 复查并修复新增交互与内容结构问题，再运行内容断言和 `git diff --check`。

## Validation

```bash
python -c "from pathlib import Path; p=Path('docs/agent-knowledge.html'); assert p.exists() and '<!DOCTYPE html>' in p.read_text(encoding='utf-8')"
python -c "from pathlib import Path; p=Path('docs/diagram/agent-knowledge/agent-system.svg'); assert p.exists() and '<svg' in p.read_text(encoding='utf-8')"
git grep -n -E '9 个|RUNNING / SUCCEEDED / REJECTED|memory/indexing/\\{dispatcher|persistence/\\{snapshots' -- docs/agent-knowledge.html
git diff --check
```
