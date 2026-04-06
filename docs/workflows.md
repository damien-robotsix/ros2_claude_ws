---
title: Workflows
layout: default
---

# Workflows

The repo ships seven GitHub Actions workflows under `.github/workflows/`. Most read model assignments from [`auto_tune_config.yml`](configuration.md).

## `claude.yml` — interactive agent

Triggered when `@claude` is mentioned in an issue or PR comment, or when an issue is assigned/labelled accordingly. Runs the `anthropics/claude-code-action@v1` action with the model configured as `models.claude_code`.

Use it to ask Claude to answer questions, review changes, or implement small-to-medium tasks directly from GitHub.

## `claude-code-review.yml` — automated review

Runs on pull requests and asks Claude (using `models.code_review`) to review the diff. The review focuses on correctness, readability, and security, as laid out in [`CLAUDE.md`](https://github.com/damien-robotsix/claude_auto_tune/blob/main/CLAUDE.md).

## `auto-improve-discover.yml` + `auto-improve-verify.yml` — self-tuning loop

The self-tuning loop is split into two workflows that share the `auto-improve` label namespace but own separate halves of the lifecycle.

`auto-improve-discover.yml` runs on the cron schedule defined in `auto_improve.schedule` (and can be dispatched manually). It inspects recent workflow runs and Claude Code session transcripts, parses them via the scripts in `scripts/`, and maintains a long-term **issue tracker** for each improvement subject. When it creates a new issue it writes a `## Baseline (before fix)` section into the body — a frozen snapshot of the signal counts and excerpts as they were at the moment the problem was promoted to an issue. When it can fix a problem automatically, it opens a focused PR that references the issue via **`Refs #<num>`** (never `Fixes #`, `Closes #`, or `Resolves #` — GitHub's auto-close behaviour is intentionally disabled so that only the verify workflow can close). The discover workflow only advances issues along `raised → pr-open`.

`auto-improve-verify.yml` runs on the cron schedule defined in `auto_improve_verify.schedule` (daily by default, and dispatchable with an `issue_number` input to verify one issue at a time). For each target issue it resolves a `WINDOW_START` timestamp — the linked PR's `mergedAt` if the fix PR has merged, otherwise the issue creation time — and passes it to the extractor subagent as a **hard filter**. The subagent then only parses workflow logs and session transcripts from GitHub Actions runs created at or after that timestamp, so conversations and logs appended *before* the fix cannot enter the "after" snapshot. The subagent is also scoped to the issue's fingerprint key so it only returns matching signals. Verify compares the in-window counts to the frozen `## Baseline (before fix)` section, appends a `## Verification history` entry, and makes the lifecycle decision. It is the only workflow allowed to advance issues to `auto-improve:merged`, close them as `auto-improve:solved`, or reopen them on regression. There is no verification threshold to tune: a single clean verify run with zero in-window matches is enough to close, and regressions after closure are caught by the daily cron.

Session transcripts analyzed per run are capped by `auto_improve.default_conversation_limit`; workflow logs are always parsed in full. When the local-transcript lane is enabled (`hub.local_transcripts.enabled: true`) and the `HUB_TOKEN` secret is provisioned, both workflows fetch session transcripts published from local Docker runs via the shared hub repo before invoking Claude. The `workflow-insights-extractor` subagent then folds those local-run signals into the same clustering pass alongside CI transcripts. Forks without the secret provisioned skip this step silently — the auto-improve loop works exactly as before.

The goal is to let the workspace grow incrementally: when a pattern of failure or friction repeats, the loop captures the lesson rather than repeating the mistake, and a fix is only considered "done" once a dedicated per-issue run has confirmed the problem is actually gone.

## `hub-daily-sweep.yml` — cross-workspace improvement sharing

Runs daily at 03:00 UTC (and on manual dispatch with a configurable lookback window). Phase 1 of the hub protocol: scans PRs merged into `main` in the last 24 hours, uses Claude to judge which are generalizable, and opens proposal issues in the configured hub repo (`hub.repo` in `auto_tune_config.yml`). This workflow is publish-only — it never adopts proposals from other forks. Gated by the `hub.enabled` master switch.

## `config-sanity.yml` — dead-key guard

Runs on every PR targeting `main`, on pushes to `main`, and daily at 06:00 UTC. Executes `scripts/check-config-keys.py`, which enumerates every leaf key in `auto_tune_config.yml` and fails if any key has no reader in `scripts/`, `.github/workflows/`, or `run.sh`. This catches the class of bug where a config entry sits dead because the consumer was deleted or hardcodes a literal fallback.

## Docs-sync agent

A daily docs-sync agent keeps pages under `docs/` aligned with what landed on `main` in the last 24 hours. It is deliberately narrow: it only edits `docs/`, never proposes code changes, and uses [`docs/.docsrules`](https://github.com/damien-robotsix/claude_auto_tune/blob/main/docs/.docsrules) to route changed source files to the doc page most likely to need updating. Its full instructions live in [`scripts/docs-sync-prompt.md`](https://github.com/damien-robotsix/claude_auto_tune/blob/main/scripts/docs-sync-prompt.md).

## Scripts

Supporting scripts live in [`scripts/`](https://github.com/damien-robotsix/claude_auto_tune/tree/main/scripts):

- `auto-improve-discover-prompt.md` — the prompt used by the discover half of the auto-improve tracker (raises/updates issues, ships PRs with `Refs #<num>`).
- `auto-improve-verify-prompt.md` — the prompt used by the per-issue verify half of the tracker (before/after comparison, exclusive owner of `merged`/`solved` transitions and of closing auto-improve issues).
- `docs-sync-prompt.md` — the prompt used by the daily docs-sync agent.
- `collect-doc-relevant-diff.sh` — emits the commit list and unified diff the docs-sync agent consumes from `.scratch/`.
- `parse-claude-transcript.py` — **deterministic** aggregator over Claude Code session JSONL files. Emits tool-call counts, error tools, repeated consecutive runs, token usage, and a sequence preview. No LLM calls.
- `parse-workflow-log.py` — **deterministic** regex-based signal extractor over raw GitHub Actions logs. Emits counts and samples for errors, tool denials, workflow-permission rejections, HTTP errors, non-zero exits, retries, timeouts, and rate limits. No LLM calls.
- `hub/push-local-transcripts.py` — **deterministic** publisher for local Claude Code session transcripts. Copies new `*.jsonl` files from `.claude-home/.claude/projects/` into `transcripts/<workspace-slug>/<YYYY-MM-DD>/` in the shared [`claude-auto-tune-hub`](https://github.com/damien-robotsix/claude-auto-tune-hub) repo, with built-in secret/host-path redaction. Invoked automatically by [`run.sh`](https://github.com/damien-robotsix/claude_auto_tune/blob/main/run.sh) after every local Docker session, and a hard no-op when `hub.local_transcripts.enabled` is false (the default) — so wiring is unconditional and the config file is the single source of truth. Failures (no network, missing git credentials) are logged but never fail the shell, so a broken sync can never mask a real Claude Code exit status. Reuses the hub repo created for the improvement-proposal lane but writes to a disjoint subtree and shares no code with `hub/hub-open-proposal.py`. No LLM calls.
- `hub/fetch-local-transcripts.py` — **deterministic** fetcher (the pull half). Run by CI workflows before the Claude step to populate `.scratch/hub-transcripts/` with session JSONL files from the hub. The `workflow-insights-extractor` subagent parses this directory alongside CI artifacts. Hard no-op when hub config is off or `HUB_TOKEN` is missing. Auth is scoped: the `HUB_TOKEN` env var is only visible to this step. No LLM calls.
- `collect-pr-review-context.py` — **deterministic** PR-context collector. Given a PR number, bundles PR metadata, diff, linked issues (parsed from closing keywords), issue comments, inline review comments, and check-run conclusions into a single JSON object. Use it from a review session instead of shelling out to `gh api` / `gh pr view` 10+ times for the same data. No LLM calls.
- `check-config-keys.py` — **deterministic** sanity checker. Enumerates leaf paths in `auto_tune_config.yml` and searches `scripts/`, `.github/workflows/`, and `run.sh` for readers. Fails if any key has no consumer. No LLM calls.
- `hub-daily-sweep-prompt.md` — the prompt used by the hub-daily-sweep workflow to decide which merged PRs are generalizable.
- `hub/hub-open-proposal.py` — **deterministic** helper that opens a proposal issue in the hub repo for a given merged PR. No LLM calls.
- `hub/hub-search.py` — **deterministic** helper that searches existing hub proposals to avoid duplicates. No LLM calls.
- `hub/list-merged-prs.py` — **deterministic** helper that lists PRs merged into `main` within a lookback window. No LLM calls.
- All LLM-side reasoning over the output of the parser scripts is handled by the `workflow-insights-extractor` subagent at [`.claude/agents/workflow-insights-extractor.md`](https://github.com/damien-robotsix/claude_auto_tune/blob/main/.claude/agents/workflow-insights-extractor.md), which the auto-improve tracker invokes via the Task tool.
