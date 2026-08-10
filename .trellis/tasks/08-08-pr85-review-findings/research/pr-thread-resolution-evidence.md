# PR #85 unresolved thread resolution evidence

Read-only refresh on 2026-08-08 returned 227 review threads, including the same
five unresolved threads. No GitHub reply or resolution mutation was performed.

## `PRRT_kwDOTXnY3c6XUMt9` — decisive EXPLAIN generation coordination

- Code: `backend/src/query/application/service.py:360-461` validates a draft,
  acquires the draft's final `target_tables` READ set, then rechecks authority,
  readiness, and EXPLAIN inside that critical section. Repair runs after the
  context exits and therefore reacquires the repaired target set.
- Tests: `backend/tests/unit/query/test_service.py:1282` covers changed-target
  reacquisition; `backend/tests/integration/query/test_generation_coordination.py:184`
  attempts a WRITE owner at every decisive EXPLAIN boundary.
- Proposed reply: 已将每次决定 repair/终态的 authority、readiness 和 EXPLAIN
  放入当前草稿最终目标表的 generation READ 临界区；repair 在释放旧锁后执行，
  若目标表改变会按新集合重新领取。已补 changed-target 单测和真实 MySQL
  READ/WRITE 排斥集成测试。当前本机 MySQL 端口不可用，集成用例需在 CI/可用
  MySQL 8.4 环境复验。

## `PRRT_kwDOTXnY3c6XW7Rp` — natural range after clarification

- Code: `backend/src/query/domain.py:673-764` derives a trusted half-open range;
  `backend/src/query/application/service.py:472-520` binds it only after the
  authoritative time column is known; `backend/src/query/domain.py:1006-1032`
  and `1506-1519` require both predicates and exact parameters.
- Tests: `backend/tests/unit/query/test_service.py:194-288` covers the full
  original-question, time-column clarification, short-answer flow;
  `backend/tests/unit/query/test_validation.py:124-210` covers executable bounds.
- Proposed reply: 自然年份/月份/最近 N 天不再要求模型伪造逐字 `time_filter`。
  时间字段澄清完成后，服务端用已验证 IANA 时区、注入时钟和权威字段类型派生
  `[start, end)`，SQL 门禁强制同时存在 `>= start` 与 `< end`。已补完整两轮流测试。

## `PRRT_kwDOTXnY3c6XXNaf` — durable clarification chain

- Code: `backend/src/conversation/repository.py:663-743` performs a keyset-paged
  authoritative message scan with independent fail-closed budgets;
  `backend/src/query/application/service.py:143-159` consumes that port rather
  than the ordinary Conversation context window.
- Tests: `backend/tests/unit/conversation/test_turn_lease.py:152-205` covers a
  chain beyond the 20-message/32,768-character ordinary budget and independent
  overflow; Query propagation is covered in `backend/tests/unit/query/test_service.py`.
- Proposed reply: Query 澄清链已改为从 MySQL 权威消息独立分页读取，不再依赖
  普通 Conversation 的 20 条/32,768 字符窗口；链自身使用 100 条/262,144 字符
  预算，超限稳定 fail closed。已补长链与独立预算回归。

## `PRRT_kwDOTXnY3c6Xcmf9` — turn claim fencing

- Code: `backend/src/conversation/repository.py:262-291` creates a fresh token
  per claim/reclaim; `429-600` compares `(turn_uid, claim_token)` for complete,
  abandon, and renew. The token is propagated through Conversation/Query/Chat
  application ports but omitted from logs and user events.
- Tests: `backend/tests/unit/conversation/test_turn_lease.py:365-432` and
  `backend/tests/integration/persistence/test_conversation_repository.py:395-450`
  cover reclaim fencing for all owner mutations.
- Proposed reply: 首次领取和同 UID 到期重领都会生成新的 claim token；renew、
  complete、abandon 全部使用 `(turn_uid, claim_token)` CAS，旧执行代无法影响新
  owner。token 仅在内部端口传播，不进入日志、审计或响应。已补单元与真实事务测试。

## `PRRT_kwDOTXnY3c6XctZA` — relative-time executable boundaries

- Code: `backend/src/query/domain.py:695-764` supports explicit year/month,
  今年、去年、本月、上月、最近 N 天. Recent N means N user-local calendar
  days including today. DATE stays local dates, DATETIME uses local wall time,
  and TIMESTAMP converts local midnight bounds to UTC.
- Tests: `backend/tests/unit/query/test_validation.py:43-210` covers leap years,
  month/year rollover, DST, DATE/DATETIME/TIMESTAMP, and mandatory half-open
  predicates; HTTP validation is in `backend/tests/unit/query/test_api.py`.
- Proposed reply: 相对时间由确定性代码转换为可执行半开边界，并保留原始
  `time_quote` 作为证据。`最近 N 天` 定义为包含今天的 N 个用户当地自然日；
  DATE/DATETIME/TIMESTAMP 分别按本地日期、本地墙钟、UTC instant 生成参数。
  已补闰年、跨月年、DST 与三种字段类型回归。

## Verification note

The local non-integration, lint, type-check, compile, build, configuration,
Compose, lockfile, and diff gates passed during independent review. After the
empty MySQL 8.4 environment was bootstrapped from `docs/docker/mysql/`, the
live generation-lock, decisive-EXPLAIN, and Conversation claim-fencing suite
also passed: `9 passed in 9.90s`.
