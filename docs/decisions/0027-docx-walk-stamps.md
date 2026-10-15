# 0027. DOCX extraction is versioned by walk stamps; older stamps stay byte-identical

- Status: Accepted
- Date: 2026-10-01 (stamps 3 and 4), 2026-10-02 (stamp 5), 2026-10-11 (stamp 6)

## Context

Each DOCX improvement (nested tables and content controls #355, headers/footers/notes/text boxes and
moves #270, smartTags/alt text/field warnings #368) changes which paragraphs exist and their order.
Stored projects map translations to paragraphs by position, so changing the walk under an existing
project would put translations in the wrong place.

## Decision

- Every document stores the walk stamp it was parsed with. Parse and export use the same walk on the
  same original bytes. A new paragraph order or new part is a NEW stamp; existing stamps never change.
- Older stamps' output stays byte-identical to main, proven by a differential over the real-file corpus
  and fuzz (952/952 real, 1200/1200 fuzz at stamp 5).
- An unknown stamp (newer project on an older build) answers 409 "Re-import".
- Stamp 3 numbers paragraphs in true reading order. Stamp 4 translates each part in place and treats
  `w:moveTo` like `w:ins`. Stamp 5 adds smartTags and alt text, warns on data-bound controls and
  `w:fldSimple` results (warned, not translated).
- Stamp 6 (#686, 2026-10-11) keeps stamp 5's numbering and reads text through
  NESTED inline wrappers (smart tag in smart tag, smart tag or hyperlink in a tracked insertion,
  inline customXml, w:dir / w:bdo, text content controls), re-creating the whole wrapper chain on
  export. Changing tokenization inside a paragraph costs a stamp, like a new paragraph order.
  Proof: walks 1 to 5 identical on 279 real files (1235 file/walk exports, base vs branch), and a
  pinned stamp-5 export of a nested fixture in verify_docx_po_fidelity test 43.

## Consequences

- Stamps are kept forever; stored older-stamp projects gain new parts only on re-import.
- Translating `fldSimple` results stays undecided (warning only).

## Links

- .claude/rules/docx-filter.md; docs/architecture/translation-pipeline.md
