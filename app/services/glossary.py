"""Glossary / termbase — static + context-dependent.

Transitional compatibility shim for one release (added 2026-09-15, remove the re-export block after
the next tagged release): `__all__` re-exports import_csv, import_csv_full,
GLOSSARY_IMPORT_FLUSH_BATCH, _apply_rows, import_bytes, _choose_sense, _emit_for_entry,
build_from_text from app.services.glossary_hub.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _GLOSSARY_META_COLS, _CSV_TERM_COLUMNS, _CSV_COLUMNS,
_META_VALUE_CAP, _sanitize_meta_value, _meta_from_row, clean_meta, _apply_csv_term_columns,
_csv_rows, CSV_FORMAT, TBX_FORMAT, JSON_FORMAT, _XML_BOMS, _XML_LEADING, _looks_like_xml,
_looks_like_json, export_format_for, _sync_concept, _term_spans, _term_in_text, _shadowed,
_PROTECTED_SPAN_PATTERNS, _PROTECTED_SPAN_RE, mask_protected_spans, _select_matched_entries, count,
_entry_statement, parse_cursor, _page_bounds, _entry_page, detect_format, add_term, update_entry,
_BASE_OPTION, _CUE_MIN_LEN, _cue_matches, _cue_fallback, page_entries, window_entries, iter_entries,
export_bytes, export_csv, list_entries, list_entries_for_glossary from app.services.glossary_common,
app.services.glossary_part2, app.services.glossary_part3.

Static:   one source term -> one target term (+ Do-Not-Translate flag).
Context-dependent (the novel bit): a term can carry multiple sense-tagged
translations; for each segment we pick the sense whose context best fits and
apply that translation. Selection runs through the provider, so a real LLM does
the disambiguation when a key is present and the deterministic mock (cue-token
overlap) does it offline.

Also: an LLM-assisted glossary *builder* that proposes candidate terms from text.

Documented in: docs/architecture/tm-and-glossary.md
"""

import csv
import io
import json
import re

import sqlalchemy as sa
from sqlmodel import Session, select

from app.models import GlossaryEntry, GlossarySense
from app.services import llm_json
from app.services.glossary_common import (
    _CSV_COLUMNS,
    _CSV_TERM_COLUMNS,
    _GLOSSARY_META_COLS,
    _META_VALUE_CAP,
    _PROTECTED_SPAN_PATTERNS,
    _PROTECTED_SPAN_RE,
    _XML_BOMS,
    _XML_LEADING,
    CSV_FORMAT,
    JSON_FORMAT,
    TBX_FORMAT,
    GlossaryCsvHeaderError,
    GlossaryImportIncomplete,
    _apply_csv_term_columns,
    _csv_rows,
    _entry_page,
    _entry_statement,
    _looks_like_json,
    _looks_like_xml,
    _meta_from_row,
    _page_bounds,
    _sanitize_meta_value,
    _select_matched_entries,
    _shadowed,
    _sync_concept,
    _term_in_text,
    _term_spans,
    clean_meta,
    count,
    drop_shadowed_terms,
    export_format_for,
    mask_protected_spans,
    parse_cursor,
)
from app.services.glossary_hub import (
    GLOSSARY_CHUNK_PAUSE_S,
    GLOSSARY_IMPORT_FLUSH_BATCH,
    GLOSSARY_MODULE_CHUNK_ROWS,
    GLOSSARY_WRITE_CHUNK_ROWS,
    _apply_rows,
    _choose_sense,
    _emit_for_entry,
    build_from_text,
    import_bytes,
    import_csv,
    import_csv_full,
)
from app.services.glossary_part2 import (
    _BASE_OPTION,
    _CUE_MIN_LEN,
    _cue_fallback,
    _cue_matches,
    add_term,
    detect_format,
    iter_entries,
    page_entries,
    update_entry,
    window_entries,
)
from app.services.glossary_part3 import (
    export_bytes,
    export_csv,
    list_entries,
    list_entries_for_glossary,
)
from app.services.providers import get_provider

__all__ = [
    "CSV_FORMAT",
    "GLOSSARY_CHUNK_PAUSE_S",
    "GLOSSARY_IMPORT_FLUSH_BATCH",
    "GLOSSARY_MODULE_CHUNK_ROWS",
    "GLOSSARY_WRITE_CHUNK_ROWS",
    "JSON_FORMAT",
    "TBX_FORMAT",
    "_BASE_OPTION",
    "_CSV_COLUMNS",
    "_CSV_TERM_COLUMNS",
    "_CUE_MIN_LEN",
    "_GLOSSARY_META_COLS",
    "_META_VALUE_CAP",
    "_PROTECTED_SPAN_PATTERNS",
    "_PROTECTED_SPAN_RE",
    "_XML_BOMS",
    "_XML_LEADING",
    "GlossaryCsvHeaderError",
    "GlossaryEntry",
    "GlossaryImportIncomplete",
    "GlossarySense",
    "_apply_csv_term_columns",
    "_apply_rows",
    "_choose_sense",
    "_csv_rows",
    "_cue_fallback",
    "_cue_matches",
    "_emit_for_entry",
    "_entry_page",
    "_entry_statement",
    "_looks_like_json",
    "_looks_like_xml",
    "_meta_from_row",
    "_page_bounds",
    "_sanitize_meta_value",
    "_select_matched_entries",
    "_shadowed",
    "_sync_concept",
    "_term_in_text",
    "_term_spans",
    "add_term",
    "build_from_text",
    "clean_meta",
    "count",
    "csv",
    "detect_format",
    "drop_shadowed_terms",
    "export_bytes",
    "export_csv",
    "export_format_for",
    "get_provider",
    "import_bytes",
    "import_csv",
    "import_csv_full",
    "io",
    "iter_entries",
    "json",
    "list_entries",
    "list_entries_for_glossary",
    "llm_json",
    "mask_protected_spans",
    "page_entries",
    "parse_cursor",
    "re",
    "sa",
    "select",
    "update_entry",
    "window_entries",
]


def match_terms(
    session: Session,
    project_id: int,
    segment_text: str,
    source_locale: str,
    target_locale: str,
    provider=None,
    glossary_id: int | None = None,
    disambig: str | None = None,
) -> list[dict]:
    """Return the glossary constraints that apply to this segment, with the
    context-correct sense chosen for any multi-sense terms.

    `disambig` (Gap D): a short, sanitized domain/audience hint from the run's context
    package, forwarded to _choose_sense; None/"" keeps sense selection byte-identical.

    `glossary_id` (Phase 5-B, the module payoff): when a run's workflow step pins a
    specific approved module Glossary, matching is scoped to THAT glossary's entries
    (locale-pair filtered) instead of the whole project's entries. When None the
    pre-module project-scoped matching is byte-identical.

    Now a thin composition over the shared selection/emission core
    (`_select_matched_entries` + `_emit_for_entry`) that `glossary_concepts.match_concepts`
    reuses, so the concept selector cannot drift from what this returns (LAW-TERM-1)."""
    entries = _select_matched_entries(
        session, project_id, segment_text, source_locale, target_locale, glossary_id
    )
    out: list[dict] = []
    for e in entries:
        emitted, _chosen, provider = _emit_for_entry(
            session, e, segment_text, provider, disambig, project_id=project_id
        )
        if emitted is not None:
            out.append(emitted)
    return out
