# 修复冲突委派 base SHA 来源：技术设计

## 根因

`pull.base.sha` 是 PR 对象中的历史 base 快照，不能作为
`origin/<base.ref>` 当前分支头的强一致预期。旧委派提示要求二者相等，
导致旧 PR 永远在 merge 前停止。

## 修复

1. `pulls.get` 继续提供 PR ref/head、mergeability 和历史上下文。
2. 通过 `git.getRef(ref="heads/<base.ref>")` 获取实时 base tip。
3. 委派前二次读取 PR，仅保护 head ref/SHA、base ref、draft、同仓和冲突状态。
4. marker 使用 PR number、实时 base SHA 和 head SHA。
5. 委派提示把实时 base SHA 标记为 observed 信息；Codex 始终 fetch 并 merge
   最新 `origin/<base.ref>`，推送前只严格保护远端 head。

## 安全边界

- head 变化可能覆盖协作者工作，必须停止。
- base 前进是正常输入，不得停止；合并最新 base 即可。
- base ref 变化会改变 PR 目标，必须停止。
- 不向 PR #58 写入任何提交；本修复经独立 master PR 交付。
