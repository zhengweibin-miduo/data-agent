# Interface 备选设计

## 方案 A：最小 interface

外部只提供：

```python
await MetadataValueRefresh.run_next_unit(claim, budget) -> WorkResult
```

状态机、频次、Top-N、差集、租约和恢复全部隐藏。优点是 interface 最深，dispatcher
调用最简单；缺点是内部实现复杂，必须保留可测试的私有 seams。

## 方案 B：阶段和 adapter 全显式

`RefreshController.run(claim, budget, adapters)` 暴露 phase 和各种 cursor，并注入
scanner、frequency store、candidate selector、published set、index publisher 和
lease store。扩展性最好，但 caller 需要理解过多状态，容易把 module 变浅。

## 方案 C：默认 caller 优先

dispatcher 只观察 `COMPLETE / CONTINUE / DEFERRED`。CDC 不调用 Elasticsearch，
只在原 DW 事务内更新频次与 desired state。优点是默认路径简单、事务边界清楚；
缺点是高级调试需要独立的只读状态投影。

## 推荐

采用 A+C 的混合：

- 外部 seam 保持一个 `run_next_unit()` 入口和一个只读状态投影。
- 内部使用显式 `Phase`、opaque cursor、MySQL adapter 和 Elasticsearch adapter。
- MySQL 是 local-substitutable dependency，用真实集成与 fake 验证。
- Elasticsearch 是外部 seam，生产 adapter 使用有界 bulk，fake 注入中断；
  真实集成验证最终文档集合。
- CDC 只在原 MySQL 事务中维护精确频次和状态，不跨 Elasticsearch I/O。

该组合最大化 interface 的 depth 和调用方 leverage，同时把变化集中在
`metadata_indexing` module，避免阶段细节扩散到 worker 和 data-sync caller。
