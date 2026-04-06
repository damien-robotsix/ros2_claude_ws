---
title: Quick start
layout: default
---

# Quick start

There are two ways to use this workspace: locally through Docker, or in CI via the Claude GitHub App.

## Local (Docker)

```bash
./run.sh
```

This builds and runs Claude in a Docker container with `--dangerously-skip-permissions` for autonomous operation. Auth state persists in `.claude-home/` on the host, so you only need to authenticate once.

What `run.sh` does:

1. Builds the `claude_auto_tune-claude` image from the repo `Dockerfile` (via `docker compose build`).
2. Ensures the host-side bind-mount directories exist under `.claude-home/`.
3. Runs the container interactively with the workspace mounted at `/workspace`.
4. After the container exits, publishes local Claude Code session transcripts to the hub if the `hub.local_transcripts.enabled` flag is set in `auto_tune_config.yml`. Failures are non-fatal and never mask the session exit status.

You can pass extra flags straight through to the Claude CLI:

```bash
./run.sh --help
```

## CI (GitHub Actions)

First, authenticate with GitHub using the required scopes:

```bash
gh auth login -h github.com -s repo,workflow
```

Then, from inside the Claude Code CLI:

```
/install-github-app
```

This installs the Claude GitHub App and configures the `ANTHROPIC_API_KEY` secret in the repo. Once installed, mention `@claude` in any issue or PR comment to trigger a run.

## Hub setup (optional)

The hub is a **private** repo that enables two features:

- **Cross-workspace improvement sharing** — the daily sweep workflow publishes generalizable fixes as proposal issues in the hub, visible to all forks.
- **Local transcript ingestion** — session transcripts from local Docker runs are published to the hub and consumed by the auto-improve loop in CI, so local-run signals feed the self-tuning system alongside CI signals.

Both features are optional and off by default. Skip this section if you don't need them.

### 1. Create the hub repo

```bash
gh repo create <your-org>/claude-auto-tune-hub --private --description "Shared hub for claude_auto_tune workspaces"
```

Add a minimal `README.md` so the repo is not empty:

```bash
cd $(mktemp -d)
git init && git remote add origin "https://github.com/<your-org>/claude-auto-tune-hub.git"
echo "# claude-auto-tune-hub" > README.md
git add README.md && git commit -m "init" && git push -u origin main
cd -
```

### 2. Create a fine-grained PAT

Go to **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens** and create a token scoped to the hub repo with these permissions:

| Permission | Level | Used by |
|---|---|---|
| `contents` | Read and write | transcript push (local) + transcript fetch (CI) |
| `issues` | Read and write | daily sweep proposals (CI) |

Set the expiration to the maximum allowed (typically 1 year) — you will need to rotate it when it expires.

### 3. Configure the workspace

Edit `auto_tune_config.yml`:

```yaml
hub:
  enabled: true
  repo: "<your-org>/claude-auto-tune-hub"
  local_transcripts:
    enabled: true
```

### 4. Add the `HUB_TOKEN` repository secret

Go to your workspace repo's **Settings → Secrets and variables → Actions** and add:

| Secret name | Value |
|---|---|
| `HUB_TOKEN` | The fine-grained PAT from step 2 |

This is used by CI workflows (`auto-improve-discover`, `auto-improve-verify`, `hub-daily-sweep`) to interact with the hub. The token is scoped per workflow step and is never exposed to the Claude Code LLM step.

### 5. Authenticate locally for transcript push

The local push script (`scripts/hub/push-local-transcripts.py`, wired into `run.sh`) uses the `gh` CLI's credentials to push to the hub. Make sure your local `gh` session has access to the hub repo:

```bash
gh auth login -h github.com
gh repo view <your-org>/claude-auto-tune-hub  # verify access
```

That's it. After the next `./run.sh` session, transcripts are automatically published to the hub. The weekly auto-improve-discover workflow will include them in its next clustering pass.

## Requirements

- Docker (for local runs)
- `gh` CLI (for the GitHub App setup step and hub push)
- A valid Anthropic API key, stored as the `ANTHROPIC_API_KEY` repo secret for CI runs
- (Optional) A `HUB_TOKEN` repo secret for hub integration — see [Hub setup](#hub-setup-optional)
