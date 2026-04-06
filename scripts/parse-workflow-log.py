#!/usr/bin/env python3
"""
Deterministic workflow-log signal extractor.

Reads a GitHub Actions workflow log from stdin (or a file passed as argument)
and emits a structured JSON summary of signals relevant to the
auto-improvement tracker. This script is **pure regex/pattern extraction** —
it never calls an LLM, never requires credentials, and never fails open on
missing dependencies. All reasoning over these signals is the job of the
``workflow-insights-extractor`` subagent that calls this script.

Usage:
    gh run view <run-id> --log | python3 scripts/parse-workflow-log.py
    python3 scripts/parse-workflow-log.py <log-file>

Output: a single JSON object on stdout with the shape documented in
``EXPECTED_SCHEMA`` below.
"""

import json
import re
import sys
from collections import Counter
from typing import Iterable


EXPECTED_SCHEMA = {
    "log_bytes": "int — total bytes read",
    "line_count": "int — total lines",
    "counts": "dict[str, int] — total occurrences per signal category",
    "signals": "dict[str, list[{line, text}]] — up to SAMPLE_CAP examples per category",
}

# Cap how many sample lines we keep per signal category. Counts are always
# exact; samples are truncated so the downstream subagent prompt stays small.
SAMPLE_CAP = 20

# Max characters of any single log line we echo back in the JSON. GitHub
# Actions logs can contain very long single lines (e.g. uploaded base64
# artifacts); trimming keeps the output predictable.
LINE_CHAR_CAP = 500


# --- pattern definitions ---------------------------------------------------
#
# Each entry is (name, compiled_regex). Order does not matter for correctness
# but is preserved in the output for stable diffs.

PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # Hard failures and generic errors. We deliberately match common casings
    # and exclude lines that merely contain the word "error" as part of a
    # noun phrase (e.g. "error_count: 0") by requiring a colon, "!", or
    # "failed" nearby.
    (
        "errors",
        re.compile(
            r"(?i)\b(?:error|fatal|failed|failure|traceback)\b"
            r"(?:\s*[:!]|\s+\w)",
        ),
    ),
    # Bash allowlist / tool-permission denials emitted by the claude-code
    # harness. These are the single most actionable signal for this repo.
    (
        "tool_denied",
        re.compile(
            r"(?i)("
            r"command requires approval"
            r"|not (?:in|on) the allowed tools? list"
            r"|tool use was blocked"
            r"|permission denied by sandbox"
            r"|bash command .* (?:blocked|rejected|denied)"
            r")"
        ),
    ),
    # The specific "GitHub App token lacks workflows permission" rejection.
    (
        "workflow_permission_rejected",
        re.compile(
            r"refusing to allow a (?:GitHub App|OAuth App) to create or update workflow",
            re.IGNORECASE,
        ),
    ),
    # HTTP client/server errors surfaced by gh/curl/requests.
    (
        "http_errors",
        re.compile(r"\bHTTP[/ ]?\d\.?\d?\s+(4\d{2}|5\d{2})\b|\b(?:status|code)[:= ]\s*(4\d{2}|5\d{2})\b"),
    ),
    # Explicit non-zero exit code reports.
    (
        "exit_codes_nonzero",
        re.compile(r"(?i)(?:exit(?:ed)?(?:\s+with)?\s+(?:code\s+)?|status\s+)(?!0\b)\d+"),
    ),
    # Retry markers.
    (
        "retries",
        re.compile(r"(?i)\b(?:retry|retrying|attempt\s+\d+\s+of\s+\d+|backing off)\b"),
    ),
    # Timeouts / cancellations.
    (
        "timeouts",
        re.compile(r"(?i)\b(?:timed? ?out|timeout|deadline exceeded|cancell?ed)\b"),
    ),
    # Rate limiting.
    (
        "rate_limited",
        re.compile(r"(?i)\b(?:rate[- ]?limit(?:ed|ing)?|too many requests|429)\b"),
    ),
]


def _clean_line(raw: str) -> str:
    """Trim a log line for inclusion in the JSON output.

    GitHub Actions prefixes each line with a timestamp and stream marker
    (e.g. ``2025-10-05T12:34:56.789Z my-step\t...``). We preserve the line
    as-is but cap its length so a single pathological line cannot blow up
    the output.
    """
    s = raw.rstrip("\n")
    if len(s) > LINE_CHAR_CAP:
        s = s[:LINE_CHAR_CAP] + "…"
    return s


def extract_signals(lines: Iterable[str]) -> dict:
    counts: Counter = Counter()
    samples: dict[str, list[dict]] = {name: [] for name, _ in PATTERNS}
    line_count = 0
    log_bytes = 0

    for lineno, raw in enumerate(lines, start=1):
        line_count = lineno
        log_bytes += len(raw)
        for name, pat in PATTERNS:
            if pat.search(raw):
                counts[name] += 1
                if len(samples[name]) < SAMPLE_CAP:
                    samples[name].append({"line": lineno, "text": _clean_line(raw)})

    return {
        "log_bytes": log_bytes,
        "line_count": line_count,
        "counts": {name: counts.get(name, 0) for name, _ in PATTERNS},
        "signals": samples,
    }


def main() -> None:
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", errors="replace") as f:
            result = extract_signals(f)
    else:
        result = extract_signals(sys.stdin)

    if result["line_count"] == 0:
        # Preserve a stable shape so downstream consumers can always
        # ``.counts`` without guarding against empties.
        result["note"] = "empty log"

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
