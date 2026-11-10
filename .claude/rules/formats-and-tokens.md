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
- Bilingua stamp (ADR 0111): a document whose skeleton carries `bilingua` is ALWAYS read and built by Bilingua, switch on or off; skeletons without it stay on the Python filter. The stamp holds profile {name, version}, schema, segment ids, units hash and the original's sha256; profile name/version and schema must match at build; the crate version is informational while segment ids and units hash are unchanged, else refuse with "re-import" (never a 500). Never edit a stamp to make a mismatch pass; a behaviour change needs a NEW profile version.
- A Bilingua document does not get `read_gen`, `text_bom`, `xliff_tokens`, `docx_walk`/`xlsx_walk`/`md_walk`, `hh_decode` stamps and must not enter the legacy readers; token numbers come from the wheel, never from the adapter's own counter. `bilingua_formats` defaults to `all`; the wheel is installed from requirements.lock on x86_64 and AMD64 only (`docs/runbooks/bilingua-wheel.md`).
