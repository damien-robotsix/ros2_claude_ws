#!/usr/bin/env python3
"""Sanity check: every key in auto_tune_config.yml has at least one reader.

Enumerates the leaf paths of auto_tune_config.yml and searches for each
across scripts/, .github/workflows/, and run.sh. Fails loudly if any key
has no reader — that is the class of bug tracked in issue #51, where
keys like auto_improve.default_conversation_limit sat in the config but
were silently ignored because the workflow hardcoded a literal fallback.

Matching rules (lenient, to avoid false positives):
  1. The fully dotted leaf path (e.g. `models.claude_code`) appears
     verbatim in a searched file — normal yq/script access.
  2. An ancestor path is read with a dynamic key (e.g.
     `model_aliases."$alias"`) — treat all children of that ancestor as
     used, since they are looked up indirectly.

Requires PyYAML (installed via python3-yaml in the Docker image; add
`pip install pyyaml` to the CI workflow step that runs this script).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "auto_tune_config.yml"
SEARCH_ROOTS = [
    REPO / "scripts",
    REPO / ".github" / "workflows",
    REPO / "run.sh",
]


def leaf_paths(node, prefix: str = "") -> list[str]:
    """Return dotted paths for every leaf in the parsed config.

    A leaf is a scalar, None, an empty mapping, or an empty list.
    Non-empty mappings recurse; non-empty lists are themselves treated
    as a single leaf (individual items rarely have standalone meaning
    for the "is this key read?" check).
    """
    if isinstance(node, dict) and node:
        out: list[str] = []
        for key, value in node.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            out.extend(leaf_paths(value, child_prefix))
        return out
    return [prefix] if prefix else []


def gather_blob(roots: list[Path]) -> str:
    parts: list[str] = []
    for root in roots:
        if root.is_file():
            parts.append(root.read_text(errors="replace"))
        elif root.is_dir():
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(root)
                if any(part.startswith((".", "__")) for part in rel.parts):
                    continue
                try:
                    parts.append(p.read_text(errors="replace"))
                except OSError:
                    pass
    return "\n".join(parts)


def dynamic_parents(blob: str) -> set[str]:
    """Return paths that are read with a dynamic key.

    Matches yq/jq-style `parent.child."$var"` and bash-style
    `parent.child[$var]`. The captured parent's children should be
    treated as used even though their literal dotted paths never
    appear in the source.
    """
    parents: set[str] = set()
    for m in re.finditer(r'([A-Za-z_][\w.]*)\."\$', blob):
        parents.add(m.group(1).lstrip("."))
    for m in re.finditer(r'([A-Za-z_][\w.]*)\[\$', blob):
        parents.add(m.group(1).lstrip("."))
    return parents


def main() -> int:
    if not CONFIG.exists():
        print(f"config not found: {CONFIG}", file=sys.stderr)
        return 2

    config = yaml.safe_load(CONFIG.read_text()) or {}
    leaves = sorted(set(leaf_paths(config)))
    blob = gather_blob(SEARCH_ROOTS)
    parents = dynamic_parents(blob)

    dead: list[str] = []
    for path in leaves:
        if path in blob:
            continue
        if any(path == p or path.startswith(p + ".") for p in parents):
            continue
        dead.append(path)

    if dead:
        print("Dead keys detected in auto_tune_config.yml (no reader found):")
        for p in dead:
            print(f"  - {p}")
        print()
        searched = ", ".join(str(r.relative_to(REPO)) for r in SEARCH_ROOTS)
        print(f"Searched: {searched}")
        print("Either wire the key into a reader or remove it from the config.")
        return 1

    print(f"OK: all {len(leaves)} config leaves have at least one reader.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
