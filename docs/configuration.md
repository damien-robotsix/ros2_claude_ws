---
title: Configuration
layout: default
---

# Configuration

Most workspace-level tuning lives in [`auto_tune_config.yml`](https://github.com/damien-robotsix/claude_auto_tune/blob/main/auto_tune_config.yml) at the repo root. Workflows and scripts read it to decide which model to use and how the auto-improve loop behaves.

## Models

The `models` section assigns a model family to each entry point:

```yaml
models:
  claude_code: "opus"          # main claude.yml workflow
  code_review: "opus"          # code-review workflow
  auto_improve: "opus"         # auto-improve discover workflow
  auto_improve_verify: "opus"  # per-issue auto-improve verify workflow
```

You can use a short alias (`haiku`, `sonnet`, `opus`) or pin a full model ID (for example `claude-sonnet-4-6`).

## Model aliases

The `model_aliases` section maps short names to specific model IDs. Update this section when new model versions are released:

```yaml
model_aliases:
  haiku:  "claude-haiku-4-5-20251001"
  sonnet: "claude-sonnet-4-6"
  opus:   "claude-opus-4-6"
```

Scripts fall back to built-in defaults if this section is missing.

## Auto-improve loop

```yaml
auto_improve:
  default_conversation_limit: 20
```

- `auto_improve.default_conversation_limit` — maximum number of Claude Code session transcripts the discover workflow analyzes in a single pass. Workflow logs are always parsed in full (no cap).

The discover and verify workflow crons are hardcoded in their respective workflow files (`.github/workflows/auto-improve-discover.yml` and `.github/workflows/auto-improve-verify.yml`) because GitHub Actions' `on.schedule` only accepts literal values and cannot be templated from config.

## Issue tracking

The split auto-improve design has no tunable verification threshold. The
verify workflow filters signals to workflow runs created after the fix
PR merged (`WINDOW_START = pr.mergedAt`), which makes the per-issue
before/after comparison inherently strict: if zero in-window matches for
the fingerprint are found, the issue is closed on that single clean
verify run. Regressions after closure are caught by the daily verify
cron and automatically reopen the issue.

## Hub — cross-workspace sharing

The `hub:` section configures the shared [`claude-auto-tune-hub`](https://github.com/damien-robotsix/claude-auto-tune-hub) repo, which is reused by multiple independent lanes:

```yaml
hub:
  enabled: true
  repo: "damien-robotsix/claude-auto-tune-hub"
  local_transcripts:
    enabled: true
```

- `hub.enabled` — master switch for every lane. When false, all hub scripts and workflows exit without touching anything.
- `hub.repo` — slug of the shared hub repo. One repo hosts multiple disjoint data lanes (each lane owns its own directory tree or label set inside the hub).
- `hub.local_transcripts.enabled` — independent switch for the local-transcript lane. When true, the **push side** ([`scripts/hub/push-local-transcripts.py`](https://github.com/damien-robotsix/claude_auto_tune/blob/main/scripts/hub/push-local-transcripts.py)) copies new Claude Code session transcripts from `.claude-home/.claude/projects/` into `transcripts/<workspace-slug>/<YYYY-MM-DD>/` in the hub, and the **pull side** ([`scripts/hub/fetch-local-transcripts.py`](https://github.com/damien-robotsix/claude_auto_tune/blob/main/scripts/hub/fetch-local-transcripts.py)) fetches them into `.scratch/hub-transcripts/` during CI so the `workflow-insights-extractor` can fold local-run signals into clustering. Defaults to `false` so both sides are a no-op until you opt in.

### `HUB_TOKEN` Actions secret

All hub-interacting CI workflows require a `HUB_TOKEN` repository secret — a fine-grained PAT scoped to the hub repo with **`contents: read`** + **`issues: write`** permissions. The token is used by:

- **`auto-improve-discover.yml` / `auto-improve-verify.yml`** — to fetch local transcripts from the hub (`contents: read`).
- **`hub-daily-sweep.yml`** — to create proposal issues and manage labels in the hub (`issues: write`).

Each fork owner provisions this themselves:

1. Create a fine-grained PAT at **Settings → Developer settings → Personal access tokens → Fine-grained tokens**, scoped to the hub repo with `contents: read` and `issues: write` permissions.
2. Add it as a repository secret named `HUB_TOKEN` under **Settings → Secrets and variables → Actions** on your workspace fork.

When the secret is missing, workflows emit a `::warning::` annotation in the job summary and continue without hub interaction — no local-run signals are included and no proposals are created, but nothing breaks. The token is scoped to specific workflow steps and is **never** passed to the Claude Code step.

The hub-daily-sweep cron is hardcoded in `.github/workflows/hub-daily-sweep.yml` (same reason as the auto-improve crons — `on.schedule` cannot be templated from config). Proposal lifetime enforcement lives in the hub repo itself.

The two lanes share only the hub repo and the `hub.enabled` master switch. They have disjoint directory trees, disjoint scripts, and independent opt-in flags — adopting one does not commit you to the other.

## Claude settings

Per-workspace Claude Code settings (tools, permissions, etc.) live in [`.claude/settings.json`](https://github.com/damien-robotsix/claude_auto_tune/blob/main/.claude/settings.json) and are tracked in git so they apply to every contributor and CI run.
