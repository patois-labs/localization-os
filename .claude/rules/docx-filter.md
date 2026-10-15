---
paths:
  - "app/filters/docx*.py"
  - "app/services/ingest.py"
  - "app/services/export.py"
  - "tests/verify_docx*"
---

# DOCX filter and walk stamps

Why: ADR 0027 (versioned walk stamps).

- Walk stamps 1 to 6 are frozen (6 = nested inline wrappers). NEVER renumber or change an existing stamp's output: a new paragraph order or new part costs a NEW stamp, kept forever. Older stamps must stay byte-identical to main (differential over the real-file corpus in `tests/verify_docx_po_fidelity.py`, plus fuzz).
- Parse and export must call the same `_all_paragraphs(doc, walk)` on the same original bytes. An unknown stamp answers 409 "Re-import" (`app/services/export.py` maps it to `ExportUnreadableBlob`).
- Stored older-stamp projects keep their walk; parts a newer stamp adds stay source-language until re-import (by design).
- Revision containers: `w:ins` and `w:moveTo` are translated in place; `w:del`, `w:moveFrom` and move-range markers are never touched. A split tracked change keeps `w:id` on the first piece; extra pieces take fresh ids from `_revision_ids`.
- `w:fldSimple` results are warned, never translated (field and wrapper at once). Complex-field results ARE translated (`_fix_field_order`). PAGE/NUMPAGES/DATE and letterless results are exempt from the warning. Whether to translate fldSimple results is an open product decision.
- A block `w:sdt` with `w:dataBinding` is translated but Word refills it from customXml: keep the `docx_data_bound_controls` warning.
- Walks are iterative with explicit stacks (2 x 32 frames, 32-level table cap identical in parse and export); no recursion. lxml refuses depth past 256.
- Walk 6 inline wrappers: `_walk_paragraph_v6`, `_w6_runs`, `_w6_paragraph_of_run` and `walk6_reads` (the warning) must agree on what is a wrapper (`_w6_content`) and on `MAX_INLINE_DEPTH`. Walks 5 and older never reach that code (`_walk_paragraph` dispatches on `ins >= 6`). A complex field whose begin and end share a wrapper keeps that wrapper open until its end (`_fields_in_links` returns the shared depth); a re-created `w:bdo` / `w:dir` that matched its source text's direction follows the translation's (`_fit_direction`), one that did not stays as it was.
- `reingest_document` must build exactly what `ingest_document` builds (segments, notes, `a11y_*`, chunk plan, token estimate) and still refuses documents with target text.
- `DOCX_MAX_PARAGRAPHS` counts every `w:p` before the walk.
- `docs/architecture/translation-pipeline.md` is CRLF in the working tree: edit it as bytes. `.py` files are LF.
- Keep warning tests tied to behaviour, not wording (`verify_info_hygiene` once pinned a wrong message).
