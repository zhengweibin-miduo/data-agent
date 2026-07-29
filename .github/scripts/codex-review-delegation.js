const assert = require("node:assert/strict");

function marker(reviewId, headSha) {
  return `<!-- codex-review-loop:${reviewId}:${headSha} -->`;
}

function delegationBody(reviewId, headSha, headRef, threads) {
  const threadList = threads
    .map((thread) => `- ${thread.url || thread.id} (${thread.id})`)
    .join("\n");
  return `${marker(reviewId, headSha)}
@codex 修复下方列出的所有有效且尚未解决的审查问题。

PR branch: \`${headRef}\`
Expected head: \`${headSha}\`
Threads:
${threadList}

直接执行修复，不要只做分析、审查或等待人工确认。开始前确认 \`origin\` 存在，并确认远端分支仍指向 Expected head；如果 head 已变化，停止任务且不要推送。\`gh auth status\` 失败时仍要完成修复和推送，只需在结果中说明无法自动回复或 resolve thread。

请先读取根目录 \`code_review.md\`、PR diff 和每条 thread 的代码上下文，再逐条判断：
- 需要修复：做最小修复并验证。
- 不需要修复：在原 thread 说明依据并 resolve。
- 无法安全完成：在原 thread 说明阻塞原因，并保持 unresolved。

需要修改代码时只创建一个提交，然后执行 \`git push origin HEAD:${headRef}\`，禁止 force-push。推送成功后，仅对已完成修复或确认不需要修复的 thread 回复提交与验证依据并 resolve；无法安全完成的 thread 回复阻塞原因并保持 unresolved。不要创建新 PR，也不要另发起复审请求。所有 GitHub 回复和最终任务总结均使用简体中文，代码标识符、路径、命令、日志和错误原文除外。内部判断过程可以保留在执行上下文中，但 GitHub thread 回复和最终任务总结禁止出现 \`[裁决]\`、\`SHOULD_FIX\` 或同类内部分类标签。

实际完成代码修复的 thread 回复保持简短，并使用以下结构：

提交 \`<实际提交 SHA>\` 已修复：<一句话说明根因和修复方式>。

验证：\`<测试命令>\` 通过（<简要结果>）。
该 thread 已 resolve。

确认不需要修改代码的 thread 使用以下结构，不得声称已有提交修复或附加测试结果：

无需修改：<一句话说明判断依据>。
该 thread 已 resolve。

修复、验证或推送任一步骤未安全完成时，说明阻塞原因并保持 unresolved，不得回复已修复或 resolve。禁止粘贴测试进度条、warnings summary、堆栈或完整命令输出；只保留测试命令、通过数量及与本次修改直接相关的异常。最终任务总结中的代码链接必须指向推送后的实际提交 SHA，不得使用任务开始时的 Expected head。`;
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

  const comments = await github.paginate(github.rest.issues.listComments, {
    owner,
    repo,
    issue_number: pull.number,
    per_page: 100,
  });
  const {
    data: { login: triggerUser },
  } = await github.rest.users.getAuthenticated();
  const ownComments = comments.filter((comment) => comment.user?.login === triggerUser);
  if (
    ownComments.some((comment) =>
      comment.body?.includes(marker(review.id, pull.head.sha)),
    )
  ) {
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

async function selfTest() {
  const body = delegationBody(42, "abc123", "feature/test", [
    { id: "THREAD_1", url: "https://github.com/owner/repo/pull/7#discussion_r1" },
  ]);
  assert.match(body, /codex-review-loop:42:abc123/);
  assert.match(body, /修复下方列出的所有有效且尚未解决的审查问题/);
  assert.match(body, /所有 GitHub 回复和最终任务总结均使用简体中文/);
  assert.match(body, /GitHub thread 回复和最终任务总结禁止出现 `\[裁决\]`、`SHOULD_FIX` 或同类内部分类标签/);
  assert.match(body, /实际完成代码修复的 thread[\s\S]*提交 `<实际提交 SHA>` 已修复/);
  assert.match(body, /提交 `<实际提交 SHA>` 已修复/);
  assert.match(body, /确认不需要修改代码的 thread[\s\S]*无需修改：<一句话说明判断依据>/);
  assert.match(body, /不得声称已有提交修复或附加测试结果/);
  assert.match(body, /修复、验证或推送任一步骤未安全完成时[\s\S]*不得回复已修复或 resolve/);
  assert.match(body, /禁止粘贴测试进度条、warnings summary、堆栈或完整命令输出/);
  assert.match(body, /代码链接必须指向推送后的实际提交 SHA/);
  assert.doesNotMatch(body, /\bP1\b/);
  assert.match(body, /discussion_r1/);
  assert.match(body, /Expected head: `abc123`/);
  assert.match(body, /最小修复并验证/);
  assert.match(body, /仅对已完成修复或确认不需要修复的 thread.*resolve/);
  assert.match(body, /说明依据并 resolve/);
  assert.match(body, /无法安全完成的 thread 回复阻塞原因并保持 unresolved/);
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
}

if (require.main === module) {
  selfTest().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

module.exports = {
  delegateReview,
  delegationBody,
  marker,
  unresolvedThreads,
};
