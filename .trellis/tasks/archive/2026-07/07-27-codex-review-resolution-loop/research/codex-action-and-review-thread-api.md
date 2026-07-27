# Codex GitHub Delegation and Review Thread API

## Codex GitHub Integration

- `@codex review` requests a review.
- Any other `@codex` PR comment starts a cloud chat with the PR as context.
- Codex can push a fix back to the PR branch when it has permission.
- Automatic reviews can run on every push; a repair task does not need to request another review.
- Sources:
  - https://learn.chatgpt.com/docs/third-party/github

## User-Identity Trigger

- A GitHub API request authenticated with a user's PAT creates the comment as that user.
- A fine-grained PAT should be limited to this repository and Pull requests: Read and write.
- `GITHUB_TOKEN` comments are authored by `github-actions[bot]` and are not used for the Codex delegation trigger.
- Sources:
  - https://docs.github.com/en/rest/issues/comments#create-an-issue-comment
  - https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens

## Repository Evidence

- Codex Review is configured to run on every push.
- Existing workflow uses `anthropics/claude-code-action@v1`, `CLAUDE_CODE_OAUTH_TOKEN`, and `contents: read`.
- Repository Secret `CODEX_TRIGGER_TOKEN` was verified as configured on 2026-07-27; the obsolete `CLAUDE_CODE_OAUTH_TOKEN` remains outside this task's deletion scope.
