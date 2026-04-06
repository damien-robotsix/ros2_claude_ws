# CI sandbox — Bash command rules

When running inside `anthropics/claude-code-action@v1` (the claude.yml,
claude-code-review.yml, claude-agent.yml, auto-improve-discover.yml,
auto-improve-verify.yml, hub-daily-sweep.yml, hub-sync.yml, and hub-adopt.yml workflows), Bash tool calls are
restricted to the `--allowed-tools` list in the workflow and are further
filtered by a sandbox. To avoid wasting tool calls on rejected commands,
follow these rules:

- **One operation per `Bash` call.** Shell pipes (`|`), command chains
  (`&&`, `;`), command substitution (`$(...)`, backticks), and process
  substitution each count as separate operations. Each sub-command must
  *individually* match the allowlist, or the whole call is rejected with
  `This Bash command contains multiple operations. The following part
  requires approval: ...`. If you need to post-process output, pipe it
  through a single `python3 -c '...'` instead, or split into sequential
  calls.
- **Do not redirect output outside the working directory.** Writing to
  `/tmp/*` or any absolute path outside
  `/home/runner/work/<repo>/<repo>` is blocked with `Output redirection
  to '...' was blocked`. Use `Write` for files in the repo; for scratch
  data, use a path under the working directory (e.g. `./.scratch/…`) and
  clean up afterward.
- **`2>&1` counts as redirection.** Omit it; tool results already include
  stderr.
- **`gh` arguments must be literal values, not URL paths.** `gh pr view
  owner/repo/pull/7` is read as a branch name and fails with
  `no pull requests found for branch "owner/repo/pull/7"`. Pass the PR
  number alone (`gh pr view 7`) and use `--repo owner/repo` if you need
  a non-default target.
- **Never issue parallel `gh` Bash calls.** When multiple `gh pr *` or
  `gh api` calls are dispatched in the same assistant turn, the first
  failure cancels all siblings — every queued call is wasted. Use
  `scripts/collect-pr-review-context.py` for PR context, or issue `gh`
  calls one at a time, sequentially.
- **Workflow files are effectively read-only.** The GitHub App token this
  agent runs under does not carry the `workflows` permission, so pushes
  that touch `.github/workflows/**` are rejected at the remote. If a fix
  needs a workflow change, surface it as a recommendation with a diff
  rather than editing the file.
- **`.claude/agents/` is writable.** Agent definition files under
  `.claude/agents/` are regular repo files — you may read, edit, and
  commit changes to them like any other source file. Do not self-restrict
  writes to this directory.

## Common CI sandbox mistakes (avoid these)

These patterns cause the majority of Bash/Edit errors in CI runs:

| Mistake | Example | Fix |
|---------|---------|-----|
| Pipe through unapproved command | `gh pr view 5 \| jq .title` | Split into two calls or use `python3 -c` |
| Redirect stderr | `git status 2>&1` | Omit `2>&1` — stderr is captured automatically |
| Write to `/tmp` | `echo x > /tmp/foo` | Use `./.scratch/foo` or `Write` tool |
| Edit unread file | `Edit(file="new.md", ...)` | `Read("new.md")` first |
| `cat` / `head` / `grep` via Bash | `cat README.md` | Use the `Read` / `Grep` dedicated tools |
| Chain commands with `&&` | `mkdir -p dir && echo done` | Use two separate Bash calls or a single `python3 -c` |
