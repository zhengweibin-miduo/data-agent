# 清理记忆检索排序与索引效率问题

## Goal

消除读路径对读路径排序的反馈，并把精确命中加成的量纲意图从"看起来像重复计分"
变成被测试锁定的显式契约。

## Background

本任务基于 `fix/memory-correctness-defects-20260726`，因为改动落在该分支已经改过的
`memory/mysql/repository.py` 上，独立开分支只会制造冲突。

- `record_access` 的 UPDATE 触发 `agent_memory.updated_at` 的 `onupdate`，
  而 `find_exact_query` 与 `find_compatible_scopes` 都按 `updated_at desc` 取候选。
  于是被检索命中的记忆会把自己顶到后续检索的前面，形成读路径改变读路径排序的反馈，
  与"`updated_at` 反映内容何时被更新"的语义也相悖。访问热度本已有 `access_count`
  与 `last_accessed_at` 表达。
- RRF 中同一份精确命中列表既作为排名信号传入，又通过 `exact_uids` 传入，加成 1.0
  相对单个 RRF 项（约 0.016）是压倒性的，读起来像重复计分。

## Findings

审查把 RRF 一项记为"疑似重复计分，若为刻意设计应注明量纲意图"。核对实现后确认
**这是刻意设计，不是缺陷**：1.0 的加成决定精确命中相对其它信号的位置，排名项决定
精确命中彼此之间的顺序。去掉排名项会让所有精确命中同分，退化为按 UID 字符串排序，
丢失基线查询给出的相关性顺序。因此本任务不改行为，只把意图写进 docstring 并用测试
锁定，包括"去掉排名项即退化"的对照用例。

## Out of Scope

`find_exact_query` 对 Text 列做 `memory_text == query` 全等比较，无法走索引，
数据量增长后会成为检索延迟主项。正确修法是为投影文本增加哈希列并建索引，用哈希等值
替代全等比较——这需要改数据库结构。项目的 `docs/docker/mysql/` 脚本明确是"空白环境
bootstrap，不是升级迁移"，已有卷不会重放；在没有迁移机制的前提下加列会让已有环境
因未知列而直接报错。因此本任务不做该改动，把它作为"需要先确定迁移方案"的独立事项
记录在 spec 中，避免被遗忘。

## Requirements

- 访问统计不得推进 `updated_at`，但必须继续递增 `access_count` 并更新
  `last_accessed_at`。
- 精确命中加成与排名项的分工必须写入 `ranking.py` 的 docstring，并有测试锁定
  "精确优先"与"精确命中内部顺序来自排名项"两条性质。
- 不改变现有检索结果的排序行为。

## Acceptance Criteria

- [x] `record_access` 的 UPDATE 显式自赋值 `updated_at`，渲染 SQL 中不出现
      `updated_at=now()`。
- [x] 有测试断言精确命中整体优先，且分差远大于单个 RRF 项。
- [x] 有测试断言精确命中之间的顺序来自排名项，并有去掉排名项后退化的对照用例。
- [x] README 记录的基础质量门禁全部通过。
