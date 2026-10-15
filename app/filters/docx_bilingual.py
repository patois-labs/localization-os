"""Bilingual translation tables in DOCX (Articulate Storyline 360 "Export to Word").

Storyline 360's own localization route exports the course text to a Word document
(File > Translation > Export) whose tables carry the source text and a column titled
"Translation"; Articulate's instruction is to "only modify text in the column titled
Translation" and leave all other text unchanged, and the import requires the same number
of text strings as the project
(https://www.articulatesupport.com/article/Storyline-360-Translating-Courses,
https://www.articulatesupport.com/article/Storyline-360-Imported-Word-Translation-with-Exceptions).

Translated as an ordinary DOCX, every cell would be machine translated: the string IDs,
the column headers, the source column, the project-information table and the scene and
slide headings. This module finds such tables so the DOCX filter can expose ONLY the
paragraphs of the Translation column as units. The walk itself is untouched (no new walk
stamp): parse() simply emits fewer units, and build() only rewrites the paragraphs it is
given, so every other byte of the document's text stays as exported.

Detection is deliberately narrow: a table whose FIRST row has a cell reading exactly
"Translation" (case-insensitive) with a source-column header ("Original Text", "Original",
"Source Text" or "Source") somewhere to its left, and at least one later row with text in
the Translation cell. Rows whose cell count differs from the header row (merged scene or
slide label rows) and repeated header rows are skipped.

Documented in: docs/architecture/translation-pipeline.md
"""

from app.filters.docx_filter import qn

_W_TBL = qn("w:tbl")
_W_TR = qn("w:tr")
_W_TC = qn("w:tc")
_W_P = qn("w:p")
_W_T = qn("w:t")

TARGET_HEADERS = frozenset({"translation"})
SOURCE_HEADERS = frozenset({"original text", "original", "source text", "source"})

NOTICE_CODE = "docx_bilingual_table"


def _cell_text(tc) -> str:
    return "".join(t.text or "" for t in tc.iter(_W_T)).strip()


def _norm(text: str) -> str:
    return " ".join(text.split()).rstrip(":").casefold()


def _columns(header: list[str]) -> tuple[int, int] | None:
    """(source column, translation column) of a header row, or None."""
    for j, name in enumerate(header):
        if name in TARGET_HEADERS:
            for i in range(j - 1, -1, -1):
                if header[i] in SOURCE_HEADERS:
                    return i, j
    return None


def translation_paragraphs(body) -> tuple[set, dict] | None:
    """The w:p elements inside Translation-column cells of every bilingual table in
    `body` (the main document part's root element), plus counts for the upload notice.
    None when the document has no bilingual table with any translation text, so the
    caller falls back to translating the whole document as before."""
    keep: set = set()
    tables = rows = empty = src_chars = 0
    for tbl in body.iter(_W_TBL):
        trs = [tr for tr in tbl if tr.tag == _W_TR]
        if len(trs) < 2:
            continue
        head_cells = [tc for tc in trs[0] if tc.tag == _W_TC]
        header = [_norm(_cell_text(tc)) for tc in head_cells]
        cols = _columns(header)
        if cols is None:
            continue
        src_col, tgt_col = cols
        found = 0
        for tr in trs[1:]:
            cells = [tc for tc in tr if tc.tag == _W_TC]
            if len(cells) != len(head_cells):
                continue  # a merged label row (scene / slide / layer)
            if [_norm(_cell_text(tc)) for tc in cells] == header:
                continue  # a repeated header row
            if not _cell_text(cells[tgt_col]):
                if _cell_text(cells[src_col]):
                    empty += 1
                continue
            keep.update(cells[tgt_col].iter(_W_P))
            src_chars += len(_cell_text(cells[src_col]))
            found += 1
        if found:
            tables += 1
            rows += found
    if not keep:
        return None
    # Gate train-11: an ordinary document that merely CONTAINS a Source | Translation
    # table (a style guide with a term table) must not lose its prose. Switch only when
    # the body text outside every table (headings aside: Storyline writes scene and
    # slide headings) is smaller than the source column the tables carry.
    if _prose_chars(body) > src_chars:
        return None
    return keep, {"tables": tables, "rows": rows, "empty_rows": empty}


_W_PSTYLE = qn("w:pStyle")
_W_VAL = qn("w:val")


def _prose_chars(body) -> int:
    """Characters in non-heading paragraphs that sit outside every table."""
    n = 0
    for p in body.iter(_W_P):
        if any(True for _ in p.iterancestors(_W_TBL)):
            continue
        style = p.find(f"{qn('w:pPr')}/{_W_PSTYLE}")
        sid = (style.get(_W_VAL) or "").casefold() if style is not None else ""
        if sid.startswith(("heading", "title")):
            continue
        n += sum(len(t.text or "") for t in p.iter(_W_T))
    return n


def notice(counts: dict) -> dict:
    """The upload notice (rides in skeleton["file_warnings"])."""
    msg = (
        f"Detected a bilingual translation table (Articulate Storyline 'Export to Word' "
        f"layout): only the Translation column is translated ({counts['rows']} rows in "
        f"{counts['tables']} tables). IDs, source text, headings and other content are "
        "left unchanged."
    )
    if counts.get("empty_rows"):
        msg += (
            f" {counts['empty_rows']} rows have an empty Translation cell and are left "
            "empty; re-export from Storyline so the Translation column is filled."
        )
    return {"code": NOTICE_CODE, "message": msg, **counts}
