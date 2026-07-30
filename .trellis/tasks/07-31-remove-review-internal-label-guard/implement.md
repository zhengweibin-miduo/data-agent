# 实施计划

1. 新增结构化回复 CLI，将校验、格式化、GitHub 查询、reply 和 resolve
   顺序封装在一个模块中。
2. 为参数/内容校验、三种 outcome、远端 SHA、resolved 跳过、幂等恢复和
   reply 失败补充 Node 自测。
3. 更新 Codex 委派提示词，只允许通过 CLI 发布 thread 回复。
4. 更新 `code_review.md` 和 backend quality spec，记录可执行合同。
5. 运行两个 Node 自测、Trellis 校验、相关检索与 `git diff --check`。
6. 创建新的本地提交，不 amend 既有提交，不推送。
