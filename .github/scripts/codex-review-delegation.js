const assert = require("node:assert/strict");

function marker(reviewId, headSha) {
  return `<!-- codex-review-loop:${reviewId}:${headSha} -->`;
}

function delegationBody(reviewId, headSha, threadIds) {
  return `${marker(reviewId, headSha)}
@codex 请处理本次 review 新增的未解决意见：${threadIds.join(", ")}。

请先读取根目录 \`code_review.md\`、PR diff 和每条 thread 的代码上下文，再逐条判断：
- 需要修复：做最小修复并验证，推送后在原 thread 回复依据并 resolve。
- 不需要修复：在原 thread 说明依据并 resolve。
- 无法安全完成：在原 thread 说明阻塞原因，并保持 unresolved。

推送后依赖仓库已启用的自动 Codex Review；不要另发起复审请求。`;
}

async function unresolvedThreadIds(github, owner, repo, pullNumber, reviewId, reviewer) {
  const ids = [];
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
        firstComment.author?.login === reviewer;
      if (!thread.isResolved && belongsToReview) ids.push(thread.id);
    }
    cursor = threads.pageInfo.hasNextPage ? threads.pageInfo.endCursor : null;
  } while (cursor);
  return ids;
}

async function delegateReview({ github, context, core }) {
  const pull = context.payload.pull_request;
  const review = context.payload.review;
  const { owner, repo } = context.repo;
  const idempotencyMarker = marker(review.id, pull.head.sha);
  const threadIds = await unresolvedThreadIds(
    github,
    owner,
    repo,
    pull.number,
    review.id,
    review.user.login,
  );
  if (threadIds.length === 0) {
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
  if (
    comments.some(
      (comment) =>
        comment.user?.login === triggerUser && comment.body?.includes(idempotencyMarker),
    )
  ) {
    core.info("This review and head commit were already delegated.");
    return;
  }

  await github.rest.issues.createComment({
    owner,
    repo,
    issue_number: pull.number,
    body: delegationBody(review.id, pull.head.sha, threadIds),
  });
}

async function selfTest() {
  const body = delegationBody(42, "abc123", ["THREAD_1"]);
  assert.match(body, /codex-review-loop:42:abc123/);
  assert.match(body, /最小修复并验证/);
  assert.match(body, /说明依据并 resolve/);
  assert.match(body, /保持 unresolved/);
  assert.doesNotMatch(body, /@codex review/);

  const context = {
    repo: { owner: "owner", repo: "repo" },
    payload: {
      pull_request: { number: 7, head: { sha: "abc123" } },
      review: { id: 42, user: { login: "codex" } },
    },
  };
  const core = { info() {} };
  let created = 0;
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
      issues: { listComments() {}, createComment: async () => created++ },
      users: { getAuthenticated: async () => ({ data: { login: "trusted-user" } }) },
    },
  };
  await delegateReview({ github, context, core });
  assert.equal(created, 0, "no unresolved threads must not create a comment");

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
  const ids = await unresolvedThreadIds(github, "owner", "repo", 7, 42, "codex");
  assert.deepEqual(ids, ["THREAD_1"], "only new threads from the current review are delegated");
  page = 0;
  github.paginate = async () => [
    { body: marker(42, "abc123"), user: { login: "trusted-user" } },
  ];
  await delegateReview({ github, context, core });
  assert.equal(created, 0, "an existing marker must prevent duplicate delegation");
}

if (require.main === module) {
  selfTest().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

module.exports = { delegateReview, delegationBody, marker, unresolvedThreadIds };
