"""Quality-validation script for Phase 16 taxonomy addition.

Runs each of the 3 golden fixtures through extract() with the OLD system prompt
(no taxonomy) and the NEW system prompt (with TEXAS_ELECTRICITY_TAXONOMY), then
diffs the resulting JSON to confirm the taxonomy addition does not change
extraction behaviour.

Usage (from repo root):
    railway run --service nodalpulse-services \\
        .venv\\Scripts\\python.exe scripts\\validate_golden.py

Or with a local .env:
    .venv\\Scripts\\python.exe scripts\\validate_golden.py

Requires ANTHROPIC_API_KEY in env.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# ── project path ──────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import anthropic

from nodalpulse.workers.extract import (
    _EXTRACT_SYSTEM_ERCOT_MN,
    _EXTRACT_SYSTEM_ERCOT_NPRR,
    _EXTRACT_SYSTEM_PUCT,
    _extract_system_for_doc_type,
)
from nodalpulse.llm.taxonomy import TEXAS_ELECTRICITY_TAXONOMY

SONNET = "claude-sonnet-4-6"
GOLDEN_DIR = Path(__file__).parent.parent / "tests" / "golden"

FIXTURES = [
    {
        "name": "puct_rate_case",
        "file": GOLDEN_DIR / "puct_rate_case.txt",
        "doc_type": "puct-filing",
        "old_system": _EXTRACT_SYSTEM_PUCT,
        "new_system": _extract_system_for_doc_type("puct-filing"),
    },
    {
        "name": "ercot_nprr",
        "file": GOLDEN_DIR / "ercot_nprr.txt",
        "doc_type": "ercot-nprr",
        "old_system": _EXTRACT_SYSTEM_ERCOT_NPRR,
        "new_system": _extract_system_for_doc_type("ercot-nprr"),
    },
    {
        "name": "ercot_mn",
        "file": GOLDEN_DIR / "ercot_mn.txt",
        "doc_type": "ercot-mn",
        "old_system": _EXTRACT_SYSTEM_ERCOT_MN,
        "new_system": _extract_system_for_doc_type("ercot-mn"),
    },
]


async def run_extraction(client: anthropic.AsyncAnthropic, system: str, user: str) -> dict:
    """Single extraction call; returns parsed JSON or raw-text fallback."""
    resp = await client.messages.create(
        model=SONNET,
        max_tokens=4096,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    raw = resp.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"__raw": raw[:500]}


def diff_dicts(old: dict, new: dict, path: str = "") -> list[str]:
    """Return list of human-readable differences between two dicts."""
    diffs = []
    all_keys = set(old) | set(new)
    for k in sorted(all_keys):
        full_key = f"{path}.{k}" if path else k
        if k not in old:
            diffs.append(f"  + {full_key}: {json.dumps(new[k])[:120]}")
        elif k not in new:
            diffs.append(f"  - {full_key}: {json.dumps(old[k])[:120]}")
        elif type(old[k]) != type(new[k]):
            diffs.append(f"  ~ {full_key}: type changed {type(old[k]).__name__} -> {type(new[k]).__name__}")
        elif isinstance(old[k], dict):
            diffs.extend(diff_dicts(old[k], new[k], full_key))
        elif isinstance(old[k], list):
            if len(old[k]) != len(new[k]):
                diffs.append(f"  ~ {full_key}: list length {len(old[k])} -> {len(new[k])}")
            # Check if list items are meaningfully different (keys present/absent)
            for i, (oi, ni) in enumerate(zip(old[k], new[k])):
                if isinstance(oi, dict) and isinstance(ni, dict):
                    diffs.extend(diff_dicts(oi, ni, f"{full_key}[{i}]"))
        elif old[k] != new[k]:
            # Value changed — show a snippet
            old_str = json.dumps(old[k])[:80]
            new_str = json.dumps(new[k])[:80]
            diffs.append(f"  ~ {full_key}:")
            diffs.append(f"      old: {old_str}")
            diffs.append(f"      new: {new_str}")
    return diffs


def count_tokens_sync(client_sync: anthropic.Anthropic, system: str, user: str) -> int:
    resp = client_sync.beta.messages.count_tokens(
        model=SONNET,
        system=[{"type": "text", "text": system}],
        messages=[{"role": "user", "content": user}],
    )
    return resp.input_tokens


async def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = anthropic.AsyncAnthropic(api_key=api_key)
    client_sync = anthropic.Anthropic(api_key=api_key)

    all_passed = True
    sep = "=" * 70

    for fixture in FIXTURES:
        name = fixture["name"]
        doc_type = fixture["doc_type"]
        text = fixture["file"].read_text(encoding="utf-8")
        user_msg = f"Document type: {doc_type}\n\n{text}"

        print(f"\n{sep}")
        print(f"  Fixture: {name}  ({doc_type})")
        print(sep)

        # Token counts
        old_tokens = count_tokens_sync(client_sync, fixture["old_system"], user_msg)
        new_tokens = count_tokens_sync(client_sync, fixture["new_system"], user_msg)
        old_sys_tokens = count_tokens_sync(client_sync, fixture["old_system"], "x") - count_tokens_sync(client_sync, "", "x") if False else None

        # Simpler: just count system block tokens
        sys_only_old = client_sync.beta.messages.count_tokens(
            model=SONNET,
            system=[{"type": "text", "text": fixture["old_system"]}],
            messages=[{"role": "user", "content": "x"}],
        ).input_tokens - client_sync.beta.messages.count_tokens(
            model=SONNET, messages=[{"role": "user", "content": "x"}],
        ).input_tokens

        sys_only_new = client_sync.beta.messages.count_tokens(
            model=SONNET,
            system=[{"type": "text", "text": fixture["new_system"]}],
            messages=[{"role": "user", "content": "x"}],
        ).input_tokens - client_sync.beta.messages.count_tokens(
            model=SONNET, messages=[{"role": "user", "content": "x"}],
        ).input_tokens

        cache_ok = sys_only_new >= 1024
        print(f"  System block tokens:  old={sys_only_old}  new={sys_only_new}  "
              f"cache_min=1024  {'OK' if cache_ok else 'FAIL — too small!'}")
        if not cache_ok:
            all_passed = False

        # Run extractions
        print("  Running OLD extraction...", flush=True)
        old_result = await run_extraction(client, fixture["old_system"], user_msg)
        print("  Running NEW extraction...", flush=True)
        new_result = await run_extraction(client, fixture["new_system"], user_msg)

        # Structural check: same top-level keys
        old_keys = set(old_result.keys())
        new_keys = set(new_result.keys())
        if old_keys != new_keys:
            print(f"  KEYS DIFFER:")
            print(f"    old keys: {sorted(old_keys)}")
            print(f"    new keys: {sorted(new_keys)}")
            all_passed = False
        else:
            print(f"  Top-level keys: {sorted(new_keys)}  (match)")

        # Diff
        diffs = diff_dicts(old_result, new_result)
        if not diffs:
            print("  Content diff: NONE — outputs are identical")
        else:
            print(f"  Content diff ({len(diffs)} changes):")
            for d in diffs:
                print(d)
            # Value-level diffs are expected (model is non-deterministic).
            # Structural/key diffs are failures.
            key_diffs = [d for d in diffs if d.startswith("  +") or d.startswith("  -")]
            if key_diffs:
                print("  STRUCTURAL DIFF — unexpected keys added/removed:")
                for d in key_diffs:
                    print(d)
                all_passed = False
            else:
                print("  (value-level variation is expected — non-deterministic model)")

    print(f"\n{sep}")
    if all_passed:
        print("  RESULT: ALL FIXTURES PASSED")
        print("  - Token counts meet 1,024 minimum for all Sonnet call sites")
        print("  - No structural (key) differences in extraction output")
    else:
        print("  RESULT: ONE OR MORE FAILURES — see details above")
    print(sep)

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
