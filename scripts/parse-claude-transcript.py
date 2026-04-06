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


def extract_tool_calls(lines: list[str]) -> dict:
    """Walk JSONL lines and return a structured activity summary."""
    tool_counter: Counter = Counter()
    error_tools: list[str] = []
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
                        error_tools.append(tool_sequences[-1])

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

    preview = tool_sequences[:SEQUENCE_PREVIEW_LEN]
    sequence_preview = " → ".join(preview)
    if len(tool_sequences) > SEQUENCE_PREVIEW_LEN:
        sequence_preview += f" … (+{len(tool_sequences) - SEQUENCE_PREVIEW_LEN} more)"

    return {
        "tool_call_count": sum(tool_counter.values()),
        "top_tools": [t for t, _ in tool_counter.most_common(5)],
        "tool_counts": dict(tool_counter.most_common(TOP_N)),
        "error_tools": dict(error_counter.most_common(TOP_N)),
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


def main() -> None:
    if len(sys.argv) > 1:
        all_lines: list[str] = []
        for arg in sys.argv[1:]:
            all_lines.extend(collect_jsonl_lines(arg))
    else:
        all_lines = sys.stdin.read().splitlines()

    if not any(line.strip() for line in all_lines):
        print(json.dumps({
            "tool_call_count": 0,
            "top_tools": [],
            "tool_counts": {},
            "error_tools": {},
            "repeated_sequences": [],
            "token_usage": {"input_tokens": 0, "output_tokens": 0},
            "tool_sequence_preview": "",
            "note": "empty transcript",
        }))
        return

    result = extract_tool_calls(all_lines)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
