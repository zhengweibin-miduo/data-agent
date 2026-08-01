# 执行计划

1. 检查 PR #76 元数据、当前 checkout/task metadata 以及 publisher/workflow/tooling 的分支参数来源，记录 file:line 和实际 head 证据。
2. 定位将本地分支 `work` 直接用于 PR 查询的代码路径，设计最小兼容修复。
3. 实现分支映射修复；确保三条线程仍全部走结构化 publisher。
4. 运行针对分支解析的测试、静态检查或 publisher dry-run，并确认错误字符串不再出现。
5. 逐条验证三条线程的 publisher-mediated 处理入口和结果；不直接回复或 resolve 线程。
6. 在启动任务进入 `in_progress` 前，由用户审阅并批准本规划；本文件不授权实现、提交或推送。

## 验证与回滚点

- 验证：publisher 查询使用 PR #76 实际 head；三条线程均返回 publisher 处理结果。
- 回滚：仅撤销分支映射相关文件改动，保留 task artifacts；不得 reset 或改写共享历史。
