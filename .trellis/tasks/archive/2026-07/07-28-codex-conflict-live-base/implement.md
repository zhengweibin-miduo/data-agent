# 修复冲突委派 base SHA 来源：实施计划

1. 调整冲突委派脚本。
   - 新增实时 base ref 查询。
   - marker 和委派正文改用实时 base SHA。
   - 二次版本校验忽略 base SHA，只保护 base ref 与 head。
   - 移除 Codex 对 base SHA 的停止/推送前强校验。
2. 更新自检。
   - 历史 base SHA 与实时 tip 不同仍委派。
   - base 前进允许继续，head/base ref 变化停止。
   - marker 按实时 base tip 去重。
3. 更新可执行规范并验证全部 delegation 脚本、YAML、Ruff、Pyright、
   compileall 和 diff。
4. 仅在独立修复分支提交并创建 master PR，不操作 PR #58。
