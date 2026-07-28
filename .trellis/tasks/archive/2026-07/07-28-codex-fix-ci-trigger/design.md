# 用户触发 Codex 修复 CI：技术设计

## 边界

- 新增 `issue_comment.created` workflow，仅识别 PR 评论中的精确命令 `/codex-fix-ci`。
- 新增独立 Node.js 委派脚本，沿用现有 Codex review delegation 的受信任脚本、自检和 PAT 模式。
- 不修改 CI workflow，不在 CI 失败时自动执行。

## 数据流

1. workflow 识别 PR 评论命令并检出默认分支的 `.github/scripts`。
2. 脚本读取 PR，校验 draft、同仓 head、PR 作者和触发者均在 `PR_AUTHORS` 白名单，并记录当前 head SHA。
3. 查询当前 head 对应的已完成失败 Actions runs，并读取失败 jobs 的 GitHub URL。
4. 再次读取 PR 确认 head 未变化。
5. 用 PR、head SHA 和失败 run ID 生成 marker；存在相同 marker 时直接返回。
6. 发布包含 `@codex`、失败链接和执行约束的中文委派评论。

## 安全与兼容

- workflow 使用 `contents: read`、`pull-requests: read`、`actions: read`；发评论通过已有 `CODEX_TRIGGER_TOKEN`。
- 只执行默认分支脚本，禁止执行 PR head 内容。
- 仅支持同仓 PR，避免 PAT 对 fork head 的写入风险。
- 只传递 GitHub Actions 页面链接和失败名称，不把完整日志复制到评论。

## 幂等与限流

- marker 对失败 run ID 排序后编码，确保集合顺序不影响幂等。
- 同一 head 出现新的失败 run 集合可再次人工触发。
- 沿用有限自动委派轮次；达到上限时只发布一次中文停止通知。

## 回滚

- 删除新增 workflow 和脚本即可完全移除能力；现有 CI 与 review triage 不受影响。
