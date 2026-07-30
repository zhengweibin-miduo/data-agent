const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");

function marker(reviewId, headSha) {
  return `<!-- codex-review-loop:${reviewId}:${headSha} -->`;
}

function manualMarker(headSha, threads) {
  const threadHash = createHash("sha256")
    .update(threads.map((thread) => thread.id).sort().join("\n"))
    .digest("hex")
    .slice(0, 16);
  return `<!-- codex-review-manual:${headSha}:${threadHash} -->`;
}

function delegationBodyWithMarker(markerText, headSha, headRef, threads) {
  const threadList = threads
    .map((thread) => `- ${thread.url || thread.id} (${thread.id})`)
    .join("\n");
  return `${markerText}
@codex fix

## 任务

修复此 PR 中所有有效且尚未解决的审查问题。

PR branch: \`${headRef}\`
Expected head: \`${headSha}\`
Threads:
${threadList}

直接执行修复，不要只做分析、审查或等待人工确认。

## 开始前检查

读取根目录 \`AGENTS.md\`、\`code_review.md\` 和 \`.agents/skills/git-pr-rules/SKILL.md\`，确认 \`origin\` 存在并 fetch，再读取 PR diff 和每条 thread 的代码上下文并检查工作树是否存在无关修改。Expected head 是本任务的分析基线，不是要求远端永远不变的锁：如果远端 head 已变化，按项目 Git/PR Skill 的远端干预判定矩阵处理。远端只是 Expected head 的线性后继，且本地只有已核验的本任务改动或未发布提交时，可以安全同步、重新验证并继续普通推送；历史关系不明、发生冲突、存在未识别或重叠改动、验证失败、需要改写已发布历史或 force-push 时才停止。\`gh auth status\` 失败但 Git 推送凭据仍可用时，可以继续修复和推送，只需说明无法自动回复或 resolve thread。

## 处理规则

逐条判断并处理：
- 需要修复：做最小修复并验证。
- 不需要修复：在原 thread 说明依据并 resolve。
- 无法安全完成：在原 thread 说明阻塞原因，并保持 unresolved。

遵守仓库规范，不做无关重构，不覆盖已有用户修改。根据实际改动执行必要的测试、lint、类型检查和 \`git diff --check\`。

## 提交与回复

验证全部通过后，复核 diff，确保没有无关修改。所有修复只创建一个中文提交，然后执行 \`git push origin HEAD:${headRef}\`；禁止 force-push，不创建新 PR，也不发起新的 Codex 审查。推送后使用 \`git ls-remote\` 核验远端实际提交 SHA。

仅对已完成修复或确认无需修改的 thread 回复并 resolve；无法安全完成的 thread 说明阻塞原因并保持 unresolved。修复、验证或推送任一步骤失败时，不得声称已修复或 resolve。

已修复的 thread 使用以下结构：

提交 \`<实际提交 SHA>\` 已修复。

- 根因：<问题原因>
- 修复：<关键修改>
- 验证：\`<测试命令>\` 通过（<简要结果>）

该 thread 已 resolve。

确认无需修改的 thread 使用以下结构，不得声称已有提交修复或附加测试结果：

无需修改：<具体判断依据>。

该 thread 已 resolve。

无法安全完成时使用以下结构：

无法安全完成：<具体阻塞原因>。

该 thread 保持 unresolved。

## 输出要求

每条审查意见必须在原 thread 下分别回复，并根据处理结果 resolve 或保持 unresolved。Codex 完成任务后可以使用默认格式发布任务总结，但所有 GitHub 回复、提交信息和任务总结必须使用简体中文。

代码标识符、文件路径、命令和原始错误信息可以保留英文。代码链接必须指向推送后的实际提交 SHA，不得引用任务开始时的 Expected head。禁止粘贴测试进度条、warnings summary、堆栈或完整命令输出，只保留测试命令、通过数量及与本次修改直接相关的异常。`;
}

function delegationBody(reviewId, headSha, headRef, threads) {
  return delegationBodyWithMarker(marker(reviewId, headSha), headSha, headRef, threads);
}

function manualDelegationBody(headSha, headRef, threads) {
  return delegationBodyWithMarker(manualMarker(headSha, threads), headSha, headRef, threads);
}

async function unresolvedThreads(github, owner, repo, pullNumber, reviewId, reviewer) {
  const matches = [];
  let cursor = null;
  do {
    const result = await github.graphql(
      `query($owner:String!,$repo:String!,$pullNumber:Int!,$cursor:String) {
        repository(owner:$owner,name:$repo) {
          pullRequest(number:$pullNumber) {
            reviewThreads(first:100,after:$cursor) {
              nodes {
                id
                isResolved
                comments(first:1) {
                  nodes {
                    url
                    author { login }
                    pullRequestReview { databaseId }
                  }
                }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
      }`,
      { owner, repo, pullNumber, cursor },
    );
    const threads = result.repository.pullRequest.reviewThreads;
    for (const thread of threads.nodes) {
      const firstComment = thread.comments.nodes[0];
      const belongsToReview =
        firstComment?.pullRequestReview?.databaseId === reviewId &&
        firstComment.author?.login?.replace(/\[bot\]$/, "") ===
          reviewer.replace(/\[bot\]$/, "");
      if (!thread.isResolved && belongsToReview) {
        matches.push({ id: thread.id, url: firstComment.url });
      }
    }
    cursor = threads.pageInfo.hasNextPage ? threads.pageInfo.endCursor : null;
  } while (cursor);
  return matches;
}

function allowedLogins(value, fallback) {
  return new Set((value || fallback).split(",").map((login) => login.trim()));
}

function assertManualPullEligible(pull, owner, repo, allowedAuthors) {
  if (pull.draft) {
    throw new Error("Draft pull requests cannot be delegated.");
  }
  if (pull.head.repo.full_name !== `${owner}/${repo}`) {
    throw new Error("Fork pull requests cannot be delegated.");
  }
  if (!allowedAuthors.has(pull.user.login)) {
    throw new Error(`Pull request author ${pull.user.login} is not allowed.`);
  }
}

function normalizedLogin(login) {
  return login?.replace(/\[bot\]$/, "");
}

function isBlockedReply(comment) {
  return comment.body?.trimStart().startsWith("无法安全完成：");
}

async function hasBlockedReply(github, thread) {
  if (thread.comments.nodes.slice(1).some(isBlockedReply)) {
    return true;
  }
  let pageInfo = thread.comments.pageInfo;
  while (pageInfo.hasNextPage) {
    const result = await github.graphql(
      `query($threadId:ID!,$cursor:String) {
        node(id:$threadId) {
          ... on PullRequestReviewThread {
            comments(first:100,after:$cursor) {
              nodes { body }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
      }`,
      { threadId: thread.id, cursor: pageInfo.endCursor },
    );
    if (result.node.comments.nodes.some(isBlockedReply)) {
      return true;
    }
    pageInfo = result.node.comments.pageInfo;
  }
  return false;
}

async function scanUnresolvedCodexThreads(github, owner, repo, pullNumber, reviewBots) {
  const active = [];
  const outdated = [];
  const allowedReviewers = new Set(
    [...reviewBots].map((login) => normalizedLogin(login)),
  );
  let cursor = null;
  do {
    const result = await github.graphql(
      `query($owner:String!,$repo:String!,$pullNumber:Int!,$cursor:String) {
        repository(owner:$owner,name:$repo) {
          pullRequest(number:$pullNumber) {
            reviewThreads(first:100,after:$cursor) {
              nodes {
                id
                isResolved
                isOutdated
                comments(first:100) {
                  nodes {
                    body
                    url
                    author { login }
                  }
                  pageInfo { hasNextPage endCursor }
                }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
      }`,
      { owner, repo, pullNumber, cursor },
    );
    const threads = result.repository.pullRequest.reviewThreads;
    for (const thread of threads.nodes) {
      const [firstComment] = thread.comments.nodes;
      const isCodexReview = allowedReviewers.has(normalizedLogin(firstComment?.author?.login));
      if (thread.isResolved || !isCodexReview) {
        continue;
      }
      const summary = { id: thread.id, url: firstComment.url };
      if (thread.isOutdated) {
        outdated.push(summary);
      } else if (!(await hasBlockedReply(github, thread))) {
        active.push(summary);
      }
    }
    cursor = threads.pageInfo.hasNextPage ? threads.pageInfo.endCursor : null;
  } while (cursor);
  return { active, outdated };
}

async function resolveReviewThreads(github, core, threads) {
  for (const thread of threads) {
    await github.graphql(
      `mutation($threadId:ID!) {
        resolveReviewThread(input:{threadId:$threadId}) {
          thread { isResolved }
        }
      }`,
      { threadId: thread.id },
    );
    core.info(`Resolved outdated review thread ${thread.id}.`);
  }
}

async function issueComments(github, owner, repo, pullNumber) {
  return github.paginate(github.rest.issues.listComments, {
    owner,
    repo,
    issue_number: pullNumber,
    per_page: 100,
  });
}

async function hasOwnMarker(github, owner, repo, pullNumber, markerText) {
  const comments = await issueComments(github, owner, repo, pullNumber);
  const {
    data: { login: triggerUser },
  } = await github.rest.users.getAuthenticated();
  return comments.some(
    (comment) => comment.user?.login === triggerUser && comment.body?.includes(markerText),
  );
}

async function delegateReview({ github, context, core }) {
  const pull = context.payload.pull_request;
  const review = context.payload.review;
  const { owner, repo } = context.repo;
  const threads = await unresolvedThreads(
    github,
    owner,
    repo,
    pull.number,
    review.id,
    review.user.login,
  );
  if (threads.length === 0) {
    core.info("This review added no unresolved Codex threads.");
    return;
  }

  if (await hasOwnMarker(github, owner, repo, pull.number, marker(review.id, pull.head.sha))) {
    core.info("This review and head commit were already delegated.");
    return;
  }

  await github.rest.issues.createComment({
    owner,
    repo,
    issue_number: pull.number,
    body: delegationBody(review.id, pull.head.sha, pull.head.ref, threads),
  });
}

async function inspectManualReview({
  github,
  context,
  prNumber,
  prAuthors,
  reviewBots,
}) {
  const pullNumber = Number(prNumber);
  if (!Number.isSafeInteger(pullNumber) || pullNumber <= 0) {
    throw new Error("pr_number must be a positive integer.");
  }

  const { owner, repo } = context.repo;
  const { data: pull } = await github.rest.pulls.get({
    owner,
    repo,
    pull_number: pullNumber,
  });
  const allowedAuthors = allowedLogins(
    prAuthors,
    "zhengweibin-miduo,iuiiui,chatgpt-codex-connector[bot]",
  );
  assertManualPullEligible(pull, owner, repo, allowedAuthors);

  const allowedReviewers = allowedLogins(
    reviewBots,
    "chatgpt-codex-connector[bot]",
  );
  const { active, outdated } = await scanUnresolvedCodexThreads(
    github,
    owner,
    repo,
    pullNumber,
    allowedReviewers,
  );
  return { active, allowedAuthors, outdated, owner, pull, pullNumber, repo };
}

async function resolveOutdatedReviewThreads(options) {
  const { github, core } = options;
  const { outdated } = await inspectManualReview(options);
  await resolveReviewThreads(github, core, outdated);
}

async function delegateManualReview(options) {
  const { github, core } = options;
  const { active, allowedAuthors, owner, pull, pullNumber, repo } =
    await inspectManualReview(options);
  if (active.length === 0) {
    core.info("This pull request has no active unresolved Codex threads.");
    return;
  }

  const { data: currentPull } = await github.rest.pulls.get({
    owner,
    repo,
    pull_number: pullNumber,
  });
  assertManualPullEligible(currentPull, owner, repo, allowedAuthors);
  if (currentPull.head.sha !== pull.head.sha || currentPull.head.ref !== pull.head.ref) {
    throw new Error("Pull request head changed while preparing manual delegation.");
  }

  await github.rest.issues.createComment({
    owner,
    repo,
    issue_number: pullNumber,
    body: manualDelegationBody(pull.head.sha, pull.head.ref, active),
  });
}

async function selfTest() {
  const body = delegationBody(42, "abc123", "feature/test", [
    { id: "THREAD_1", url: "https://github.com/owner/repo/pull/7#discussion_r1" },
  ]);
  assert.match(body, /codex-review-loop:42:abc123/);
  assert.match(body, /^@codex fix$/m);
  assert.match(body, /修复此 PR 中所有有效且尚未解决的审查问题/);
  assert.match(body, /所有 GitHub 回复、提交信息和任务总结必须使用简体中文/);
  assert.match(body, /已修复的 thread 使用以下结构[\s\S]*提交 `<实际提交 SHA>` 已修复/);
  assert.match(body, /提交 `<实际提交 SHA>` 已修复/);
  assert.match(body, /确认无需修改的 thread[\s\S]*无需修改：<具体判断依据>/);
  assert.match(body, /不得声称已有提交修复或附加测试结果/);
  assert.match(body, /无法安全完成：<具体阻塞原因>/);
  assert.match(body, /该 thread 保持 unresolved/);
  assert.match(body, /修复、验证或推送任一步骤失败时，不得声称已修复或 resolve/);
  assert.match(body, /禁止粘贴测试进度条、warnings summary、堆栈或完整命令输出/);
  assert.match(body, /代码链接必须指向推送后的实际提交 SHA/);
  assert.doesNotMatch(body, /\bP1\b/);
  assert.match(body, /discussion_r1/);
  assert.match(body, /Expected head: `abc123`/);
  assert.match(body, /\.agents\/skills\/git-pr-rules\/SKILL\.md/);
  assert.match(body, /远端只是 Expected head 的线性后继/);
  assert.match(body, /可以安全同步、重新验证并继续普通推送/);
  assert.match(body, /历史关系不明、发生冲突[\s\S]*force-push 时才停止/);
  assert.match(body, /最小修复并验证/);
  assert.match(body, /仅对已完成修复或确认无需修改的 thread 回复并 resolve/);
  assert.match(body, /说明依据并 resolve/);
  assert.match(body, /无法安全完成的 thread 说明阻塞原因并保持 unresolved/);
  assert.match(body, /git push origin HEAD:feature\/test/);
  assert.doesNotMatch(body, /@codex review/);
  const context = {
    repo: { owner: "owner", repo: "repo" },
    payload: {
      pull_request: { number: 7, head: { ref: "feature/test", sha: "abc123" } },
      review: { id: 42, user: { login: "codex" } },
    },
  };
  const core = { info() {} };
  const createdBodies = [];
  const github = {
    graphql: async () => ({
      repository: {
        pullRequest: {
          reviewThreads: {
            nodes: [],
            pageInfo: { hasNextPage: false, endCursor: null },
          },
        },
      },
    }),
    paginate: async () => [],
    rest: {
      issues: {
        listComments() {},
        createComment: async ({ body: commentBody }) => createdBodies.push(commentBody),
      },
      users: { getAuthenticated: async () => ({ data: { login: "trusted-user" } }) },
    },
  };
  await delegateReview({ github, context, core });
  assert.equal(createdBodies.length, 0, "no unresolved threads must not create a comment");

  let page = 0;
  github.graphql = async () => {
    page++;
    return {
      repository: {
        pullRequest: {
          reviewThreads: {
            nodes:
              page === 1
                ? [
                    {
                      id: "OLD_THREAD",
                      isResolved: false,
                      comments: {
                        nodes: [
                          {
                            url: "https://github.com/owner/repo/pull/7#discussion_old",
                            author: { login: "codex" },
                            pullRequestReview: { databaseId: 41 },
                          },
                        ],
                      },
                    },
                  ]
                : [
                    {
                      id: "THREAD_1",
                      isResolved: false,
                      comments: {
                        nodes: [
                          {
                            url: "https://github.com/owner/repo/pull/7#discussion_r1",
                            author: { login: "codex" },
                            pullRequestReview: { databaseId: 42 },
                          },
                        ],
                      },
                    },
                  ],
            pageInfo:
              page === 1
                ? { hasNextPage: true, endCursor: "next" }
                : { hasNextPage: false, endCursor: null },
          },
        },
      },
    };
  };
  const threads = await unresolvedThreads(github, "owner", "repo", 7, 42, "codex");
  assert.deepEqual(
    threads,
    [{ id: "THREAD_1", url: "https://github.com/owner/repo/pull/7#discussion_r1" }],
    "only new threads from the current review are delegated",
  );
  page = 0;
  const botThreads = await unresolvedThreads(github, "owner", "repo", 7, 42, "codex[bot]");
  assert.deepEqual(
    botThreads,
    [{ id: "THREAD_1", url: "https://github.com/owner/repo/pull/7#discussion_r1" }],
    "GitHub bot login variants must match",
  );
  page = 0;
  github.paginate = async () => [
    { body: marker(41, "abc123"), user: { login: "trusted-user" } },
  ];
  await delegateReview({ github, context, core });
  assert.equal(
    createdBodies.length,
    1,
    "a different review on the same head must still be delegated",
  );

  page = 0;
  github.paginate = async () => [
    { body: marker(42, "abc123"), user: { login: "trusted-user" } },
  ];
  await delegateReview({ github, context, core });
  assert.equal(
    createdBodies.length,
    1,
    "the same review and head must not be delegated twice",
  );

  page = 0;
  createdBodies.length = 0;
  context.payload.pull_request.head = { ref: "feature/next", sha: "def456" };
  github.paginate = async () =>
    Array.from({ length: 100 }, (_, index) => ({
      body: marker(index, `sha-${index}`),
      user: { login: "trusted-user" },
    }));
  await delegateReview({ github, context, core });
  assert.equal(
    createdBodies.length,
    1,
    "historical delegation comments must not block a new review and head",
  );

  page = 0;
  github.paginate = async () => [
    ...Array.from({ length: 100 }, (_, index) => ({
      body: marker(index, `sha-${index}`),
      user: { login: "trusted-user" },
    })),
    { body: createdBodies[0], user: { login: "trusted-user" } },
  ];
  await delegateReview({ github, context, core });
  assert.equal(createdBodies.length, 1, "the same review and head must remain idempotent");

  const manualThreads = [
    { id: "THREAD_B", url: "https://github.com/owner/repo/pull/7#discussion_b" },
    { id: "THREAD_A", url: "https://github.com/owner/repo/pull/7#discussion_a" },
  ];
  assert.equal(
    manualMarker("manual-sha", manualThreads),
    manualMarker("manual-sha", [...manualThreads].reverse()),
    "manual idempotency marker must not depend on GraphQL thread order",
  );
  assert.equal(
    await hasBlockedReply(
      {
        graphql: async () => ({
          node: {
            comments: {
              nodes: [{ body: "无法安全完成：第 101 条回复中的阻塞原因。" }],
              pageInfo: { hasNextPage: false, endCursor: null },
            },
          },
        }),
      },
      {
        id: "THREAD_PAGED",
        comments: {
          nodes: [{ body: "review finding" }],
          pageInfo: { hasNextPage: true, endCursor: "page-2" },
        },
      },
    ),
    true,
    "blocked replies after the first 100 comments must be excluded",
  );

  const manualBodies = [];
  const resolvedThreadIds = [];
  const manualComments = [
    {
      body: `${marker(41, "older-sha")}\n- https://example.test/old (PRRT_ALREADY_DELEGATED)`,
      user: { login: "allowed-author" },
    },
  ];
  const manualGithub = {
    graphql: async (query, variables) => {
      if (query.includes("resolveReviewThread")) {
        resolvedThreadIds.push(variables.threadId);
        return { resolveReviewThread: { thread: { isResolved: true } } };
      }
      return {
        repository: {
          pullRequest: {
            reviewThreads: {
              nodes: [
              {
                id: "PRRT_MANUAL",
                isResolved: false,
                comments: {
                  nodes: [
                    {
                      body: "review finding",
                      url: "https://github.com/owner/repo/pull/7#discussion_manual",
                      author: { login: "codex-reviewer" },
                    },
                  ],
                  pageInfo: { hasNextPage: false, endCursor: null },
                },
              },
              {
                id: "PRRT_ALREADY_DELEGATED",
                isResolved: false,
                comments: {
                  nodes: [
                    {
                      body: "review finding",
                      url: "https://github.com/owner/repo/pull/7#discussion_delegated",
                      author: { login: "codex-reviewer" },
                    },
                  ],
                  pageInfo: { hasNextPage: false, endCursor: null },
                },
              },
              {
                id: "PRRT_RESOLVED",
                isResolved: true,
                comments: {
                  nodes: [
                    {
                      body: "review finding",
                      url: "https://github.com/owner/repo/pull/7#discussion_resolved",
                      author: { login: "codex-reviewer" },
                    },
                  ],
                  pageInfo: { hasNextPage: false, endCursor: null },
                },
              },
              {
                id: "PRRT_OUTDATED",
                isResolved: resolvedThreadIds.includes("PRRT_OUTDATED"),
                isOutdated: true,
                comments: {
                  nodes: [
                    {
                      body: "review finding",
                      url: "https://github.com/owner/repo/pull/7#discussion_outdated",
                      author: { login: "codex-reviewer" },
                    },
                  ],
                  pageInfo: { hasNextPage: false, endCursor: null },
                },
              },
              {
                id: "PRRT_BLOCKED",
                isResolved: false,
                comments: {
                  nodes: [
                    {
                      body: "review finding",
                      url: "https://github.com/owner/repo/pull/7#discussion_blocked",
                      author: { login: "codex-reviewer" },
                    },
                    {
                      body: "无法安全完成：缺少外部权限。",
                      url: "https://github.com/owner/repo/pull/7#discussion_blocked_reply",
                      author: { login: "codex" },
                    },
                  ],
                  pageInfo: { hasNextPage: false, endCursor: null },
                },
              },
              {
                id: "PRRT_OTHER_REVIEWER",
                isResolved: false,
                comments: {
                  nodes: [
                    {
                      body: "review finding",
                      url: "https://github.com/owner/repo/pull/7#discussion_other",
                      author: { login: "someone-else" },
                    },
                  ],
                  pageInfo: { hasNextPage: false, endCursor: null },
                },
              },
              ],
              pageInfo: { hasNextPage: false, endCursor: null },
            },
          },
        },
      };
    },
    paginate: async () => manualComments,
    rest: {
      issues: {
        listComments() {},
        createComment: async ({ body: commentBody }) => manualBodies.push(commentBody),
      },
      pulls: {
        get: async () => ({
          data: {
            draft: false,
            head: {
              ref: "feature/manual",
              sha: "manual-sha",
              repo: { full_name: "owner/repo" },
            },
            user: { login: "allowed-author" },
          },
        }),
      },
      users: { getAuthenticated: async () => ({ data: { login: "trusted-user" } }) },
    },
  };
  const manualContext = { repo: { owner: "owner", repo: "repo" } };
  await resolveOutdatedReviewThreads({
    github: manualGithub,
    context: manualContext,
    core,
    prNumber: "7",
    prAuthors: "allowed-author",
    reviewBots: "codex-reviewer[bot]",
  });
  const resolvedGraphql = manualGithub.graphql;
  manualGithub.graphql = async (query, variables) => {
    assert.doesNotMatch(
      query,
      /resolveReviewThread/,
      "the PAT-backed delegation client must not resolve threads",
    );
    return resolvedGraphql(query, variables);
  };
  await delegateManualReview({
    github: manualGithub,
    context: manualContext,
    core,
    prNumber: "7",
    prAuthors: "allowed-author",
    reviewBots: "codex-reviewer[bot]",
  });
  assert.equal(manualBodies.length, 1, "manual dispatch must create one delegation");
  assert.deepEqual(
    resolvedThreadIds,
    ["PRRT_OUTDATED"],
    "the resolver step must resolve outdated Codex threads",
  );
  assert.match(manualBodies[0], /PRRT_MANUAL/);
  assert.match(manualBodies[0], /PRRT_ALREADY_DELEGATED/);
  assert.doesNotMatch(manualBodies[0], /PRRT_RESOLVED/);
  assert.doesNotMatch(manualBodies[0], /PRRT_OUTDATED/);
  assert.doesNotMatch(manualBodies[0], /PRRT_BLOCKED/);
  assert.doesNotMatch(manualBodies[0], /PRRT_OTHER_REVIEWER/);

  manualComments.push({ body: manualBodies[0], user: { login: "trusted-user" } });
  await delegateManualReview({
    github: manualGithub,
    context: manualContext,
    core,
    prNumber: 7,
    prAuthors: "allowed-author",
    reviewBots: "codex-reviewer[bot]",
  });
  assert.equal(
    manualBodies.length,
    2,
    "previously delegated active unresolved threads must be delegated again",
  );
  assert.match(manualBodies[1], /PRRT_ALREADY_DELEGATED/);

  manualComments.length = 0;
  let pullReadCount = 0;
  manualGithub.rest.pulls.get = async () => ({
    data: {
      draft: false,
      head: {
        ref: "feature/manual",
        sha: pullReadCount++ === 0 ? "manual-sha" : "new-sha",
        repo: { full_name: "owner/repo" },
      },
      user: { login: "allowed-author" },
    },
  });
  await assert.rejects(
    delegateManualReview({
      github: manualGithub,
      context: manualContext,
      core,
      prNumber: 7,
      prAuthors: "allowed-author",
      reviewBots: "codex-reviewer[bot]",
    }),
    /Pull request head changed while preparing manual delegation/,
  );
  assert.equal(manualBodies.length, 2, "a stale manual delegation must not be created");

  manualGithub.rest.pulls.get = async () => ({
    data: {
      draft: true,
      head: { ref: "feature/manual", sha: "manual-sha", repo: { full_name: "owner/repo" } },
      user: { login: "allowed-author" },
    },
  });
  await assert.rejects(
    delegateManualReview({
      github: manualGithub,
      context: manualContext,
      core,
      prNumber: 7,
      prAuthors: "allowed-author",
      reviewBots: "codex-reviewer[bot]",
    }),
    /Draft pull requests cannot be delegated/,
  );
}

if (require.main === module) {
  selfTest().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

module.exports = {
  delegateReview,
  delegateManualReview,
  delegationBody,
  manualDelegationBody,
  manualMarker,
  marker,
  resolveOutdatedReviewThreads,
  resolveReviewThreads,
  scanUnresolvedCodexThreads,
  unresolvedThreads,
};
