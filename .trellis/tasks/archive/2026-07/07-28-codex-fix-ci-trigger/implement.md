# 用户触发 Codex 修复 CI：实施计划

1. 新增 CI 修复委派 Node 脚本。
   - 解析精确命令。
   - 校验 PR、触发用户、当前 head 和失败 Actions runs/jobs。
   - 生成幂等 marker 和简体中文 Codex 委派正文。
   - 加入独立可运行的最小自检。
2. 新增 `issue_comment.created` workflow。
   - 仅响应 PR 评论命令。
   - 检出默认分支受信任脚本。
   - 配置最小权限和 `CODEX_TRIGGER_TOKEN`。
3. 验证。
   - `node .github/scripts/codex-ci-fix-delegation.js`
   - `actionlint`（若仓库环境可用）
   - `git diff --check`
   - 人工核对 workflow 事件、权限、并发键和脚本输入。
4. 质量复核。
   - 不执行 PR head 代码。
   - 非当前 head 的失败 run 不进入委派。
   - 无失败、重复触发、越权和 head 变化路径均安全返回。
