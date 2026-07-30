const assert = require("node:assert/strict");

const COMMAND = "/codex-resolve-conflicts";
const MAX_RESOLUTION_ROUNDS = 10;
const MAX_MERGEABLE_ATTEMPTS = 4;
const NON_CONFLICT_STATES = new Set(["behind", "blocked", "clean", "has_hooks", "unstable"]);

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

function mergeabilityUnknown(pull) {
  return pull.mergeable == null || pull.mergeable_state === "unknown";
}

function hasContentConflict(pull) {
  if (mergeabilityUnknown(pull) || NON_CONFLICT_STATES.has(pull.mergeable_state)) {
    return false;
  }
  return pull.mergeable === false || pull.mergeable_state === "dirty";
}

async function getPullWithMergeability(github, owner, repo, pullNumber, sleep) {
  let pull;
  for (let attempt = 0; attempt < MAX_MERGEABLE_ATTEMPTS; attempt++) {
    ({ data: pull } = await github.rest.pulls.get({
      owner,
      repo,
      pull_number: pullNumber,
    }));
    if (!mergeabilityUnknown(pull)) return pull;
    if (attempt + 1 < MAX_MERGEABLE_ATTEMPTS) {
      await sleep(2 ** attempt * 1000);
    }
  }
  return pull;
}

function marker(pull, observedBaseSha) {
  return `<!-- codex-conflict-resolution:${pull.number}:${observedBaseSha}:${pull.head.sha} -->`;
}

function limitMarker() {
  return `<!-- codex-conflict-resolution-limit:${MAX_RESOLUTION_ROUNDS} -->`;
}

function samePullVersion(left, right) {
  return (
    left.head.sha === right.head.sha &&
    left.head.ref === right.head.ref &&
    left.base.ref === right.base.ref
  );
}

function delegationBody(pull, observedBaseSha) {
  return `${marker(pull, observedBaseSha)}
@codex 解决当前 PR 与实际 base 分支的内容冲突，只处理冲突，不顺带修改业务逻辑。

PR head: \`${pull.head.ref}\`
Expected head: \`${pull.head.sha}\`
PR base: \`${pull.base.ref}\`
Observed base: \`${observedBaseSha}\`

开始前读取根目录 \`.agents/skills/git-pr-rules/SKILL.md\` 并执行 \`git fetch --prune origin\`。Expected head 是冲突分析基线，不是要求远端永远不变的锁；\`origin/${pull.head.ref}\` 已变化时，先按项目 Git/PR Skill 的远端干预判定矩阵处理。远端只是 Expected head 的线性后继且本地没有其他改动时，可检出并同步最新原 head 后继续；历史关系不明、存在未识别或重叠改动、同步冲突、需要改写已发布历史或 force-push 时停止。然后执行 \`git merge --no-ff --no-commit origin/${pull.base.ref}\` 合并 fetch 后最新的 base，只解决冲突后创建 merge commit；无法可靠判断取舍时停止并报告。禁止 rebase，最终只创建一个 merge commit，不创建其他业务修改提交。

完成最小验证后，推送前重新 fetch；远端 head 再次前进时重新执行一次完整干预判定，安全同步后重新验证，第二次竞态或任一停止条件出现时不得推送；base 前进不阻止推送。仅使用 \`git push origin HEAD:${pull.head.ref}\` 推送回原 PR 分支，禁止 force-push，不要创建新 PR。

所有 GitHub 回复和最终总结使用简体中文，只保留冲突处理、merge commit SHA 和测试摘要。`;
}

async function delegateConflictResolution({
  github,
  context,
  core,
  authors,
  sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
}) {
  if (!context.payload.issue?.pull_request || !isCommand(context.payload.comment?.body)) {
    core.info("The comment is not an exact conflict resolution command on a pull request.");
    return;
  }

  const { owner, repo } = context.repo;
  const pullNumber = context.payload.issue.number;
  const pull = await getPullWithMergeability(github, owner, repo, pullNumber, sleep);
  const trigger = context.payload.comment.user.login;
  if (
    pull.draft ||
    pull.head.repo?.full_name !== `${owner}/${repo}` ||
    !authorized(pull.user.login, authors) ||
    !authorized(trigger, authors)
  ) {
    core.info("The pull request or command author is not eligible for conflict delegation.");
    return;
  }
  if (mergeabilityUnknown(pull)) {
    core.warning("GitHub did not finish computing mergeability within the retry limit.");
    return;
  }
  if (!hasContentConflict(pull)) {
    core.info("GitHub does not report a content conflict for this pull request.");
    return;
  }

  const {
    data: {
      object: { sha: observedBaseSha },
    },
  } = await github.rest.git.getRef({
    owner,
    repo,
    ref: `heads/${pull.base.ref}`,
  });
  const {
    data: currentPull,
  } = await github.rest.pulls.get({ owner, repo, pull_number: pullNumber });
  if (
    currentPull.draft ||
    currentPull.head.repo?.full_name !== `${owner}/${repo}` ||
    !samePullVersion(pull, currentPull) ||
    mergeabilityUnknown(currentPull) ||
    !hasContentConflict(currentPull)
  ) {
    core.warning("The pull request version or conflict state changed while mergeability was inspected.");
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
  const delegationMarker = marker(currentPull, observedBaseSha);
  if (ownComments.some((comment) => comment.body?.includes(delegationMarker))) {
    core.info("This pull request head and base were already delegated.");
    return;
  }

  const rounds = ownComments.filter((comment) =>
    comment.body?.includes("<!-- codex-conflict-resolution:"),
  ).length;
  if (rounds >= MAX_RESOLUTION_ROUNDS) {
    if (!ownComments.some((comment) => comment.body?.includes(limitMarker()))) {
      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: pullNumber,
        body: `${limitMarker()}
该 PR 已达到 ${MAX_RESOLUTION_ROUNDS} 轮 Codex 冲突解决委派上限，停止继续委派；请人工处理剩余冲突。`,
      });
    }
    core.warning(`The PR reached the ${MAX_RESOLUTION_ROUNDS}-round conflict resolution limit.`);
    return;
  }

  await github.rest.issues.createComment({
    owner,
    repo,
    issue_number: pullNumber,
    body: delegationBody(currentPull, observedBaseSha),
  });
}

async function selfTest() {
  assert.equal(isCommand(COMMAND), true);
  assert.equal(isCommand(` ${COMMAND}`), false);
  assert.equal(isCommand(`${COMMAND}\n`), false);
  assert.equal(authorized("alice", "alice,bob"), true);
  assert.equal(authorized("mallory", "alice,bob"), false);
  assert.equal(hasContentConflict({ mergeable: false, mergeable_state: "dirty" }), true);
  assert.equal(hasContentConflict({ mergeable: true, mergeable_state: "dirty" }), true);
  for (const state of ["behind", "blocked", "clean", "unstable"]) {
    assert.equal(
      hasContentConflict({ mergeable: false, mergeable_state: state }),
      false,
      `${state} must not be treated as a content conflict`,
    );
  }
  assert.equal(hasContentConflict({ mergeable: null, mergeable_state: "unknown" }), false);

  const basePull = {
    number: 7,
    draft: false,
    mergeable: false,
    mergeable_state: "dirty",
    user: { login: "alice" },
    head: { ref: "feature/conflict", sha: "head123", repo: { full_name: "owner/repo" } },
    base: { ref: "release/dev", sha: "base123" },
  };
  const body = delegationBody(basePull, "livebase456");
  assert.match(body, /Expected head: `head123`/);
  assert.match(body, /Observed base: `livebase456`/);
  assert.doesNotMatch(body, /Expected base/);
  assert.match(body, /base 前进不阻止推送/);
  assert.match(body, /\.agents\/skills\/git-pr-rules\/SKILL\.md/);
  assert.match(body, /远端只是 Expected head 的线性后继/);
  assert.match(body, /可检出并同步最新原 head 后继续/);
  assert.match(body, /历史关系不明[\s\S]*force-push 时停止/);
  assert.match(body, /第二次竞态或任一停止条件出现时不得推送/);
  assert.match(body, /origin\/release\/dev/);
  assert.match(body, /git merge --no-ff --no-commit origin\/release\/dev/);
  assert.match(body, /只创建一个 merge commit/);
  assert.match(body, /禁止 rebase/);
  assert.match(body, /禁止 force-push/);
  assert.match(body, /git push origin HEAD:feature\/conflict/);

  const context = {
    repo: { owner: "owner", repo: "repo" },
    payload: {
      issue: { number: 7, pull_request: {} },
      comment: { body: COMMAND, user: { login: "bob" } },
    },
  };
  const core = { info() {}, warning() {} };
  const createdBodies = [];
  let pulls = [];
  let liveBaseSha = "livebase456";
  const github = {
    paginate: async () => [],
    rest: {
      pulls: { get: async () => ({ data: pulls.shift() }) },
      git: {
        getRef: async ({ ref }) => {
          assert.equal(ref, "heads/release/dev");
          return { data: { object: { sha: liveBaseSha } } };
        },
      },
      issues: {
        listComments() {},
        createComment: async ({ body: commentBody }) => createdBodies.push(commentBody),
      },
      users: { getAuthenticated: async () => ({ data: { login: "token-owner" } }) },
    },
  };

  const delays = [];
  pulls = [
    { ...basePull, mergeable: null, mergeable_state: "unknown" },
    { ...basePull, mergeable: null, mergeable_state: "unknown" },
    basePull,
    basePull,
  ];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async (delay) => delays.push(delay),
  });
  assert.deepEqual(delays, [1000, 2000], "unknown mergeability uses finite backoff");
  assert.equal(createdBodies.length, 1, "a confirmed conflict is delegated once");
  assert.match(createdBodies[0], /codex-conflict-resolution:7:livebase456:head123/);
  assert.match(createdBodies[0], /Observed base: `livebase456`/);

  createdBodies.length = 0;
  pulls = Array(MAX_MERGEABLE_ATTEMPTS).fill({
    ...basePull,
    mergeable: null,
    mergeable_state: "unknown",
  });
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "unknown mergeability must not be delegated");

  pulls = [basePull, { ...basePull, base: { ...basePull.base, sha: "changed" } }];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 1, "a historical base SHA change must not stop delegation");
  assert.match(createdBodies[0], /Observed base: `livebase456`/);
  createdBodies.length = 0;

  pulls = [basePull, { ...basePull, head: { ...basePull.head, sha: "changed" } }];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "a changed head must stop delegation");

  pulls = [basePull, { ...basePull, base: { ...basePull.base, ref: "main" } }];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "a changed base ref must stop delegation");

  pulls = [basePull, { ...basePull, head: { ...basePull.head, ref: "feature/changed" } }];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "a changed head ref must stop delegation");

  pulls = [basePull, { ...basePull, mergeable: false, mergeable_state: "clean" }];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "a resolved conflict must stop delegation");

  pulls = [
    basePull,
    { ...basePull, mergeable: null, mergeable_state: "unknown" },
  ];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "unknown current mergeability must stop delegation");

  pulls = [basePull, { ...basePull, draft: true }];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "a pull request becoming draft must stop delegation");

  pulls = [basePull, basePull];
  const priorLiveBaseMarker = marker(basePull, liveBaseSha);
  github.paginate = async () => [
    { body: priorLiveBaseMarker, user: { login: "token-owner" } },
  ];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "the same base and head are idempotent");

  liveBaseSha = "livebase789";
  pulls = [basePull, basePull];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 1, "an advanced live base permits a new delegation");
  assert.match(createdBodies[0], /codex-conflict-resolution:7:livebase789:head123/);
  createdBodies.length = 0;

  context.payload.comment.user.login = "mallory";
  pulls = [basePull];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "the command author must be authorized");

  context.payload.comment.user.login = "bob";
  pulls = [{ ...basePull, user: { login: "mallory" } }];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "the PR author must be authorized");

  pulls = [{ ...basePull, draft: true }];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "a draft pull request must not be delegated");

  pulls = [
    {
      ...basePull,
      head: { ...basePull.head, repo: { full_name: "someone-else/repo" } },
    },
  ];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 0, "a fork pull request must not be delegated");

  github.paginate = async () =>
    Array.from({ length: MAX_RESOLUTION_ROUNDS }, (_, round) => ({
      body: `<!-- codex-conflict-resolution:7:base${round}:head${round} -->`,
      user: { login: "token-owner" },
    }));
  pulls = [basePull, basePull];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 1, "the tenth round publishes one limit notice");
  assert.match(createdBodies[0], /codex-conflict-resolution-limit:10/);

  github.paginate = async () => [
    ...Array.from({ length: MAX_RESOLUTION_ROUNDS }, (_, round) => ({
      body: `<!-- codex-conflict-resolution:7:base${round}:head${round} -->`,
      user: { login: "token-owner" },
    })),
    { body: limitMarker(), user: { login: "token-owner" } },
  ];
  pulls = [basePull, basePull];
  await delegateConflictResolution({
    github,
    context,
    core,
    authors: "alice,bob",
    sleep: async () => {},
  });
  assert.equal(createdBodies.length, 1, "the limit notice must be idempotent");
}

if (require.main === module) {
  selfTest().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}

module.exports = {
  COMMAND,
  MAX_MERGEABLE_ATTEMPTS,
  MAX_RESOLUTION_ROUNDS,
  authorized,
  delegateConflictResolution,
  delegationBody,
  getPullWithMergeability,
  hasContentConflict,
  isCommand,
  marker,
  mergeabilityUnknown,
  samePullVersion,
};
