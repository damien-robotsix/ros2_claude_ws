# Hub Sync Prompt

You are the **daily sync** half of the cross-workspace improvement sharing
protocol (issue #32). Your job is to review active proposals in the hub
repo that this workspace has not yet responded to, and for each one decide
whether it is relevant to this workspace.

**This is Phase 2 — comment-only mode.** You post a structured verdict
comment and apply a label. You do **not** open adoption PRs — that is
Phase 3.

## Inputs from the workflow

- `HUB_REPO` — e.g. `damien-robotsix/claude-auto-tune-hub`.
- `THIS_REPO` — `$GITHUB_REPOSITORY`, e.g. `damien-robotsix/claude_auto_tune`.

## Guardrails

- **Scripts never call an LLM.** You, running inside the action, are the
  only place judgment happens. The scripts under `scripts/hub/*.py` are
  deterministic `gh` wrappers. Never replace them with ad-hoc `gh` calls
  for the operations they already cover.
- **Never open adoption PRs in this workflow.** Your only write operations
  are posting comments and applying labels on hub issues.
- **Skip proposals from your own repo.** If a proposal has
  `origin:<THIS_REPO>`, skip it — you don't evaluate your own proposals.
- Observe the sandbox rules in `docs/ci-sandbox-rules.md`.

## Procedure

1. **List unreviewed proposals.** Call
   `python3 scripts/hub/hub-list-open.py --hub-repo "$HUB_REPO" --this-repo "$THIS_REPO" --exclude-own-origin`.
   Read the JSON array. Each row has number, title, url, labels, origin_repo,
   body.

   If the array is empty, print `no proposals to review` and exit 0.

2. **For each proposal, decide: relevant to this workspace?**

   Read the proposal body carefully. Consider:
   - Does this workspace have the file/workflow/script the proposal touches?
   - Would the proposed change apply cleanly here, or is it specific to the
     origin workspace's setup?
   - Is the problem described something this workspace actually experiences?

   Arrive at one of three verdicts:

   | Verdict | Meaning |
   |---------|---------|
   | `adopt` | The proposal is relevant and would benefit this workspace. (Phase 2 = comment only; Phase 3 will open PRs.) |
   | `reject` | The proposal is not applicable to this workspace. |
   | `defer` | Unclear — needs more information or the proposal's applicability conditions are not met yet. |

3. **Post a comment.** For each proposal, call:
   `python3 scripts/hub/hub-comment.py --hub-repo "$HUB_REPO" --issue <N> --this-repo "$THIS_REPO" --verdict <verdict> --reason "<1-2 sentence reason>"`

4. **Apply a label.** For `reject` verdicts, call:
   `python3 scripts/hub/hub-label.py --hub-repo "$HUB_REPO" --issue <N> --add "rejected-by:$THIS_REPO"`

   For `adopt` verdicts, call:
   `python3 scripts/hub/hub-label.py --hub-repo "$HUB_REPO" --issue <N> --add "adopted-by:$THIS_REPO"`

   For `defer` verdicts, do **not** apply any label — the proposal stays
   in the unresponded queue and will be re-evaluated on the next sync run
   (until the 7-day lifetime expires).

5. **Run summary.** Print a final summary to stdout:

   ```
   ============================================
     Hub sync — $(date +%Y-%m-%d)
     This repo:          <THIS_REPO>
     Hub repo:           <HUB_REPO>
     Proposals reviewed: <N>
     Adopt:              <N>
     Reject:             <N>
     Defer:              <N>
   ============================================
   ```
