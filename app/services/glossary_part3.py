"""glossary: part 3 (layered pure move)

Documented in: docs/architecture/tm-and-glossary.md
"""

import io

from sqlmodel import Session

from app import output_csv as csv
from app.services.glossary_common import (
    _CSV_COLUMNS,
    _CSV_TERM_COLUMNS,
    _GLOSSARY_META_COLS,
    CSV_FORMAT,
    JSON_FORMAT,
    TBX_FORMAT,
)
from app.services.glossary_part2 import iter_entries


def export_bytes(
    session: Session,
    project_id: int | None,
    glossary_id: int | None = None,
    fmt: str = CSV_FORMAT,
    *,
    dialect: str = "tbx-basic",
    strict_portable: bool = False,
    drops_out: list[str] | None = None,
) -> bytes:
    """Export a glossary as CSV (default), TBX, or `.glossary.json` — the seam behind
    every export surface. `.glossary.json` (D2) is the no-caveat lossless format
    (concepts + all term metadata + status/audience/term_type/cross_refs/homonyms); CSV
    is lossless for what it carries (senses + DNT + the five meta fields + the six
    term columns it flattens); TBX (T2) defaults to TBX-Basic v3 (DCA) with the los:
    private namespace — `dialect="tbx-core"` and `strict_portable=True` drop los:
    (portable emits a clean pure-TBX-Basic instance, §6.5). A concept-format (TBX/JSON)
    export of an EMPTY glossary raises glossary_tbx.EmptyGlossaryError; CSV of an empty
    glossary is unchanged (a valid header-only file, §6.4/M8). Concept-aware JSON/TBX-Basic
    export is MODULE-glossary only (a legacy project falls to the flat TBX-Core export)."""
    if fmt == TBX_FORMAT:
        from app.services import glossary_tbx

        return glossary_tbx.export_tbx(
            session,
            project_id,
            glossary_id=glossary_id,
            dialect=dialect,
            strict_portable=strict_portable,
            drops_out=drops_out,
        )
    if fmt == JSON_FORMAT:
        from app.services import glossary_concepts, glossary_tbx

        if glossary_id is None:
            raise glossary_concepts.ConceptError(
                ".glossary.json export requires a module glossary."
            )
        doc = glossary_concepts.to_v1(session, glossary_id)
        if not (doc.get("concepts") or []):
            raise glossary_tbx.EmptyGlossaryError("this glossary has no concepts to export.")
        return glossary_concepts.dumps_v1(doc)
    return export_csv(session, project_id, glossary_id=glossary_id)


def export_csv(
    session: Session,
    project_id: int | None,
    glossary_id: int | None = None,
) -> bytes:
    """Export a glossary to CSV (round-trips import_csv).

    Emits the full header (_CSV_COLUMNS): the required columns, the five TBX-ish meta
    columns (filled from GlossaryEntry.meta), AND the six concept-model term columns
    (SPEC-terminology-backend §7.1, Decision G — status/term_type/audience/
    grammatical_gender/grammatical_number/geographical_usage, filled from the entry's
    linked source GlossaryTerm, blank when unlinked). One base row per term, then one
    row per sense; a sense row leaves the six new columns BLANK (M8 — sense metadata
    lives on the sibling homonym term, carried in full only by JSON/TBX).

    LAW-TERM-1 CSV carve-out, stated honestly (gate round-1 L1): a base row of a
    MODULE glossary with a linked term is NOT all-blank in the new columns — `status`
    (default `preferred`) and `audience` (default `human,mt,llm`) are ALWAYS populated
    from the linked source `GlossaryTerm`; the other four (term_type/grammatical_*/
    geographical_usage) are blank when unset. So byte-identity across the H15 header
    holds only for those four extra columns, plus the five meta columns, for a glossary
    carrying no new-column data. LEGACY per-project entries have no concept, so all six
    new columns are blank there. `glossary_id` selects a module glossary; otherwise the
    project's legacy entries."""
    if glossary_id is not None:
        items = iter_entries(session, glossary_id=glossary_id)
    else:
        items = iter_entries(session, project_id=project_id)

    from app.models import GlossaryTerm

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(_CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for item in items:
        e = item["entry"]
        meta = e.meta or {}
        base = {
            "source_term": e.source_term,
            "target_term": e.target_term,
            "dnt": "true" if e.dnt else "",
            "sense_label": "",
            "context_cue": "",
        }
        for col in _GLOSSARY_META_COLS:
            base[col] = meta.get(col, "")
        # The six concept-model term columns from the linked source GlossaryTerm.
        term = session.get(GlossaryTerm, e.term_id) if e.term_id else None
        for col in _CSV_TERM_COLUMNS:
            base[col] = ""
        if term is not None:
            base["status"] = term.status or ""
            base["term_type"] = term.term_type or ""
            # |-delimited so comma stays the CSV field separator (M27).
            base["audience"] = (term.audience or "").replace(",", "|")
            base["grammatical_gender"] = term.grammatical_gender or ""
            base["grammatical_number"] = term.grammatical_number or ""
            base["geographical_usage"] = term.geographical_usage or ""
        writer.writerow(base)
        for s in item["senses"]:
            # Sense rows carry no meta and BLANK new columns (M8); importer merges term
            # meta from the base row regardless.
            writer.writerow(
                {
                    "source_term": e.source_term,
                    "target_term": s.target_term,
                    "dnt": "",
                    "sense_label": s.sense_label,
                    "context_cue": s.context_cue or "",
                }
            )
    return buf.getvalue().encode("utf-8")


def list_entries(session: Session, project_id: int) -> list[dict]:
    """Deliberately materialize all legacy project entries; page_entries is the bounded seam."""
    return list(iter_entries(session, project_id=project_id))


def list_entries_for_glossary(session: Session, glossary_id: int) -> list[dict]:
    """Deliberately materialize all module entries; page_entries is the bounded seam."""
    return list(iter_entries(session, glossary_id=glossary_id))
