---
paths:
  - "app/services/qe_jev*.py"
  - "app/services/qa.py"
  - "app/services/qe*.py"
  - "app/services/jev_inspector.py"
  - "app/web/admin_jev.py"
  - "tests/verify_qe_jev*"
  - "tests/verify_jev*"
---

# Jev judge and number checks

Why: ADRs 0032 (Jev as judge-role engine), 0033 (number-check advisory forms).

- `app/services/qa.py` is shared by the write seam, editor grid and output guard: put Jev-only number rules in `qe_jev_structural.py` / `qe_jev_numbers.py`, never in `qa.py`.
- A number finding is advisory (MINOR) ONLY for the two owner-approved forms: dot-format clock time H.MM equal to the source time, and a number word 0 to 10 for its digit (listed languages). Everything else stays MAJOR and blocks. Never blanket-treat MINOR as advisory.
- Dropped time, changed date, lost minus and currency swap/drop all block; written-out times and dates are accepted silently.
- Rule text must match behaviour on all 7 surfaces (`llm.md`, `CLI.md`, `GUIDE-NEW-FEATURES.md`, `quality-lqa-evaluations.md`, `jev-compatible-server.md`, `help.html`, `qe_jev.describe_rule` which feeds API and MCP).
- Every new regex is linear: test hostile long inputs. `qa._MASK_RE` is a linear scanner object (email and markup-tag are hand-written scanners, the rest regex) with only `.sub`/`.finditer` and match objects exposing `m[0]`/`group`/`start`/`end`/`span`; change it only with `tests/verify_qa_mask_linear.py` (old-vs-new identical spans plus 100k-char timing) green; `_CJK_TIME_RE` must not start with an optional group plus `\s*`.
- Keep `tests/verify_qe_jev.py` must-block and must-not-block case lists growing together.
- Jev engines are judge-role only (`jev` hosted, key only, URL pinned, priced, capped; `jev_local` BYO URL, free). Unsure never vetoes or passes.
- POC comparisons run the LLM side on Gemma via LM Studio `engine:1` (`--llm-provider engine:1`).
