# 0069. Bilingual files translate only into the language they declare

Status: Accepted
Date: 2026-10-11

## Context

#687: a vendor bilingual file (Trados SDLXLIFF/packages, Phrase, Wordfast, TTX, memoQ, zipped XLIFF) can declare a
target language different from the project's. Writing another language into it produces a file the vendor tool
rejects or mislabels. The maintainer approved rejecting it.

## Decision

- Accept the upload with a warning and translate only into the target language the file declares.
- If none of the project's targets matches the declared language, refuse (422, nothing stored) with a message that
  names the file's language, the project's targets and both fixes. Web, API, CLI and MCP all show the same text.
- Documents imported before the rule are refused at delivery for another language (export reads the declaration
  from the original bytes) and skipped and reported in a project bundle.
- Same release: a headerless PO gets a header (charset UTF-8 + target Language) on export (decision 9); HTML Help
  reads its code page from the .hhp project file when present (decision 21).
