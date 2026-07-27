# Implementation Plan

1. 将 `codex-review-triage.yml` 简化为 `pull_request_review: submitted` 触发的用户委派流程。
2. 使用 GitHub Script：
   - 校验 reviewer、PR 作者、同仓 head 和 draft 状态；
   - 拉取本 review 对应的未解决 Codex threads；
   - 用 `CODEX_TRIGGER_TOKEN` 搜索幂等标记并以用户身份发布 `@codex` 委派评论。
3. 添加一个小型脚本测试覆盖：
   - 无 threads 不评论；
   - 同一 review/commit 不重复评论；
   - 委派文案包含修复/说明/resolve/blocked 约束。
4. 验证：
   - YAML 可解析；
   - 委派逻辑测试；
   - `uv run ruff check src tests`；
   - `uv run pyright`；
   - `git diff --check`。
5. 提交后将任务分支新增提交推送到 PR #51 的现有 head 分支。

## Deployment Prerequisite

已在 `zhengweibin-miduo/data-agent` 配置 Actions Secret `CODEX_TRIGGER_TOKEN`；它对应仅限当前仓库、具有 Pull requests: Read and write 的细粒度 PAT。不得改为当前高权限 `gh` 登录 token。
