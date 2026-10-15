"""docs/specs/mcp-server.md rot-guard (audit 2026-09-05 D3).

`docs/specs/mcp-server.md` is an internal planning spec, not a user-facing descriptor,
so nothing in the guard suite watched its internals -- and its §4 tier table and §14 totals drifted
badly (read 18 / write 10 / spend 6, "69 tools", "+29 tools") while the live registry grew to
read 30 / write 33 / spend 16, 79 tools. This guard pins the SPEC's tier COUNTS, its SPEND-tool
LIST, and its tool-count TOTALS to `integrations/mcp-server/registry.py::TOOLS`, so the same drift
cannot silently re-open.

Hermetic + offline: it imports ONLY `registry` (which imports nothing from `app`) and reads the
spec off disk -- no app boot, no DB, no server. Final line: "MCP SPEC TIERS OK".
"""

import collections
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))

import registry

_SPEC = os.path.join(_ROOT, "docs", "specs", "mcp-server.md")


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _live_counts():
    """The live tier counts, spend-tool set, total and family count from the registry."""
    tiers = collections.Counter(t.tier for t in registry.TOOLS)
    spend = {t.name for t in registry.TOOLS if t.tier == "spend"}
    families = {t.family for t in registry.TOOLS}
    return tiers, spend, len(registry.TOOLS), len(families)


# --- spec parsers (each returns the number/set the SPEC CLAIMS, or None) -----
def _spec_tier_count(text, tier):
    """The tool count the SPEC's §4 table row for `tier` claims (a plain `N tools`, or the
    destructive row's `**N tools**`), or None if the row/number is gone."""
    m = re.search(
        r"\|\s*\*\*" + tier + r"\*\*\s*\|[^|]*\|\s*\**\s*(\d+)\s*tools", text, re.IGNORECASE
    )
    return int(m.group(1)) if m else None


def _spec_spend_list(text):
    """The backtick-quoted spend-tool names the SPEC's spend row lists (a set), or None if the
    row is gone. Reads the cell after `N tools:` up to the closing table pipe."""
    m = re.search(r"\*\*spend\*\*.*?\d+\s*tools:\s*(.+?)\s*\|", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return set(re.findall(r"`([a-z_]+)`", m.group(1)))


def _spec_totals(text):
    """Every `N tools across <7|seven> families` count the SPEC states (item 3 + §14), as a list."""
    return [int(n) for n in re.findall(r"(\d+)\s*tools across (?:7|seven) families", text)]


def _problems(text, tiers, spend, total, families):
    """Everything the SPEC and the live registry disagree about, as reader-facing sentences."""
    problems = []
    for tier in ("read", "write", "spend", "destructive"):
        claimed = _spec_tier_count(text, tier)
        live = tiers.get(tier, 0)
        if claimed is None:
            problems.append(f"§4 tier table has no `{tier}` count row")
        elif claimed != live:
            problems.append(f"§4 `{tier}` says {claimed} tools; the registry has {live}")
    claimed_spend = _spec_spend_list(text)
    if claimed_spend is None:
        problems.append("§4 spend row lists no tool names")
    elif claimed_spend != spend:
        missing = sorted(spend - claimed_spend)
        extra = sorted(claimed_spend - spend)
        if missing:
            problems.append("§4 spend list is MISSING spend tools: " + ", ".join(missing))
        if extra:
            problems.append("§4 spend list names NON-spend tools: " + ", ".join(extra))
    totals = _spec_totals(text)
    if not totals:
        problems.append("the SPEC no longer states an `N tools across N families` total")
    for claimed in totals:
        if claimed != total:
            problems.append(f"the SPEC states {claimed} tools total; the registry has {total}")
    return problems


def main():
    with open(_SPEC, encoding="utf-8") as fh:
        text = fh.read()
    tiers, spend, total, families = _live_counts()

    # Sanity: the audit's live numbers, so a registry refactor that changed the shape (not just the
    # counts) surfaces here too, not only as a spec mismatch.
    assert families == 7, f"the registry has {families} families, not 7 -- re-read the audit"
    assert total == sum(tiers.values()), "tier counts do not sum to the tool total"

    problems = _problems(text, tiers, spend, total, families)
    assert not problems, "mcp spec and registry.py disagree:\n  " + "\n  ".join(problems)

    # MUTATION-PROVEN IN BOTH DIRECTIONS: a guard that never fires is worthless. Forge a spec that
    # is stale in each dimension and assert the checker CATCHES it -- so a real future drift cannot
    # slip past a checker that silently stopped matching.
    stale_read = re.sub(
        r"(\|\s*\*\*read\*\*\s*\|[^|]*\|\s*)\d+(\s*tools)", r"\g<1>18\g<2>", text, count=1
    )
    assert _problems(stale_read, tiers, spend, total, families), (
        "the checker failed to catch a stale `read` tier count"
    )
    # Mutate INSIDE the §4 spend cell only (a spend-tool name can also appear in a family section
    # elsewhere in the spec, so a blind text.replace could hit the wrong occurrence).
    _sm = re.search(
        r"(\*\*spend\*\*.*?\d+\s*tools:\s*)(.+?)(\s*\|)", text, re.IGNORECASE | re.DOTALL
    )
    dropped_spend = (
        text[: _sm.start(2)]
        + _sm.group(2).replace("`vendor_reject_rework`", "`nonexistent_never_a_tool`")
        + text[_sm.end(2) :]
    )
    assert _problems(dropped_spend, tiers, spend, total, families), (
        "the checker failed to catch a spend-list drift"
    )
    stale_total = re.sub(r"(\d+)(\s*tools across (?:7|seven) families)", r"69\2", text, count=1)
    assert _problems(stale_total, tiers, spend, total, families), (
        "the checker failed to catch a stale tool-count total"
    )
    # ...and it does NOT false-positive on the real, current spec (already asserted above via
    # `not problems`, restated here as the positive twin of the three negatives).
    assert not _problems(text, tiers, spend, total, families)

    print(
        f"  SPEC tiers pinned to registry: read {tiers['read']} / write {tiers['write']} / "
        f"spend {len(spend)} / destructive {tiers.get('destructive', 0)}, {total} tools OK"
    )
    print("MCP SPEC TIERS OK")


if __name__ == "__main__":
    main()
