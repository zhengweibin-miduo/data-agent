# 用户触发 Codex 解决 PR 冲突：实施计划

1. 新增冲突委派脚本。
   - 精确命令与双白名单校验。
   - mergeability 有限重试和严格冲突判断。
   - base/head 二次校验、marker 去重及轮次上限。
   - 生成包含完整 Git 安全合同的中文委派正文。
   - 增加独立 Node 自检。
2. 新增 `issue_comment.created` workflow。
   - 仅响应 PR 评论命令。
   - 使用默认分支受信任脚本、最小权限和 `CODEX_TRIGGER_TOKEN`。
3. 验证。
   - Node 自检和语法检查。
   - YAML 解析及可用时运行 `actionlint`。
   - 现有 CI/review delegation 自检。
   - Ruff、Pyright 与 `git diff --check`。
4. 最终复核。
   - unknown、非冲突、越权、fork、重复触发和 head/base 变化均不得委派。
   - 委派禁止 rebase/force-push，并要求一个 merge commit 推送原分支。
