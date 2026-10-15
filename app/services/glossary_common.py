"""glossary: shared leaf helpers (layered pure move)

Documented in: docs/architecture/tm-and-glossary.md
"""

import csv
import io
import re

import sqlalchemy as sa
from sqlmodel import Session, select

from app.models import GlossaryEntry, GlossarySense

_GLOSSARY_META_COLS = ("part_of_speech", "definition", "usage_example", "subject_field", "notes")


_CSV_TERM_COLUMNS = (
    "status",
    "term_type",
    "audience",
    "grammatical_gender",
    "grammatical_number",
    "geographical_usage",
)


_CSV_COLUMNS = (
    "source_term",
    "target_term",
    "dnt",
    "sense_label",
    "context_cue",
    *_GLOSSARY_META_COLS,
    *_CSV_TERM_COLUMNS,
)


_META_VALUE_CAP = 500


# TBX-ish OPTIONAL term-metadata columns (SPEC-context-metadata phase 4 + the maintainer
# 2026-07-14 "glossary = a term database"). Stored in GlossaryEntry.meta; the four
# required behaviours (source_term/target_term/dnt + senses) are unchanged. A legacy
# CSV without these columns imports exactly as before (row.get -> None -> nothing
# stamped, meta stays NULL). Kept small + stable.
#
# The first four are the TBX-anchored fields the spec reserved (subject_field == the
# ITS "domain"). `notes` is the maintainer's free-text context field (ITS Localization Note),
# capped like the rest. INVARIANT (Terminology T3, LAW-TERM-4 — the old "never reaches the
# model" rule is RETIRED): metadata reaches the model ONLY through the audience-gated
# concept renderer (glossary_concepts.render_for_llm), and ONLY when a workflow step turns
# its concept_glossary_render toggle ON. With the toggle OFF (the default) the prompt
# injection (match_terms -> prompts.glossary_line/translation_prompt) still consumes ONLY
# source_term/target_term/dnt/sense_label/case_sensitive — match_terms's five-key dict is
# unchanged (LAW-TERM-1) — so meta stays context-for-humans on that path; the renderer is
# the one seam that surfaces it, and it does so behind the per-term audience gate.
# Concept-model term-level CSV columns (SPEC-terminology-backend §7.1, Decision G).
# These are REAL GlossaryTerm columns (NOT legacy meta keys — M25: clean_meta's allowlist
# is NOT extended for them). part_of_speech is deliberately EXCLUDED — it is already
# present via _GLOSSARY_META_COLS (M25). `audience` is |-delimited in the CSV cell
# ("human|mt|llm") because comma is the field separator (M27).
# Full CSV column order (required cols first, then the TBX-ish meta cols, then the
# concept-model term columns). Used by export_csv; import tolerates any subset/superset
# (csv.DictReader is header-driven). LAW-TERM-1 CARVE-OUT (H15): growing the header
# necessarily changes CSV output, so CSV byte-identity holds only for a glossary carrying
# no new-column data; the golden asserts the amended header.
# Cap on a stored meta value (mirrors tm._META_VALUE_CAP). Glossary meta cells are DATA
# about a term, never instructions; phase 4 does not render them into a prompt, but they
# are newline-flattened + capped at ingestion (forward-looking safety — a future render
# seam can never be fed a structure-token/newline payload smuggled through meta).
def _sanitize_meta_value(val) -> str:
    """Flatten whitespace + cap an externally-sourced glossary meta cell."""
    return " ".join(str(val or "").split())[:_META_VALUE_CAP]


def _meta_from_row(row: dict) -> dict:
    """The non-empty TBX-ish meta cells of a CSV row, sanitized. {} when none present
    (a legacy CSV without the columns) so the caller leaves meta untouched."""
    out: dict = {}
    for col in _GLOSSARY_META_COLS:
        raw = (row.get(col) or "").strip()
        if raw:
            out[col] = _sanitize_meta_value(raw)
    return out


def clean_meta(meta) -> dict:
    """Sanitize a submitted term-metadata dict (web form / CLI flags / API body) down
    to the ALLOWLISTED keys, each newline-flattened + capped. Blank/None values are
    dropped so an empty submission clears the field rather than storing "". Returns {}
    for None/empty/garbage. The single ingestion seam for term meta from any surface —
    mirrors _meta_from_row's per-value hygiene so a value can never smuggle a
    structure-token/newline payload into GlossaryEntry.meta."""
    out: dict = {}
    if not isinstance(meta, dict):
        return out
    for col in _GLOSSARY_META_COLS:
        cleaned = _sanitize_meta_value(meta.get(col))
        if cleaned:
            out[col] = cleaned
    return out


def _apply_csv_term_columns(
    session: Session,
    glossary_id: int,
    data: bytes,
    source_locale: str,
    target_locale: str,
    *,
    chunk_rows: int | None = None,
    pause_s: float = 0.0,
) -> None:
    """Apply the six concept-model term columns of a module CSV to the linked source terms.

    #615: the rows are handled in batches of at most `chunk_rows` (None: one batch), each
    looked up with ONE query per 500 surfaces (it was one scan of the glossary per row) and
    committed on its own, with `pause_s` seconds between commits so the SQLite write lock is
    free for other writers. A file whose candidate rows fit one batch is one commit, as before."""
    import time

    from app.models import GlossaryTerm
    from app.services import glossary_concepts

    def _flush(batch: list) -> bool:
        surfaces = sorted({s for s, _ in batch})
        term_of: dict[str, int | None] = {}
        for i in range(0, len(surfaces), 500):
            for surface, term_id in session.exec(
                select(GlossaryEntry.source_term, GlossaryEntry.term_id)
                .where(
                    GlossaryEntry.glossary_id == glossary_id,
                    GlossaryEntry.source_term.in_(surfaces[i : i + 500]),
                    GlossaryEntry.source_locale == source_locale,
                    GlossaryEntry.target_locale == target_locale,
                )
                .order_by(GlossaryEntry.id)
            ):
                term_of.setdefault(surface, term_id)
        ids = sorted({t for t in term_of.values() if t is not None})
        terms: dict[int, GlossaryTerm] = {}
        for i in range(0, len(ids), 500):
            for t in session.exec(
                select(GlossaryTerm).where(GlossaryTerm.id.in_(ids[i : i + 500]))
            ):
                terms[t.id] = t
        changed = False
        for surface, cells in batch:
            term_id = term_of.get(surface)
            term = terms.get(term_id) if term_id is not None else None
            if term is None:
                continue
            if cells["status"]:
                term.status = glossary_concepts._valid_status(cells["status"])
            if cells["term_type"]:
                term.term_type = glossary_concepts._valid_term_type(cells["term_type"])
            if cells["audience"]:
                # |-delimited in the cell (M27) -> canonical CSV subset.
                term.audience = glossary_concepts.canon_audience(cells["audience"].split("|"))
            if cells["grammatical_gender"]:
                term.grammatical_gender = _sanitize_meta_value(cells["grammatical_gender"])
            if cells["grammatical_number"]:
                term.grammatical_number = _sanitize_meta_value(cells["grammatical_number"])
            if cells["geographical_usage"]:
                term.geographical_usage = _sanitize_meta_value(cells["geographical_usage"])
            session.add(term)
            changed = True
        return changed

    batch: list = []
    for row in _csv_rows(data):
        term_surface = (row.get("source_term") or "").strip()
        if not term_surface or (row.get("sense_label") or "").strip():
            continue  # blank / sense rows carry no term-level columns (M8)
        cells = {c: (row.get(c) or "").strip() for c in _CSV_TERM_COLUMNS}
        if not any(cells.values()):
            continue
        batch.append((term_surface, cells))
        if chunk_rows and len(batch) >= chunk_rows:
            if _flush(batch):
                session.commit()
                if pause_s > 0:
                    time.sleep(pause_s)
            batch = []
    if batch and _flush(batch):
        session.commit()


class GlossaryImportIncomplete(ValueError):
    """A multi-chunk glossary CSV/TBX import failed after at least one chunk was committed
    (#615, ADR 0061).

    The committed entries stay (a ValueError so every surface shows the message as a refusal;
    the message names only the exception CLASS, never the database text, which can carry SQL
    and glossary terms). `rows_written` of `rows_total` entries were processed, and
    `content_changed` says whether any committed chunk really changed the glossary, which the
    module seam needs to revert an APPROVED glossary to draft. Importing the same file again
    finishes the job without duplicates."""

    def __init__(
        self,
        message: str,
        rows_written: int = 0,
        rows_total: int = 0,
        content_changed: bool = True,
    ):
        super().__init__(message)
        self.rows_written = rows_written
        self.rows_total = rows_total
        self.content_changed = content_changed


class GlossaryCsvHeaderError(ValueError):
    """A glossary CSV whose header row has no `source_term` column (#615). A ValueError, so
    every surface that already shows import refusals shows it; the web routes catch it by
    name to put the full message (expected and found columns) on the page."""


def _csv_rows(data: bytes):
    """The row iterator for a glossary CSV payload. ONE definition, used by BOTH
    import_csv and import_bytes's CSV arm, so the decode contract (utf-8-sig,
    errors="replace") can never drift between the two entry points."""
    from app.output_safety import csv_original_cell

    rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    # #615: a header row without a source_term column (e.g. `source,target`) used to import
    # zero entries and answer ok:true, so the file looked imported. Refuse it, naming the
    # columns found, before anything is parsed or written. A file with no header at all (empty)
    # still imports nothing, as before.
    names = [str(n or "").strip() for n in (rows.fieldnames or [])]
    if names and "source_term" not in names:
        found = ", ".join(n for n in names if n)[:200] or "(blank header)"
        raise GlossaryCsvHeaderError(
            "this glossary CSV has no 'source_term' column, so no terms could be read. "
            "The header row must start with source_term,target_term (optional: dnt, "
            f"sense_label, context_cue, ...). Found: {found}."
        )
    return ({key: csv_original_cell(value) for key, value in row.items()} for row in rows)


CSV_FORMAT = "csv"


TBX_FORMAT = "tbx"


JSON_FORMAT = "json"


_XML_BOMS = (b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00", b"\xef\xbb\xbf", b"\xfe\xff", b"\xff\xfe")


_XML_LEADING = re.compile(rb"[\s\x00]*")


# --- format dispatch (CSV | TBX) ----------------------------------------------
# The glossary bulk seams accept BOTH a glossary CSV and a native TBX termbase. One
# detector + one dispatcher serve every surface (legacy project + module glossary x
# web + CLI + /api), so no surface can drift into supporting only one format.
# SPEC-terminology-backend D2: the lossless concept-aware file format (.glossary.json),
# carrying status/audience/term_type/cross_refs/homonyms that CSV flattens. Module
# glossaries only (a concept requires a module Glossary).
# Byte-order marks a real XML file can start with (UTF-32 first: its BOMs have the
# UTF-16 ones as prefixes). After the BOM, whitespace AND NULs are skipped — a
# UTF-16/32 encoded document interleaves NULs, so "<" is not necessarily byte 0 —
# and the skip is unbounded because a pretty-printer's leading blank lines are not
# an attacker-controlled window we should guess the size of.
def _looks_like_xml(data: bytes) -> bool:
    """True when the payload's first significant byte is '<' — the content sniff
    behind detect_format. BOM- and UTF-16-aware: a UTF-16 TBX (BOM + '<\\x00…' or
    '\\x00<…') sniffed as CSV before, which meant a filename-less /api import of one
    silently landed zero terms instead of parsing (or erroring) as TBX."""
    for bom in _XML_BOMS:
        if data.startswith(bom):
            data = data[len(bom) :]
            break
    at = _XML_LEADING.match(data).end()
    return data[at : at + 1] == b"<"


def _looks_like_json(data: bytes) -> bool:
    """True when the payload's first significant byte is `{` (BOM/whitespace-skipped) —
    the content sniff for a filename-less .glossary.json import."""
    for bom in _XML_BOMS:
        if data.startswith(bom):
            data = data[len(bom) :]
            break
    at = _XML_LEADING.match(data).end()
    return data[at : at + 1] == b"{"


def export_format_for(filename: str | None) -> str:
    """Export format implied by an output path: .tbx -> TBX, .glossary.json/.json -> JSON
    (M4 — the CLI export path uses this to pick the writer; without the arm a
    `.glossary.json` name would silently write CSV), anything else CSV."""
    name = (filename or "").strip().lower()
    if name.endswith(".tbx"):
        return TBX_FORMAT
    if name.endswith(".glossary.json") or name.endswith(".json"):
        return JSON_FORMAT
    return CSV_FORMAT


def _sync_concept(session: Session, entry) -> None:
    """Dual-write hook (LAW-TERM-2): upsert the concept/term store backing one entry.
    Imported lazily to avoid an import cycle (glossary_concepts imports glossary)."""
    if entry is None or entry.glossary_id is None:
        return
    from app.services import glossary_concepts

    glossary_concepts.sync_entry_concept(session, entry)


def _term_spans(term: str, text: str, case_sensitive: bool) -> list[tuple[int, int]]:
    """Every (start, end) occurrence of `term` in `text`, with the SAME matching rule
    _term_in_text applies (whole-word for ASCII word-ish terms, substring otherwise).
    Spans are what longest-match shadowing needs; the boolean below delegates here so
    the two can never drift apart."""
    if not term:
        return []
    flags = 0 if case_sensitive else re.IGNORECASE
    if _UNSEGMENTED_RE.search(term):
        # CJK, Thai and friends have no spaces between words: a substring is the only
        # rule. Matching the ORIGINAL text with IGNORECASE (never text.lower()) keeps
        # spans aligned even for characters whose lower() changes the length (#510).
        return [m.span() for m in re.finditer(re.escape(term), text, flags)]
    # Every other script: whole-word by Unicode word characters, enforced only at an edge
    # that IS a word character, so "users'" or "C++" still end cleanly before a space.
    head = _WORD_BEFORE if _is_word_edge(term[0]) else ""
    tail = _WORD_AFTER if _is_word_edge(term[-1]) else ""
    return [m.span() for m in re.finditer(head + re.escape(term) + tail, text, flags)]


# Word characters for the boundary test: Unicode \w plus combining marks U+0300-U+036F.
_WORD_CLASS = "[" + chr(92) + "w" + chr(0x300) + "-" + chr(0x36F) + "]"
_WORD_BEFORE = "(?<!" + _WORD_CLASS + ")"
_WORD_AFTER = "(?!" + _WORD_CLASS + ")"

# Scripts written without spaces between words (Thai, Lao, Myanmar, Khmer, Hiragana,
# Katakana, Han); a term containing one keeps the substring rule. Code point ranges.
_UNSEGMENTED_RANGES = (
    (0x0E00, 0x0EFF),
    (0x1000, 0x109F),
    (0x1780, 0x17FF),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
)
_UNSEGMENTED_RE = re.compile(
    "[" + "".join(chr(a) + "-" + chr(b) for a, b in _UNSEGMENTED_RANGES) + "]"
)


def _is_word_edge(ch: str) -> bool:
    return bool(re.match(r"\w", ch))


def _term_in_text(term: str, text: str, case_sensitive: bool) -> bool:
    return bool(_term_spans(term, text, case_sensitive))


def _shadowed(matched: list[tuple]) -> set[int]:
    """The ids of entries every one of whose occurrences sits INSIDE a strictly longer
    matched term's occurrence — classic longest-match terminology behaviour.

    WHY (live prod evidence, runs 29/30, 2026-07-28): the glossary carries the DNT
    entry "Acme Cloud" (keep verbatim) AND a generic `cloud` -> `nube`. The output
    "Bienvenido a Acme Cloud" preserved the DNT correctly and the adherence gate then
    flagged the INNER `cloud` for not being `nube` — a MAJOR on a segment that did
    exactly what the glossary asked. A shorter term that only ever appears inside a
    longer matched term is not an independent constraint on this segment, so it must
    reach NEITHER the prompt nor the gate. Source-side only (spans in the source), so
    it needs nothing the matcher does not already have; equal-length terms (the
    `attachment`/`Attachment` case-variant pair) never shadow each other.

    `matched` is [(entry, spans), ...]."""
    dropped: set[int] = set()
    for i, (entry, spans) in enumerate(matched):
        covered = True
        for s0, s1 in spans:
            inside = any(
                o0 <= s0 and s1 <= o1 and (o1 - o0) > (s1 - s0)
                for j, (_other, ospans) in enumerate(matched)
                if j != i
                for o0, o1 in ospans
            )
            if not inside:
                covered = False
                break
        if covered and spans:
            dropped.add(id(entry))
    return dropped


def drop_shadowed_terms(terms: list[dict], segment_text: str) -> list[dict]:
    """`terms` (emitted term dicts, possibly merged from several glossaries) minus the
    ones every occurrence of which sits inside a strictly longer term's occurrence in
    `segment_text` (#663). `_select_matched_entries` already does this within ONE
    glossary; this closes the cross-glossary gap ("Colorado Trail" in a module glossary,
    `trail` in the legacy entries). Order of the survivors is preserved."""
    if len(terms) < 2:
        return list(terms)
    from app.services.segment import strip_placeholders

    match_text = mask_protected_spans(strip_placeholders(segment_text))
    spanned = [
        (t, _term_spans(t.get("source_term") or "", match_text, bool(t.get("case_sensitive"))))
        for t in terms
    ]
    spanned = [(t, sp) for t, sp in spanned if sp]
    dropped = _shadowed(spanned)
    return [t for t in terms if id(t) not in dropped]


_PROTECTED_SPAN_PATTERNS = (
    r"\$\{[^{}]*\}",
    r"\{[^{}]*\}",
    r"%\([A-Za-z_][A-Za-z0-9_]*\)[-#+]*[0-9.]*[hlL]?[diouxXeEfFgGcrsa]",  # Python %(name)s (#689)
    r"%(?:\d+\$)?[0-9.\-]*[a-zA-Z]",
    r"\$[A-Za-z_][A-Za-z0-9_]*",
)


_PROTECTED_SPAN_RE = re.compile("|".join(_PROTECTED_SPAN_PATTERNS))


# --- placeholder-interior exclusion (QA BUG-02) --------------------------------
# A glossary term must NEVER be matched INSIDE a placeholder. Production evidence
# (QA campaign 2026-07-28): the source "Welcome back, {name}." matched the glossary
# term `name` inside the literal token `{name}`, so the adherence gate demanded
# `nombre`/`nom` in the target — a requirement that can only be satisfied by BREAKING
# the placeholder (style rule 2: placeholders are copied verbatim). The flag was
# therefore permanently unsatisfiable, and it fired on a CORRECT translation.
#
# The exclusion happens HERE, at the single term-matching seam, rather than in the
# violation checker: a term that is only present inside a placeholder is not a
# glossary constraint at all, so it must not reach the prompt EITHER (the model was
# being told to translate `name` -> `nombre` on a segment whose only "name" is a
# placeholder). Dropping it here fixes both surfaces at once — the prompt injection
# (prompts.glossary_line) and the deterministic gate (quality.glossary_term_violations
# consumes exactly the dicts match_terms returns).
#
# Spans are BLANKED (replaced by spaces, same length) rather than deleted, so word
# boundaries and offsets around them are preserved: "Welcome back, {name}." keeps its
# comma/period neighbours and a term straddling the span can never be forged.
# ${VAR} shell/template style
# {name}, {0}, ICU-ish {count, plural, ...}
# printf: %s %d %1$s %.2f
# $VAR
def mask_protected_spans(text: str) -> str:
    """`text` with every placeholder span blanked to same-length spaces.

    The placeholder families are the ones the project's own inline-tag extractor
    protects (segment._TAG_PATTERNS) plus `$VAR`: `{...}` / `${...}`, the printf
    `%s %d %1$s %.2f` family, and `$VAR`. HTML tags and URLs are deliberately NOT
    blanked — a term inside `<b>Save</b>` is real translatable content, and the
    reported defect is about placeholder INTERIORS only."""
    return _PROTECTED_SPAN_RE.sub(lambda m: " " * len(m.group(0)), text or "")


def _select_matched_entries(
    session: Session,
    project_id: int | None,
    segment_text: str,
    source_locale: str,
    target_locale: str,
    glossary_id: int | None = None,
) -> list[GlossaryEntry]:
    """THE shared selection core (SPEC-terminology-backend §5.3/H6, L1): the query +
    specificity precedence + span match + longest-match shadowing, returning the
    surviving GlossaryEntry rows in original order — everything match_terms does
    BEFORE per-entry sense selection/emission. `glossary_concepts.match_concepts`
    imports and reuses this so the concept selector cannot diverge from `match_terms`
    (the §9-T1 non-divergence test). Kept byte-for-byte with the code it replaced.

    WILDCARD LOCALES (#60). A blank/NULL stored source_locale or target_locale is a
    documented "any locale" wildcard (glossary_module.add_term's docstring). Match a
    stored locale when it EQUALS the requested locale OR is blank/NULL; a non-blank
    stored locale still matches only its own locale."""

    def _loc_ok(col, requested):
        return sa.or_(col == requested, col == "", col.is_(None))

    if glossary_id is not None:
        stmt = select(GlossaryEntry).where(
            GlossaryEntry.glossary_id == glossary_id,
            _loc_ok(GlossaryEntry.source_locale, source_locale),
            _loc_ok(GlossaryEntry.target_locale, target_locale),
        )
    else:
        stmt = select(GlossaryEntry).where(
            GlossaryEntry.project_id == project_id,
            GlossaryEntry.glossary_id == None,  # noqa: E711
            _loc_ok(GlossaryEntry.source_locale, source_locale),
            _loc_ok(GlossaryEntry.target_locale, target_locale),
        )
    entries = session.exec(stmt).all()
    # PRECEDENCE: specific over wildcard (#60). Group by source term (case-folded) and
    # keep only the entries at the HIGHEST locale specificity — score = (non-blank
    # source) + (non-blank target) — dropping strictly-less-specific duplicates of the
    # same term. A term with no same-name rival (the common case) is untouched.
    if len(entries) > 1:

        def _spec(e) -> int:
            return int(bool((e.source_locale or "").strip())) + int(
                bool((e.target_locale or "").strip())
            )

        best: dict[str, int] = {}
        for e in entries:
            k = (e.source_term or "").strip().casefold()
            best[k] = max(best.get(k, -1), _spec(e))
        entries = [e for e in entries if _spec(e) == best[(e.source_term or "").strip().casefold()]]
    if not entries:
        return []
    # Strip inline-fidelity placeholder tokens (⟦r1⟧…) before matching so a term whose
    # words straddle a run/inline-element boundary is still found; content placeholders
    # ({name}, %s, $VAR) are BLANKED so a term is never matched inside one (BUG-02).
    from app.services.segment import strip_placeholders

    match_text = mask_protected_spans(strip_placeholders(segment_text))
    # Two passes: find every matching entry WITH its occurrence spans, then drop the
    # ones a strictly longer matched term completely covers (see _shadowed — the
    # "Acme Cloud" DNT vs generic `cloud` false positive). ORIGINAL entry order kept.
    matched = [(e, _term_spans(e.source_term, match_text, e.case_sensitive)) for e in entries]
    matched = [(e, spans) for e, spans in matched if spans]
    dropped = _shadowed(matched)
    return [e for e, _spans in matched if id(e) not in dropped]


def count(session: Session, project_id: int) -> int:
    """Count only a project's legacy entries, without loading entry rows."""
    return int(
        session.exec(
            select(sa.func.count())
            .select_from(GlossaryEntry)
            .where(
                GlossaryEntry.project_id == project_id,
                GlossaryEntry.glossary_id == None,  # noqa: E711
            )
        ).one()
    )


def _entry_statement(project_id, glossary_id):
    if (project_id is None) == (glossary_id is None):
        raise ValueError("exactly one of project_id and glossary_id is required")
    stmt = select(GlossaryEntry)
    if glossary_id is not None:
        return stmt.where(GlossaryEntry.glossary_id == glossary_id)
    return stmt.where(
        GlossaryEntry.project_id == project_id,
        GlossaryEntry.glossary_id == None,  # noqa: E711
    )


def parse_cursor(cursor):
    """An omitted/empty cursor starts the walk; explicit cursors must be positive."""
    if cursor in (None, ""):
        return 0
    try:
        value = int(cursor)
        if value <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor must be a positive integer") from exc
    return value


def _page_bounds(cursor, limit):
    cursor = parse_cursor(cursor)
    try:
        limit = int(limit) if limit is not None else 100
    except (TypeError, ValueError):
        limit = 100
    # An arbitrarily large position is beyond every signed 64-bit database id.
    # Keep it an empty page instead of overflowing the DBAPI integer binding.
    return min(cursor, 2**63 - 1), max(1, min(limit, 1000))


def _entry_page(session, stmt, limit):
    rows = session.exec(stmt.limit(limit + 1)).all()
    entries, has_more = rows[:limit], len(rows) > limit
    grouped = {e.id: [] for e in entries}
    senses = session.exec(
        select(GlossarySense).where(GlossarySense.glossary_entry_id.in_(list(grouped)))
    ).all()
    for sense in senses:
        grouped[sense.glossary_entry_id].append(sense)
    return {
        "entries": [{"entry": e, "senses": grouped[e.id]} for e in entries],
        "has_more": has_more,
        "next_cursor": entries[-1].id if has_more else None,
    }
