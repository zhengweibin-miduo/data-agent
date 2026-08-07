const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { spawnSync } = require("node:child_process");

const OUTCOMES = new Set(["fixed", "no_change", "blocked"]);
const BLOCKED_REPLY_PREFIX = "无法安全完成：";
const FIELD_LIMITS = {
  threadId: 200,
  reason: 500,
  fix: 500,
  testCommand: 300,
  testSummary: 200,
};
const FORBIDDEN_CONTENT = [
  { name: "Codex 触发词", pattern: /@codex/i },
  { name: "字面量 \\\\n", pattern: /\\n/ },
  { name: "pytest 进度", pattern: /\[\s*\d{1,3}%\s*\]/ },
  { name: "warnings summary", pattern: /warnings summary/i },
  { name: "Python traceback", pattern: /traceback \(most recent call last\)/i },
  { name: "site-packages 路径", pattern: /site-packages[\\/]/i },
  { name: "pytest 文档链接", pattern: /docs\.pytest\.org/i },
  { name: "完整日志分隔线", pattern: /[.=]{20,}/ },
];

const THREAD_QUERY = `
query($threadId: ID!, $commentsCursor: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      comments(first: 100, after: $commentsCursor) {
        nodes { body }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}`;

const ADD_REPLY_MUTATION = `
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
    input: { pullRequestReviewThreadId: $threadId, body: $body }
  ) {
    comment { id url }
  }
}`;

const RESOLVE_THREAD_MUTATION = `
mutation($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) {
    thread { id isResolved }
  }
}`;

function parseArgs(argv) {
  const values = {};
  const names = new Map([
    ["--pr-number", "prNumber"],
    ["--thread-id", "threadId"],
    ["--outcome", "outcome"],
    ["--reason", "reason"],
    ["--fix", "fix"],
    ["--commit-sha", "commitSha"],
    ["--test-command", "testCommand"],
    ["--test-summary", "testSummary"],
  ]);

  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const name = names.get(flag);
    if (!name) {
      throw new Error(`未知参数：${flag || "<empty>"}`);
    }
    if (Object.hasOwn(values, name)) {
      throw new Error(`参数重复：${flag}`);
    }
    if (index + 1 >= argv.length) {
      throw new Error(`参数缺少值：${flag}`);
    }
    values[name] = argv[index + 1];
  }
  return values;
}

function singleLine(name, value, { required = false } = {}) {
  if (value === undefined) {
    if (required) {
      throw new Error(`缺少必填字段：${name}`);
    }
    return undefined;
  }
  if (typeof value !== "string") {
    throw new Error(`${name} 必须是字符串`);
  }
  const normalized = value.trim();
  if (!normalized) {
    throw new Error(`${name} 不能为空`);
  }
  if (/[\r\n]/.test(normalized)) {
    throw new Error(`${name} 必须是单行文本`);
  }
  if (normalized.length > FIELD_LIMITS[name]) {
    throw new Error(`${name} 超过 ${FIELD_LIMITS[name]} 字符`);
  }
  for (const forbidden of FORBIDDEN_CONTENT) {
    if (forbidden.pattern.test(normalized)) {
      throw new Error(`${name} 包含禁止内容：${forbidden.name}`);
    }
  }
  return normalized;
}

function rejectUnexpected(input, names) {
  for (const name of names) {
    if (input[name] !== undefined) {
      throw new Error(`${input.outcome} 不允许字段：${name}`);
    }
  }
}

function validateInput(input) {
  const prNumber = input.prNumber === undefined ? undefined : singleLine("prNumber", input.prNumber);
  if (prNumber !== undefined && !/^[1-9][0-9]*$/.test(prNumber)) {
    throw new Error("prNumber 必须是正整数");
  }
  const threadId = singleLine("threadId", input.threadId, { required: true });
  const outcome = singleLine("outcome", input.outcome, { required: true });
  if (!OUTCOMES.has(outcome)) {
    throw new Error(`不支持的 outcome：${outcome}`);
  }
  const reason = singleLine("reason", input.reason, { required: true });

  if (outcome === "fixed") {
    const fix = singleLine("fix", input.fix, { required: true });
    const commitSha = singleLine("commitSha", input.commitSha, { required: true });
    const testCommand = singleLine("testCommand", input.testCommand, { required: true });
    const testSummary = singleLine("testSummary", input.testSummary, { required: true });
    if (!/^[0-9a-f]{40}$/.test(commitSha)) {
      throw new Error("commitSha 必须是 40 位小写十六进制 SHA");
    }
    return { prNumber, threadId, outcome, reason, fix, commitSha, testCommand, testSummary };
  }

  rejectUnexpected(input, ["fix", "commitSha", "testCommand", "testSummary"]);
  return { prNumber, threadId, outcome, reason };
}

function replyMarker(input) {
  const digest = createHash("sha256")
    .update(JSON.stringify(input))
    .digest("hex")
    .slice(0, 20);
  return `<!-- codex-thread-reply:${digest} -->`;
}

function formatReply(input) {
  const marker = replyMarker(input);
  if (input.outcome === "fixed") {
    return [
      `提交 \`${input.commitSha}\` 已修复。`,
      "",
      `- 根因：${input.reason}`,
      `- 修复：${input.fix}`,
      `- 验证：\`${input.testCommand}\` 通过（${input.testSummary}）`,
      "",
      "该 thread 已 resolve。",
      "",
      marker,
    ].join("\n");
  }
  if (input.outcome === "no_change") {
    return [
      `无需修改：${input.reason}。`,
      "",
      "该 thread 已 resolve。",
      "",
      marker,
    ].join("\n");
  }
  return [
    `无法安全完成：${input.reason}。`,
    "",
    "该 thread 保持 unresolved。",
    "",
    marker,
  ].join("\n");
}

function hasBlockedReply(thread) {
  return thread.comments
    .slice(1)
    .some((comment) => comment.body?.trimStart().startsWith(BLOCKED_REPLY_PREFIX));
}

function runGh(args) {
  const result = spawnSync("gh", args, {
    encoding: "utf8",
    windowsHide: true,
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || "").trim();
    throw new Error(`gh ${args[0]} 失败${detail ? `：${detail}` : ""}`);
  }
  return result.stdout;
}

function parseGhJson(output, operation) {
  try {
    return JSON.parse(output);
  } catch (error) {
    throw new Error(`${operation} 返回了无效 JSON：${error.message}`);
  }
}

function assertGraphqlPayload(payload, operation) {
  if (payload.errors?.length) {
    throw new Error(`${operation} 失败：${JSON.stringify(payload.errors)}`);
  }
  return payload;
}

function createGhAdapter(execute = runGh, prNumber) {
  function graphql(query, fields, operation) {
    const args = ["api", "graphql", "-f", `query=${query}`];
    for (const [name, value] of Object.entries(fields)) {
      args.push("-F", `${name}=${value}`);
    }
    const payload = parseGhJson(execute(args), operation);
    return assertGraphqlPayload(payload, operation);
  }

  return {
    getCurrentPr() {
      const prArgs = prNumber === undefined ? [] : [prNumber];
      return parseGhJson(
        execute([
          "pr",
          "view",
          ...prArgs,
          "--json",
          "number,url,state,headRefName,headRefOid",
        ]),
        "读取当前 PR",
      );
    },
    getThread(threadId) {
      const comments = [];
      let commentsCursor;
      let result;
      do {
        const fields = { threadId };
        if (commentsCursor) {
          fields.commentsCursor = commentsCursor;
        }
        const payload = graphql(THREAD_QUERY, fields, "读取 review thread");
        const thread = payload.data?.node;
        if (!thread?.id || typeof thread.isResolved !== "boolean") {
          throw new Error(`找不到 review thread：${threadId}`);
        }
        comments.push(...(thread.comments?.nodes || []));
        result = { id: thread.id, isResolved: thread.isResolved, comments };
        const pageInfo = thread.comments?.pageInfo;
        commentsCursor = pageInfo?.hasNextPage ? pageInfo.endCursor : undefined;
        if (pageInfo?.hasNextPage && !commentsCursor) {
          throw new Error(`review thread 评论分页缺少 cursor：${threadId}`);
        }
      } while (commentsCursor);
      return result;
    },
    addReply(threadId, body) {
      const args = [
        "api",
        "graphql",
        "-f",
        `query=${ADD_REPLY_MUTATION}`,
        "-F",
        `threadId=${threadId}`,
        "-f",
        `body=${body}`,
      ];
      const payload = assertGraphqlPayload(
        parseGhJson(execute(args), "发布 thread 回复"),
        "发布 thread 回复",
      );
      const comment = payload.data?.addPullRequestReviewThreadReply?.comment;
      if (!comment?.id) {
        throw new Error("发布 thread 回复未返回 comment");
      }
      return comment;
    },
    resolveThread(threadId) {
      const payload = graphql(
        RESOLVE_THREAD_MUTATION,
        { threadId },
        "resolve review thread",
      );
      if (payload.data?.resolveReviewThread?.thread?.isResolved !== true) {
        throw new Error(`review thread 未成功 resolve：${threadId}`);
      }
    },
  };
}

function publishReply(rawInput, adapter = createGhAdapter()) {
  const input = validateInput(rawInput);
  const body = formatReply(input);
  const marker = replyMarker(input);
  const thread = adapter.getThread(input.threadId);

  if (thread.isResolved) {
    return { status: "skipped_resolved", threadId: input.threadId };
  }
  if (hasBlockedReply(thread)) {
    return { status: "skipped_blocked", threadId: input.threadId };
  }

  const alreadyPublished = thread.comments.some((comment) =>
    comment.body?.includes(marker),
  );
  if (!alreadyPublished) {
    if (input.outcome === "fixed") {
      const pull = adapter.getCurrentPr();
      if (pull.state !== "OPEN") {
        throw new Error(`当前 PR 状态不是 OPEN：${pull.state || "<unknown>"}`);
      }
      if (pull.headRefOid !== input.commitSha) {
        throw new Error(
          `commitSha 与当前 PR head 不一致：${input.commitSha} != ${pull.headRefOid}`,
        );
      }
    }
    adapter.addReply(input.threadId, body);
  }

  if (input.outcome === "blocked") {
    return {
      status: alreadyPublished ? "already_published" : "published_blocked",
      threadId: input.threadId,
    };
  }

  if (input.outcome === "fixed") {
    const pull = adapter.getCurrentPr();
    if (pull.state !== "OPEN") {
      throw new Error(`当前 PR 状态不是 OPEN：${pull.state || "<unknown>"}`);
    }
    if (pull.headRefOid !== input.commitSha) {
      throw new Error(
        `commitSha 与当前 PR head 不一致：${input.commitSha} != ${pull.headRefOid}`,
      );
    }
  }
  const latestThread = adapter.getThread(input.threadId);
  if (latestThread.isResolved) {
    return { status: "skipped_resolved", threadId: input.threadId };
  }
  if (hasBlockedReply(latestThread)) {
    return { status: "skipped_blocked", threadId: input.threadId };
  }
  adapter.resolveThread(input.threadId);
  return {
    status: alreadyPublished ? "resolved_existing_reply" : "published_and_resolved",
    threadId: input.threadId,
  };
}

function fakeAdapter({ thread, threads, pull, addError, resolveError } = {}) {
  const calls = [];
  let threadReadIndex = 0;
  return {
    calls,
    getThread(threadId) {
      calls.push(["getThread", threadId]);
      if (threads) {
        const nextThread = threads[Math.min(threadReadIndex, threads.length - 1)];
        threadReadIndex += 1;
        return nextThread;
      }
      return thread || { id: threadId, isResolved: false, comments: [] };
    },
    getCurrentPr() {
      calls.push(["getCurrentPr"]);
      return pull || { state: "OPEN", headRefOid: "a".repeat(40) };
    },
    addReply(threadId, body) {
      calls.push(["addReply", threadId, body]);
      if (addError) {
        throw addError;
      }
      return { id: "COMMENT_1", url: "https://github.test/comment/1" };
    },
    resolveThread(threadId) {
      calls.push(["resolveThread", threadId]);
      if (resolveError) {
        throw resolveError;
      }
    },
  };
}

function fixedInput(overrides = {}) {
  return {
    threadId: "PRRT_thread",
    outcome: "fixed",
    reason: "扫描游标保存了旧进度",
    fix: "保存本工作单元的新游标",
    commitSha: "a".repeat(40),
    testCommand: "uv run pytest tests/unit/example.py -q",
    testSummary: "12 passed",
    ...overrides,
  };
}

function selfTest() {
  const normalized = validateInput(fixedInput());
  const body = formatReply(normalized);
  assert.match(body, /^提交 `a{40}` 已修复。/);
  assert.match(body, /\n\n- 根因：扫描游标保存了旧进度\n/);
  assert.match(body, /- 验证：`uv run pytest tests\/unit\/example.py -q` 通过（12 passed）/);
  assert.doesNotMatch(body, /\\n/);
  assert.match(body, /<!-- codex-thread-reply:[0-9a-f]{20} -->$/);

  assert.throws(
    () => validateInput(fixedInput({ commitSha: "" })),
    /commitSha 不能为空/,
  );
  assert.throws(
    () => validateInput(fixedInput({ commitSha: "abc123" })),
    /40 位小写十六进制 SHA/,
  );
  assert.throws(
    () => validateInput(fixedInput({ reason: "第一行\\n第二行" })),
    /字面量/,
  );
  assert.throws(
    () => validateInput(fixedInput({ reason: "第一行\n第二行" })),
    /单行文本/,
  );
  assert.throws(
    () => validateInput(fixedInput({ testSummary: "warnings summary" })),
    /warnings summary/,
  );
  assert.throws(
    () => validateInput(fixedInput({ testSummary: "....... [ 43%]" })),
    /pytest 进度/,
  );
  for (const field of ["reason", "fix", "testSummary"]) {
    assert.throws(
      () => validateInput(fixedInput({ [field]: "不得提及 @CoDeX" })),
      /Codex 触发词/,
    );
  }
  assert.throws(
    () => validateInput({ threadId: "T", outcome: "no_change", reason: "无需修改", testSummary: "1 passed" }),
    /no_change 不允许字段/,
  );

  const fixed = fakeAdapter();
  const fixedResult = publishReply(fixedInput(), fixed);
  assert.equal(fixedResult.status, "published_and_resolved");
  assert.deepEqual(
    fixed.calls.map((call) => call[0]),
    ["getThread", "getCurrentPr", "addReply", "getCurrentPr", "getThread", "resolveThread"],
  );

  const noChange = fakeAdapter();
  const noChangeResult = publishReply(
    { threadId: "PRRT_no_change", outcome: "no_change", reason: "现有实现已覆盖该边界" },
    noChange,
  );
  assert.equal(noChangeResult.status, "published_and_resolved");
  assert.deepEqual(
    noChange.calls.map((call) => call[0]),
    ["getThread", "addReply", "getThread", "resolveThread"],
  );

  const blocked = fakeAdapter();
  const blockedResult = publishReply(
    { threadId: "PRRT_blocked", outcome: "blocked", reason: "缺少外部依赖" },
    blocked,
  );
  assert.equal(blockedResult.status, "published_blocked");
  assert.deepEqual(
    blocked.calls.map((call) => call[0]),
    ["getThread", "addReply"],
  );

  const existingBlockedComment = { body: "无法安全完成：需要架构决策。" };
  const blockedInputs = [
    fixedInput({ threadId: "PRRT_existing_blocked_fixed" }),
    {
      threadId: "PRRT_existing_blocked_no_change",
      outcome: "no_change",
      reason: "现有实现已覆盖该边界",
    },
    {
      threadId: "PRRT_existing_blocked_again",
      outcome: "blocked",
      reason: "仍缺少外部依赖",
    },
  ];
  for (const blockedInput of blockedInputs) {
    const existingBlocked = fakeAdapter({
      thread: {
        id: blockedInput.threadId,
        isResolved: false,
        comments: [{ body: "review finding" }, existingBlockedComment],
      },
    });
    assert.equal(publishReply(blockedInput, existingBlocked).status, "skipped_blocked");
    assert.equal(
      existingBlocked.calls.some(([operation]) =>
        operation === "addReply" || operation === "resolveThread"),
      false,
      `${blockedInput.outcome} must not mutate a thread with an existing blocked reply`,
    );
  }

  const lateBlockedThread = {
    id: "PRRT_late_blocked",
    isResolved: false,
    comments: [{ body: "review finding" }, existingBlockedComment],
  };
  for (const lateInput of [
    fixedInput({ threadId: "PRRT_late_blocked" }),
    {
      threadId: "PRRT_late_blocked",
      outcome: "no_change",
      reason: "现有实现已覆盖该边界",
    },
  ]) {
    const lateBlocked = fakeAdapter({
      threads: [
        { id: lateInput.threadId, isResolved: false, comments: [] },
        lateBlockedThread,
      ],
    });
    assert.equal(publishReply(lateInput, lateBlocked).status, "skipped_blocked");
    assert.equal(
      lateBlocked.calls.some(([operation]) => operation === "resolveThread"),
      false,
      `${lateInput.outcome} must keep a concurrently blocked thread unresolved`,
    );
  }

  const resolved = fakeAdapter({
    thread: { id: "PRRT_resolved", isResolved: true, comments: [] },
  });
  assert.equal(
    publishReply(fixedInput({ threadId: "PRRT_resolved" }), resolved).status,
    "skipped_resolved",
  );
  assert.deepEqual(resolved.calls.map((call) => call[0]), ["getThread"]);

  const existingInput = validateInput(fixedInput({ threadId: "PRRT_existing" }));
  const existing = fakeAdapter({
    thread: {
      id: "PRRT_existing",
      isResolved: false,
      comments: [{ body: formatReply(existingInput) }],
    },
  });
  assert.equal(publishReply(existingInput, existing).status, "resolved_existing_reply");
  assert.deepEqual(
    existing.calls.map((call) => call[0]),
    ["getThread", "getCurrentPr", "getThread", "resolveThread"],
  );

  const failedReply = fakeAdapter({ addError: new Error("reply failed") });
  assert.throws(() => publishReply(fixedInput(), failedReply), /reply failed/);
  assert.equal(
    failedReply.calls.some((call) => call[0] === "resolveThread"),
    false,
  );

  const failedResolve = fakeAdapter({ resolveError: new Error("resolve failed") });
  assert.throws(() => publishReply(fixedInput(), failedResolve), /resolve failed/);
  assert.deepEqual(
    failedResolve.calls.map((call) => call[0]),
    ["getThread", "getCurrentPr", "addReply", "getCurrentPr", "getThread", "resolveThread"],
  );

  const changedHeadAfterReply = fakeAdapter({
    thread: {
      id: "PRRT_existing_changed_head",
      isResolved: false,
      comments: [{ body: formatReply(validateInput(fixedInput({ threadId: "PRRT_existing_changed_head" }))) }],
    },
    pull: { state: "OPEN", headRefOid: "b".repeat(40) },
  });
  assert.throws(
    () => publishReply(fixedInput({ threadId: "PRRT_existing_changed_head" }), changedHeadAfterReply),
    /commitSha 与当前 PR head 不一致/,
  );
  assert.equal(
    changedHeadAfterReply.calls.some((call) => call[0] === "resolveThread"),
    false,
  );

  const changedHead = fakeAdapter({
    pull: { state: "OPEN", headRefOid: "b".repeat(40) },
  });
  assert.throws(
    () => publishReply(fixedInput(), changedHead),
    /commitSha 与当前 PR head 不一致/,
  );
  assert.equal(
    changedHead.calls.some((call) => call[0] === "addReply"),
    false,
  );

  assert.deepEqual(parseArgs([
    "--pr-number",
    "76",
    "--thread-id",
    "PRRT_1",
    "--outcome",
    "blocked",
    "--reason",
    "缺少依赖",
  ]), {
    prNumber: "76",
    threadId: "PRRT_1",
    outcome: "blocked",
    reason: "缺少依赖",
  });
  assert.throws(() => validateInput({
    prNumber: "0",
    threadId: "T",
    outcome: "blocked",
    reason: "缺少依赖",
  }), /prNumber 必须是正整数/);
  const prViewCalls = [];
  createGhAdapter((args) => {
    prViewCalls.push(args);
    return JSON.stringify({
      number: 76,
      url: "https://github.test/pull/76",
      state: "OPEN",
      headRefName: "refactor/separate-frontend-backend-20260801",
      headRefOid: "a".repeat(40),
    });
  }, "76").getCurrentPr();
  assert.deepEqual(prViewCalls[0].slice(0, 3), ["pr", "view", "76"]);
  assert.throws(() => parseArgs(["--unknown", "value"]), /未知参数/);

  const graphqlCalls = [];
  const graphqlPages = [
    {
      data: {
        node: {
          id: "PRRT_paged",
          isResolved: false,
          comments: {
            nodes: [
              { body: "review finding" },
              ...Array.from({ length: 99 }, () => ({ body: "follow-up" })),
            ],
            pageInfo: { hasNextPage: true, endCursor: "CURSOR_1" },
          },
        },
      },
    },
    {
      data: {
        node: {
          id: "PRRT_paged",
          isResolved: false,
          comments: {
            nodes: [{ body: "无法安全完成：第 101 条回复中的阻塞原因。" }],
            pageInfo: { hasNextPage: false, endCursor: null },
          },
        },
      },
    },
  ];
  const pagedAdapter = createGhAdapter((args) => {
    graphqlCalls.push(args);
    return JSON.stringify(graphqlPages.shift());
  });
  assert.equal(
    publishReply(
      { threadId: "PRRT_paged", outcome: "no_change", reason: "现有实现已覆盖该边界" },
      pagedAdapter,
    ).status,
    "skipped_blocked",
  );
  assert.equal(
    graphqlCalls[1].includes("commentsCursor=CURSOR_1"),
    true,
  );
}

function main(argv) {
  const input = parseArgs(argv);
  const result = publishReply(input, createGhAdapter(runGh, input.prNumber));
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (require.main === module) {
  try {
    if (process.argv.length === 3 && process.argv[2] === "--self-test") {
      selfTest();
    } else {
      main(process.argv.slice(2));
    }
  } catch (error) {
    console.error(`错误：${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = {
  createGhAdapter,
  formatReply,
  parseArgs,
  publishReply,
  replyMarker,
  selfTest,
  validateInput,
};
