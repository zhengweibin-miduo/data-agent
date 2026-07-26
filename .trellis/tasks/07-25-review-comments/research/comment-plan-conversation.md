# Conversation 核心流程注释定位

以下定位基于当前 worktree 的 `prd.md`、`design.md`、`implement.md`、根目录 `code_review.md` 与 `.trellis/spec/backend/quality-guidelines.md`。目标是补充 rationale/invariant/事务与生命周期约束，不逐行翻译可见代码。

## 高价值插入点

1. **单活动 turn 门禁与用户消息幂等**
   - 位置：`src/data_agent/conversation/repository.py:187-259`，`ConversationRepository.start_turn`。
   - 原因/不变量：`get(..., for_update=True)` 在事务内锁定会话；`active_turn_uid` 只允许同一 `turn_uid` 重试，其他轮次返回 `conversation_busy`；已存在用户消息必须内容完全一致，否则 `idempotency_conflict`。这三者共同保证一个会话只有一个在途 turn，且网络重试不重复写消息。
   - 建议中文注释："先锁定会话再检查 active_turn_uid，避免并发请求同时通过门禁；同一 turn 仅允许相同内容幂等重试，内容变化必须拒绝。"
   - 证据：集成测试 `tests/integration/persistence/test_conversation_repository.py:33-44,57-58` 验证重复 start 返回同一 UID；质量规范要求 one active turn、turn idempotency（`quality-guidelines.md:136-139`）。

2. **助手消息、outbox 与释放门禁的原子顺序**
   - 位置：`repository.py:261-362`，`complete_turn`。
   - 原因/不变量：先确认用户消息存在及当前 active turn，再插入助手消息；随后用唯一键语义写 `conversation_memory_outbox`，最后仅在 `active_turn_uid == turn_uid` 时清空门禁。整个调用方 Session 提交前，助手消息、提炼任务和 turn 完成状态必须同成败。
   - 建议中文注释："助手消息与 outbox 必须和清除 active_turn_uid 处于同一事务；任一步失败都保留可重试的在途 turn，避免出现无助手消息却已放行下一轮。"
   - 证据：`repository.py:336-356`；测试 `test_conversation_repository.py:45-56,74-90` 验证助手幂等与 outbox 只有一条。

3. **稳定 keyset 历史游标与展示顺序**
   - 位置：`repository.py:151-185`，`history`；`service.py:52-75`，`history` 的 404 转换。
   - 原因/不变量：查询按自增 `agent_message.id DESC` 并取 `limit+1`，`next_before` 使用最后一条可见记录的 ID；返回前反转为正序。游标不是 offset，插入新消息不会导致已翻页记录漂移；同时 user/conversation 条件防止跨租户读取。
   - 建议中文注释："使用递减主键作为 keyset 游标而非 offset；多取一条只用于判断下一页，返回给调用方前恢复时间线正序。"
   - 证据：`repository.py:163-183`；集成测试 `test_conversation_repository.py:60-89` 验证稳定消息顺序与其他用户不可见；规范 `quality-guidelines.md:136-139`。

4. **`list` Docstring 与实际排序契约不一致（需修复）**
   - 位置：`repository.py:79-113`，`ConversationRepository.list`。
   - 事实：Docstring 写作“按更新时间与主键稳定读取”，实现只有 `.order_by(agent_conversation.c.id.desc())`，并未按 `updated_at` 排序。
   - 建议：将 Docstring 改为“按会话自增主键倒序执行稳定 keyset 分页读取用户会话”；若产品真正要求更新时间排序，则需另行修改 SQL/游标（超出本注释定位，不应仅靠注释掩盖）。
   - 证据：`repository.py:86,93-96`；`before` 也比较 `id`（`repository.py:87-90`），说明当前游标契约是 ID。

5. **摘要游标后的有界消息读取**
   - 位置：`repository.py:364-392`，`context_messages`；`service.py:145-193`，`_context`。
   - 原因/不变量：`after_id` 排除已进入摘要的消息，`through_id` 在 outbox 提炼时截断到助手消息；查询倒序限量后反转，保证上下文时间序列。服务层再按字符预算从最新消息向前截断，避免上下文超过上限。
   - 建议中文注释："摘要游标和 through_id 定义可见消息区间；先按 ID 倒序取最新窗口再反转，字符预算从最新消息开始保留，确保新上下文优先且顺序不乱。"
   - 证据：`repository.py:377-392`、`extraction.py:455-460`、`service.py:175-183`；规范要求 summary cursor monotonicity。

6. **outbox 按会话顺序领取、租约与 skip-locked**
   - 位置：`repository.py:394-478`，`claim_extractions`。
   - 原因/不变量：相关子查询只选每个会话最早 outbox ID，防止同会话后续轮次并行提炼；`available_at`/lease 到期条件支持恢复；`with_for_update(skip_locked=True)` 避免 worker 互相阻塞；领取后写 lease token，并在同一短事务加载租户隔离且有界消息。
   - 建议中文注释："每个会话一次只领取最早未完成轮次，保证摘要按 turn 顺序推进；租约令牌使 worker 崩溃后可重领，skip_locked 让其他会话继续处理。"
   - 证据：`repository.py:402-429,433-461`；集成测试 `test_conversation_repository.py:102-155` 验证同会话首次只领取 `turn-1`、完成后才领取 `turn-2`。

7. **lease token 校验与摘要游标单调推进**
   - 位置：`repository.py:480-518`，`finish_extraction`。
   - 原因/不变量：只有仍持有相同 lease token 的 worker 能确认任务；摘要更新要求旧游标为空或小于本批 `through`，防止迟到 worker 覆盖较新摘要；随后按 token 删除 outbox。返回 False 表示租约已失效，调用方应走失败/重试路径。
   - 建议中文注释："完成阶段再次校验 lease token，避免过期 worker 写入；游标只允许前进，outbox 删除与摘要更新同事务提交。"
   - 证据：`repository.py:486-517`；`extraction.py:216-225` 在 `finished=False` 时抛错并进入退避；质量规范要求 outbox replay 与摘要游标单调性。

8. **提炼失败的 lease 释放与指数退避**
   - 位置：`repository.py:520-544`，`retry_extraction`；`extraction.py:186-243`，`_process_claim`。
   - 原因/不变量：模型/校验/持久化任一步异常都清除 lease、递增 attempts、设置有界指数 `available_at`，让任务可恢复且避免热循环；成功路径先 upsert 候选，再完成摘要，保证记忆写入与摘要确认同事务。
   - 建议中文注释："异常不删除 outbox；清除租约并按 attempts 退避，交给后续 worker 重试。成功时候选记忆 upsert 与 finish_extraction 同一 Session，避免摘要已前进但记忆未落库。"
   - 证据：`extraction.py:216-231`、`repository.py:526-543`；规范要求持久化重试、outbox replay。

9. **证据约束与模糊确认拒绝**
   - 位置：`extraction.py:59-152`，`_validated_candidates`。
   - 原因/不变量：候选 evidence UID 必须属于本批用户消息且角色为 user；`supporting_user_quote` 必须精确包含并涵盖 value；若依据助手结论，还需助手 UID/原文及其后的用户确认，且拒绝 `yes/可以/好的` 等模糊确认。这样可阻止模型猜测或跨消息伪造长期记忆。
   - 建议中文注释："模型输出不具备权威性，必须回查本批原始消息和角色/顺序；只有可定位的用户原文（以及对助手结论的明确后续确认）才允许写入记忆。"
   - 证据：`extraction.py:72-105`；系统提示 `extraction.py:52-56` 明确要求精确 quote；规范要求 exact quote evidence、ambiguous-confirmation rejection。

10. **用户删除顺序与长期记忆 tombstone**
    - 位置：`service.py:135-143`，`delete_user_data`；`repository.py:143-149`，`delete_user_conversations`。
    - 原因/不变量：同一数据库 Session 中先硬删除用户会话（含消息/outbox 外键链），再 tombstone 用户长期记忆；事务提交前两者不可见，失败可整体回滚，避免记忆保留而对话证据已消失或反之。
    - 建议中文注释："删除顺序是先清除会话及其 outbox，再 tombstone 用户记忆；两步共享一个事务，确保用户数据删除请求不会留下可检索的孤立记忆。"
    - 证据：`service.py:140-143`；规范明确要求 delete-before-purge ordering（`quality-guidelines.md:136-139`）。

## 不建议添加注释的机械 CRUD

- `ConversationService.create/list/delete` 的单层 Session 转发（`service.py:32-50,77-95`），代码和函数名已清楚表达意图。
- `ConversationRepository.create` 的 UUID/INSERT/回读和 `_inserted_id` 主键读取（`repository.py:58-77,42-49`），无额外业务顺序约束。
- `ConversationRepository.get` 的 user+uid 条件与可选 `FOR UPDATE`（`repository.py:115-131`）可由已有 Docstring 覆盖；除非在调用点解释为何必须锁。
- `_message`、简单模型转换及 API 层参数转发；逐字段翻译会制造噪音。
- 测试中的数据库建表、清理和 `check_equal` 准备步骤（`tests/integration/persistence/test_conversation_repository.py:20-32,91-99`），不属于产品流程 rationale。
