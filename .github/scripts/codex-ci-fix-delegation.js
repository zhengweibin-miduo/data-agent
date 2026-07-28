const assert = require("node:assert/strict");

const COMMAND = "/codex-fix-ci";
const MAX_FIX_ROUNDS = 10;
const FAILED_CONCLUSIONS = new Set([
  "action_required",
  "cancelled",
  "failure",
  "stale",
  "startup_failure",
  "timed_out",
]);

function isCommand(body) {
  return body === COMMAND;
}

function authorized(login, authors) {
  return authors
    .split(",")
    .map((author) => author.trim())
    .filter(Boolean)
    .includes(login);
}

function marker(pullNumber, headSha, runs) {
  const runIds = runs
    .map((run) => run.id)
    .sort((left, right) => left - right)
    .join(",");
  return `<!-- codex-ci-fix:${pullNumber}:${headSha}:${runIds} -->`;
}

function limitMarker() {
  return `<!-- codex-ci-fix-limit:${MAX_FIX_ROUNDS} -->`;
}

function delegationBody(pull, runs) {
  const failures = runs
    .map((run) => {
      const jobs = run.jobs
        .map((job) => `  - Job: [${job.name}](${job.html_url})`)
        .join("\n");
      return `- Workflow: [${run.name}](${run.html_url})\n${jobs}`;
    })
    .join("\n");

  return `${marker(pull.number, pull.head.sha, runs)}
@codex 修复当前提交下方列出的 GitHub Actions CI 失败。

PR branch: \`${pull.head.ref}\`
Expected head: \`${pull.head.sha}\`
Failures:
${failures}

直接定位根因并做最小修复，不要复制完整 Actions 或测试日志。开始前确认 \`origin\` 存在；修改和验证完成后只创建一个提交。推送前再次确认远端 \`origin/${pull.head.ref}\` 仍等于 Expected head，若已变化则停止且不得推送。使用 \`git push origin HEAD:${pull.head.ref}\` 推送回原 PR 分支，禁止 force-push，不要创建新 PR。

所有 GitHub 回复和最终总结使用简体中文，只保留提交 SHA、根因、修复内容和测试摘要。`;
}

async function failedRuns(github, owner, repo, pullNumber, headSha) {
  const runs = await github.paginate(github.rest.actions.listWorkflowRunsForRepo, {
    owner,
    repo,
    head_sha: headSha,
    status: "completed",
    per_page: 100,
  });
  const failed = runs.filter(
    (run) =>
      run.head_sha === headSha &&
      run.event === "pull_request" &&
      run.pull_requests?.some((pull) => pull.number === pullNumber) &&
      run.status === "completed" &&
      FAILED_CONCLUSIONS.has(run.conclusion),
  );

  return Promise.all(
    failed.map(async (run) => {
      const jobs = await github.paginate(github.rest.actions.listJobsForWorkflowRun, {
        owner,
        repo,
        run_id: run.id,
        filter: "latest",
        per_page: 100,
      });
      return {
        id: run.id,
        name: run.name,
        html_url: run.html_url,
        jobs: jobs
          .filter((job) => FAILED_CONCLUSIONS.has(job.conclusion))
          .map(({ name, html_url }) => ({ name, html_url })),
      };
    }),
  );
}

async function delegateCiFix({ github, context, core, authors }) {
  if (!context.payload.issue?.pull_request || !isCommand(context.payload.comment?.body)) {
    core.info("The comment is not an exact CI fix command on a pull request.");
    return;
  }

  const { owner, repo } = context.repo;
  const pullNumber = context.payload.issue.number;
  const {
    data: pull,
  } = await github.rest.pulls.get({ owner, repo, pull_number: pullNumber });
  const trigger = context.payload.comment.user.login;
  if (
    pull.draft ||
    pull.head.repo?.full_name !== `${owner}/${repo}` ||
    !authorized(pull.user.login, authors) ||
    !authorized(trigger, authors)
  ) {
    core.info("The pull request or command author is not eligible for CI fix delegation.");
    return;
  }

  const headSha = pull.head.sha;
  const runs = await failedRuns(github, owner, repo, pullNumber, headSha);
  if (runs.length === 0) {
    core.info("The current pull request head has no failed GitHub Actions runs.");
    return;
  }

  const {
    data: currentPull,
  } = await github.rest.pulls.get({ owner, repo, pull_number: pullNumber });
  if (currentPull.head.sha !== headSha) {
    core.warning("The pull request head changed while failures were inspected.");
    return;
  }

  const comments = await github.paginate(github.rest.issues.listComments, {
    owner,
    repo,
    issue_number: pullNumber,
    per_page: 100,
  });
  const {
    data: { login: commentAuthor },
  } = await github.rest.users.getAuthenticated();
  const ownComments = comments.filter((comment) => comment.user?.login === commentAuthor);
  const delegationMarker = marker(pullNumber, headSha, runs);
  if (ownComments.some((comment) => comment.body?.includes(delegationMarker))) {
    core.info("This head and failed run set were already delegated.");
    return;
  }

  const rounds = ownComments.filter((comment) =>
    comment.body?.includes("<!-- codex-ci-fix:"),
  ).length;
  if (rounds >= MAX_FIX_ROUNDS) {
    if (!ownComments.some((comment) => comment.body?.includes(limitMarker()))) {
      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: pullNumber,
        body: `${limitMarker()}
该 PR 已达到 ${MAX_FIX_ROUNDS} 轮 Codex CI 修复委派上限，停止继续委派；请人工处理剩余失败。`,
      });
    }
    core.warning(`The PR reached the ${MAX_FIX_ROUNDS}-round CI fix limit.`);
    return;
  }

  await github.rest.issues.createComment({
    owner,
    repo,
    issue_number: pullNumber,
    body: delegationBody(currentPull, runs),
  });
}

async function selfTest() {
  assert.equal(isCommand(COMMAND), true);
  assert.equal(isCommand(` ${COMMAND}`), false);
  assert.equal(isCommand(`${COMMAND}\n`), false);
  assert.equal(isCommand(`${COMMAND} now`), false);
  assert.equal(authorized("alice", "alice,bob"), true);
  assert.equal(authorized("mallory", "alice,bob"), false);

  const runs = [
    {
      id: 20,
      name: "test",
      html_url: "https://github.test/runs/20",
      jobs: [{ name: "unit", html_url: "https://github.test/jobs/21" }],
    },
    {
      id: 10,
      name: "lint",
      html_url: "https://github.test/runs/10",
      jobs: [{ name: "ruff", html_url: "https://github.test/jobs/11" }],
    },
  ];
  const pull = {
    number: 7,
    head: { ref: "feature/fix", sha: "abc123" },
  };
  assert.equal(
    marker(7, "abc123", runs),
    "<!-- codex-ci-fix:7:abc123:10,20 -->",
    "run order must not affect idempotency",
  );
  const body = delegationBody(pull, runs);
  assert.match(body, /@codex/);
  assert.match(body, /Expected head: `abc123`/);
  assert.match(body, /只创建一个提交/);
  assert.match(body, /再次确认远端 `origin\/feature\/fix` 仍等于 Expected head/);
  assert.match(body, /若已变化则停止且不得推送/);
  assert.match(body, /git push origin HEAD:feature\/fix/);
  assert.match(body, /禁止 force-push/);
  assert.match(body, /只保留提交 SHA、根因、修复内容和测试摘要/);
  assert.match(body, /github\.test\/jobs\/21/);

  const workflowRuns = [
    {
      id: 1,
      head_sha: "abc123",
      event: "pull_request",
      pull_requests: [{ number: 7 }],
      status: "completed",
      conclusion: "failure",
    },
    {
      id: 2,
      head_sha: "old",
      event: "pull_request",
      pull_requests: [{ number: 7 }],
      status: "completed",
      conclusion: "failure",
    },
    {
      id: 3,
      head_sha: "abc123",
      event: "pull_request",
      pull_requests: [{ number: 7 }],
      status: "completed",
      conclusion: "success",
    },
    {
      id: 4,
      head_sha: "abc123",
      event: "push",
      pull_requests: [{ number: 7 }],
      status: "completed",
      conclusion: "failure",
    },
    {
      id: 5,
      head_sha: "abc123",
      event: "pull_request",
      pull_requests: [{ number: 8 }],
      status: "completed",
      conclusion: "failure",
    },
  ];
  const githubForRuns = {
    paginate: async (method, options) =>
      options.run_id
        ? [
            { name: "failed", html_url: "job-url", conclusion: "failure" },
            { name: "passed", html_url: "passed-url", conclusion: "success" },
          ]
        : workflowRuns,
    rest: {
      actions: {
        listWorkflowRunsForRepo() {},
        listJobsForWorkflowRun() {},
      },
    },
  };
  const selected = await failedRuns(githubForRuns, "owner", "repo", 7, "abc123");
  assert.deepEqual(
    selected.map((run) => ({ id: run.id, jobs: run.jobs })),
    [{ id: 1, jobs: [{ name: "failed", html_url: "job-url" }] }],
    "only failed pull_request runs and jobs for the current PR and head are delegated",
  );

  const basePull = {
    number: 7,
    draft: false,
    user: { login: "alice" },
    head: {
      ref: "feature/fix",
      sha: "abc123",
      repo: { full_name: "owner/repo" },
    },
  };
  const context = {
    repo: { owner: "owner", repo: "repo" },
    payload: {
      issue: { number: 7, pull_request: {} },
      comment: { body: COMMAND, user: { login: "bob" } },
    },
  };
  const createdBodies = [];
  const core = { info() {}, warning() {} };
  let pullReads = 0;
  const github = {
    paginate: async (method, options) => {
      if (options.head_sha) return workflowRuns.slice(0, 1);
      if (options.run_id) {
        return [{ name: "unit", html_url: "job-url", conclusion: "failure" }];
      }
      return [];
    },
    rest: {
      actions: {
        listWorkflowRunsForRepo() {},
        listJobsForWorkflowRun() {},
      },
      pulls: {
        get: async () => {
          pullReads++;
          return {
            data:
              pullReads === 1
                ? basePull
                : { ...basePull, head: { ...basePull.head, sha: "changed" } },
          };
        },
      },
      issues: {
        listComments() {},
        createComment: async ({ body: commentBody }) => createdBodies.push(commentBody),
      },
      users: { getAuthenticated: async () => ({ data: { login: "token-owner" } }) },
    },
  };
  await delegateCiFix({ github, context, core, authors: "alice,bob" });
  assert.equal(createdBodies.length, 0, "a changed head must stop delegation");

  pullReads = 0;
  github.rest.pulls.get = async () => ({ data: basePull });
  github.paginate = async (method, options) => {
    if (options.head_sha) return workflowRuns.slice(0, 1);
    if (options.run_id) {
      return [{ name: "unit", html_url: "job-url", conclusion: "failure" }];
    }
    return [
      {
        body: marker(7, "abc123", [{ id: 1 }]),
        user: { login: "token-owner" },
      },
    ];
  };
  await delegateCiFix({ github, context, core, authors: "alice,bob" });
  assert.equal(createdBodies.length, 0, "the same head and failed run set is idempotent");

  context.payload.comment.user.login = "mallory";
  github.paginate = async () => {
    throw new Error("unauthorized commands must stop before reading runs");
  };
  await delegateCiFix({ github, context, core, authors: "alice,bob" });
  assert.equal(createdBodies.length, 0, "the command author must be authorized");

  context.payload.comment.user.login = "bob";
  github.rest.pulls.get = async () => ({
    data: { ...basePull, user: { login: "mallory" } },
  });
  await delegateCiFix({ github, context, core, authors: "alice,bob" });
  assert.equal(createdBodies.length, 0, "the PR author must be authorized");
}

if (require.main === module) {
  selfTest().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

module.exports = {
  COMMAND,
  FAILED_CONCLUSIONS,
  MAX_FIX_ROUNDS,
  authorized,
  delegateCiFix,
  delegationBody,
  failedRuns,
  isCommand,
  marker,
};
