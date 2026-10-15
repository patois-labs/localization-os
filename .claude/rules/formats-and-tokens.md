---
paths:
  - "app/filters/**"
  - "app/services/segment.py"
  - "app/services/srx.py"
  - "app/services/ingest.py"
  - "app/services/export.py"
  - "app/services/qa*.py"
  - "app/services/quality*.py"
  - "app/services/tag_*.py"
  - "tests/verify_*filter*"
  - "tests/verify_segment*"
---

# File formats, tokens and walk stamps

Why: ADRs 0064 to 0073.

- Point tokens now have TWO kinds: `x` (order kept) and `p` (free order, argument-naming placeholders, ADR 0068). Parse tokens with the kind-agnostic grammar `⟦/?[a-z]?\d+⟧`, never a hard-coded `x`. Old rows keep `x`; never re-tokenize stored segments.
- Split and walk versions are stamped per document and old documents replay their OLD version: splitter `legacy` (v1) / `legacy:2` / `srx:<hash>` (keep `default_v1.srx` byte-identical), `DOCX_WALK_VERSION` (6), `xlsx_walk` (1 cells, 2 text boxes), `text_code_page`. A behaviour change that moves segment ids needs a NEW version, never an edit of the old one (b-splitter, b-smarttags, b-xlsx gates).
- Untouched exports must stay byte-identical (ADR 0064): a new filter's `build()` with nothing written returns the upload; every charset label must name the codec actually written.
- New units appended to an existing format (SVG title/desc, xlsx text boxes, comments at `COMMENT_BASE` 1,000,000) go AFTER existing units so stored segment ids do not move.
- RTL-only paths are gated on target direction; LTR outputs must stay byte-identical (`gate\ltr_hash.py` in the b-rtl gate).
- The soak probe decodes exports with the declared charset and legacy code pages since round 14; an older runner reads a Windows-1252 export as "source left". Check the runner before filing an encoding bug from the soak.
