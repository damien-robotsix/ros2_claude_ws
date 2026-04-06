---
title: Claude Auto Tune
layout: default
---

# Claude Auto Tune

A self-improving [Claude Code](https://claude.com/claude-code) workspace. Run Claude locally in Docker for safe autonomous operation, or through CI via the official `anthropics/claude-code-action@v1` GitHub Action.

The workspace is designed to grow incrementally — start simple, add tooling as patterns emerge — and to periodically tune its own configuration, prompts, and scripts based on accumulated run history.

## Documentation

- [Quick start](quickstart.md) — run Claude locally or wire it into CI.
- [Configuration](configuration.md) — `auto_tune_config.yml`, model aliases, and workspace settings.
- [Workflows](workflows.md) — the CI workflows (`claude`, `claude-code-review`, `auto-improve`) and what each does.
- [Architecture](architecture.md) — repo layout and how the pieces fit together.

## Source

The project lives at [damien-robotsix/claude_auto_tune](https://github.com/damien-robotsix/claude_auto_tune).
