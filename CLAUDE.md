# CLAUDE.md

This file provides guidance to Claude Code when working in this workspace.

## Purpose

A self-improving Claude Code workspace for ROS2


## Working on tasks

- Always `Read` a file before calling `Write` or `Edit` on it — the tools reject calls on files that have not been read in the current session, and skipping this wastes a round-trip
- Read the full task description and all available context before starting
- Follow existing code patterns and conventions
- Write clear, focused commit messages explaining the "why"
- Keep changes scoped to one task — avoid unrelated modifications
- Run the test suite if one exists before finalizing changes

## Avoiding tool-call errors

Every failed tool call wastes a round-trip and tokens. Follow these rules to
keep the error rate low:

### Prefer dedicated tools over Bash
- **Read files** → use `Read`, not `cat` / `head` / `tail` / `sed`
- **Search file contents** → use `Grep`, not `grep` / `rg`
- **Find files by name** → use `Glob`, not `find` / `ls`
- **Edit files** → use `Edit`, not `sed` / `awk`
- **Create files** → use `Write`, not `echo` / `cat <<EOF`
- Reserve `Bash` for commands that genuinely require shell execution (git, gh, python3, npm, etc.)

### Guard against Edit / Write failures
- **Always `Read` before `Edit` or `Write`** — the tools reject calls on unread files
- **Verify the file exists** before editing — use `Glob` or `Read` first if you are unsure about the path
- **Ensure `old_string` is unique** in the file — if it isn't, include more surrounding context or use `replace_all`

### Guard against Bash failures
- **Check that commands and paths exist** before running them — a quick `which <cmd>` or `ls <dir>` avoids "command not found" / "No such file" errors
- **Do not assume tools are installed** — the Docker image and CI runner have a fixed set of packages; if unsure, verify first
- **Avoid complex shell pipelines** when a single Python one-liner (`python3 -c '...'`) is clearer and less error-prone

## Code review

- Focus on correctness, readability, and security
- Flag potential bugs, edge cases, and missing error handling
- Suggest improvements only when they meaningfully improve the code
- Be specific — reference exact lines and propose concrete fixes
- To gather PR context, use `python3 scripts/collect-pr-review-context.py <pr-number>` instead of issuing multiple `gh api` / `gh pr view` Bash calls. It returns PR metadata, diff, linked issues, comments, and check-run status as a single JSON bundle.
- **Do NOT issue parallel `gh pr *` or `gh api` Bash calls.** When multiple parallel `gh` Bash calls are dispatched in one assistant turn, the first failure cancels all siblings — wasting every queued call. Always gather PR context with a single `scripts/collect-pr-review-context.py` call. If you must use `gh` directly, issue calls sequentially, never in parallel.
- **Do NOT spawn `Agent` subagents for simple lookups.** Use `Read`, `Grep`, or `Glob` directly — they are faster and cheaper. Each `Agent` subagent adds ~5k tokens of overhead. Specifically:
  - To read a file or check its contents → `Read`
  - To find files by name/pattern → `Glob`
  - To search code for a string or regex → `Grep`
  - To check a function signature or class definition → `Grep` or `Read`
  - **Only** use `Agent` when the task genuinely requires multi-step exploration across many files where you cannot predict the search path in advance (e.g., tracing a complex call chain through 5+ files). A sequence of 2+ consecutive `Agent` calls is almost always wrong — use direct tools instead.

## Safety rules

- Never commit secrets, API keys, credentials, or private keys
- Never modify `.env`, `*.key`, `*.pem`, or `credentials.*` files
- Never force-push or rewrite published history
- Never skip CI checks or pre-commit hooks

## Key conventions

- When something fails or needs rework, capture the lesson so the pattern is not repeated
- Prefer simple, direct solutions over abstractions until a pattern repeats
- The auto-improve system is split in two: `auto-improve-discover.yml` raises/updates tracked issues and ships fix PRs using `Refs #<num>` (never `Fixes #`/`Closes #`/`Resolves #`), and `auto-improve-verify.yml` owns all per-issue before/after comparison and is the only workflow allowed to close an `auto-improve` issue.
- The `Read` tool caps file content at 10k tokens and rejects unbounded calls on larger files with `File content (N tokens) exceeds maximum allowed tokens (10000). Use offset and limit parameters...`. For files you expect to be large (workflow logs, JSON bundles from `scripts/collect-pr-review-context.py`, long markdown prompts under `scripts/`, parser output, transcripts), do one of: (a) `Grep` first and then `Read` with a targeted `offset`/`limit` window, (b) start with `Read(..., limit=200)` and paginate only as needed, or (c) pipe the file through a `python3 -c` filter via `Bash`. Do not issue an unbounded `Read` on a file whose size you have not checked.

<!--
CI-sandbox Bash rules (allowlist, no multi-op pipes, no /tmp redirection,
no 2>&1, literal gh args, read-only workflow files) live in
docs/ci-sandbox-rules.md and are appended to this file at runtime by the
CI workflows (.github/workflows/claude*.yml, auto-improve-*.yml). They are
intentionally not inlined here so local runs stay free of CI-only
guardrails.
-->
