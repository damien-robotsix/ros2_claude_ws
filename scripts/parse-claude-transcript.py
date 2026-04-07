#!/usr/bin/env python3
"""
Deterministic Claude Code session-transcript signal extractor.

Claude Code saves session transcripts as JSONL files under::

    ~/.claude/projects/<encoded-path>/<session-id>.jsonl

Each line is a JSON object representing one turn (user, assistant, or tool
result). This script walks one or more such files and emits a structured
JSON summary of tool-call activity: total counts, top tools, failed tools,
repeated consecutive runs, token usage, and a short sequence preview.

It is **pure aggregation** — no LLM calls, no credentials, no network. All
reasoning over this summary is the job of the ``workflow-insights-extractor``
subagent that calls this script.

Usage::

    cat ~/.claude/projects/**/*.jsonl | python3 scripts/parse-claude-transcript.py
    python3 scripts/parse-claude-transcript.py <transcript-file.jsonl>
    python3 scripts/parse-claude-transcript.py <dir-of-jsonl-files/>
"""

import json
import pathlib
import sys
from collections import Counter


# Cap on how many items of each list we include in the output. Counts are
# always exact; samples are truncated so the downstream subagent prompt
# stays small.
TOP_N = 20
SEQUENCE_PREVIEW_LEN = 100


def _classify_error(error_text: str) -> str:
    """Classify an error as 'controllable' or 'uncontrollable'.

    Controllable errors are ones that prompt or code changes can fix:
    tool misuse, bad arguments, edit conflicts, missing reads, etc.

    Uncontrollable errors are external: network timeouts, HTTP 429/5xx,
    auth failures, rate limits, DNS resolution, and similar transient
    infrastructure issues.
    """
    if not error_text:
        return "controllable"
    lower = error_text.lower()
    uncontrollable_patterns = (
        "timeout", "timed out", "connection refused", "connection reset",
        "network", "dns", "resolve host",
        "rate limit", "rate_limit", "429", "too many requests",
        "401", "403", "unauthorized", "forbidden", "auth",
        "500", "502", "503", "504", "internal server error",
        "bad gateway", "service unavailable", "gateway timeout",
        "ssl", "certificate", "eof", "broken pipe",
    )
    for pat in uncontrollable_patterns:
        if pat in lower:
            return "uncontrollable"
    return "controllable"


def extract_tool_calls(lines: list[str]) -> dict:
    """Walk JSONL lines and return a structured activity summary."""
    tool_counter: Counter = Counter()
    error_tools: list[str] = []
    controllable_errors: list[str] = []
    uncontrollable_errors: list[str] = []
    tool_sequences: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # Claude Code JSONL wraps messages:
        #   {"type": "assistant", "message": {...}}
        # Fall back to top-level role/content for older formats.
        msg = entry.get("message", entry)
        role = msg.get("role", entry.get("type", ""))
        content = msg.get("content", [])

        if isinstance(content, str):
            content = [{"type": "text", "text": content}]

        usage = msg.get("usage") or entry.get("usage", {})
        if usage:
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

        if role == "assistant":
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "unknown")
                    tool_counter[name] += 1
                    tool_sequences.append(name)

        elif role in ("tool", "user"):
            # Tool results are delivered as either role="tool" (older) or
            # role="user" with tool_result blocks (current). Detect errors
            # either way.
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if block.get("is_error") and tool_sequences:
                        tool_name = tool_sequences[-1]
                        error_tools.append(tool_name)
                        error_content = block.get("content", "")
                        if isinstance(error_content, list):
                            error_content = " ".join(
                                b.get("text", "") for b in error_content
                                if isinstance(b, dict)
                            )
                        category = _classify_error(str(error_content))
                        if category == "controllable":
                            controllable_errors.append(tool_name)
                        else:
                            uncontrollable_errors.append(tool_name)

    # Repeated consecutive-run detection: runs of 3+ identical calls in a
    # row are a strong signal that a loop could be replaced by a single
    # deterministic script.
    repeated: list[dict] = []
    i = 0
    while i < len(tool_sequences):
        j = i
        while j < len(tool_sequences) and tool_sequences[j] == tool_sequences[i]:
            j += 1
        run_len = j - i
        if run_len >= 3:
            repeated.append({"tool": tool_sequences[i], "run_length": run_len, "start_index": i})
        i = j

    error_counter = Counter(error_tools)
    controllable_counter = Counter(controllable_errors)
    uncontrollable_counter = Counter(uncontrollable_errors)

    preview = tool_sequences[:SEQUENCE_PREVIEW_LEN]
    sequence_preview = " → ".join(preview)
    if len(tool_sequences) > SEQUENCE_PREVIEW_LEN:
        sequence_preview += f" … (+{len(tool_sequences) - SEQUENCE_PREVIEW_LEN} more)"

    return {
        "tool_call_count": sum(tool_counter.values()),
        "top_tools": [t for t, _ in tool_counter.most_common(5)],
        "tool_counts": dict(tool_counter.most_common(TOP_N)),
        "error_tools": dict(error_counter.most_common(TOP_N)),
        "controllable_errors": dict(controllable_counter.most_common(TOP_N)),
        "uncontrollable_errors": dict(uncontrollable_counter.most_common(TOP_N)),
        "repeated_sequences": repeated[:TOP_N],
        "token_usage": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        },
        "tool_sequence_preview": sequence_preview,
    }


def collect_jsonl_lines(source: str) -> list[str]:
    """Collect all JSONL lines from a file, directory, or stdin sentinel."""
    p = pathlib.Path(source)
    if p.is_dir():
        lines: list[str] = []
        for jf in sorted(p.rglob("*.jsonl")):
            lines.extend(jf.read_text(errors="replace").splitlines())
        return lines
    if p.is_file():
        return p.read_text(errors="replace").splitlines()
    return []


def _is_subagent_path(path: pathlib.Path) -> bool:
    """Return True if *path* lives under a ``subagents/`` directory."""
    return "subagents" in path.parts


def _parent_session_id(path: pathlib.Path) -> str | None:
    """Extract the parent session UUID from a subagent file path.

    Claude Code stores subagent transcripts at::

        <session-uuid>/subagents/agent-<id>.jsonl

    Returns the session UUID or ``None`` if the path doesn't match.
    """
    parts = path.parts
    try:
        idx = parts.index("subagents")
    except ValueError:
        return None
    if idx > 0:
        return parts[idx - 1]
    return None


def collect_files_by_role(source: str) -> tuple[list[str], dict[str, list[str]]]:
    """Collect JSONL lines split into parent and per-subagent buckets.

    Returns (parent_lines, {subagent_id: lines}).
    """
    p = pathlib.Path(source)
    parent_lines: list[str] = []
    subagent_map: dict[str, list[str]] = {}

    if p.is_dir():
        for jf in sorted(p.rglob("*.jsonl")):
            lines = jf.read_text(errors="replace").splitlines()
            if _is_subagent_path(jf):
                subagent_map[jf.stem] = lines
            else:
                parent_lines.extend(lines)
    elif p.is_file():
        lines = p.read_text(errors="replace").splitlines()
        if _is_subagent_path(p):
            subagent_map[p.stem] = lines
        else:
            parent_lines = lines

    return parent_lines, subagent_map


def build_subagent_summary(
    subagent_map: dict[str, list[str]],
    parent_session_ids: dict[str, str | None],
) -> list[dict]:
    """Build a per-subagent breakdown list for the output."""
    summaries: list[dict] = []
    for agent_id, lines in sorted(subagent_map.items()):
        stats = extract_tool_calls(lines)
        entry: dict = {
            "agent_id": agent_id,
            "tool_call_count": stats["tool_call_count"],
            "tool_counts": stats["tool_counts"],
            "error_tools": stats["error_tools"],
            "token_usage": stats["token_usage"],
        }
        psid = parent_session_ids.get(agent_id)
        if psid is not None:
            entry["parent_session_id"] = psid
        summaries.append(entry)
    return summaries


def main() -> None:
    if len(sys.argv) > 1:
        all_lines: list[str] = []
        all_subagents: dict[str, list[str]] = {}
        parent_session_ids: dict[str, str | None] = {}
        for arg in sys.argv[1:]:
            p = pathlib.Path(arg)
            if p.is_dir() or (p.is_file() and _is_subagent_path(p)):
                parent, subs = collect_files_by_role(arg)
                all_lines.extend(parent)
                all_subagents.update(subs)
                # Resolve parent session IDs from file paths
                if p.is_dir():
                    for jf in sorted(p.rglob("*.jsonl")):
                        if _is_subagent_path(jf):
                            parent_session_ids[jf.stem] = _parent_session_id(jf)
                elif _is_subagent_path(p):
                    parent_session_ids[p.stem] = _parent_session_id(p)
            else:
                all_lines.extend(collect_jsonl_lines(arg))
    else:
        all_lines = sys.stdin.read().splitlines()
        all_subagents = {}
        parent_session_ids = {}

    if not any(line.strip() for line in all_lines) and not all_subagents:
        print(json.dumps({
            "tool_call_count": 0,
            "top_tools": [],
            "tool_counts": {},
            "error_tools": {},
            "controllable_errors": {},
            "uncontrollable_errors": {},
            "repeated_sequences": [],
            "token_usage": {"input_tokens": 0, "output_tokens": 0},
            "tool_sequence_preview": "",
            "subagent_summary": [],
            "note": "empty transcript",
        }))
        return

    result = extract_tool_calls(all_lines)

    if all_subagents:
        result["subagent_summary"] = build_subagent_summary(
            all_subagents, parent_session_ids
        )
    else:
        result["subagent_summary"] = []

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
