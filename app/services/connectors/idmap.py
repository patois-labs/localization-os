"""ConnectorIdMap access — the T3 (document-per-locale) source->target id map
(SPEC-t3-notion-helpscout, the reserve-then-fill + reconciliation state table).

This is FRAMEWORK code, not connector code (spec Guardrail: review it as such). It
is the ONE place the T3 map is read/written, so the reconciliation lifecycle
(building | done | orphaned | needs_link) lives in exactly one module and every T3
connector (Notion first, then Help Scout, GitBook, WordPress, AEM) shares it.

Concurrency: `reserve` is the create-idempotency point. Two runs for the SAME
(connection, source, locale) — a scheduled run + a manual one, or the server + the
CLI — must not both POST a new target. Callers wrap the whole
read-decide-reserve-fill span in `write_lock(...)` (a striped in-process mutex,
the T2 D1 precedent) so the second caller re-reads the winner's row and RESUMES/
UPDATES instead of duplicating. The UNIQUE (connection_id, source_id,
target_locale) index (app/db.py / the alembic revision) is the cross-process
backstop: a losing concurrent INSERT hits IntegrityError and is turned into a
re-read here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import engine
from app.models import ConnectorIdMap

# Reuse the T2 striped write-lock pool (single-process guarantee, ADR-003 Tier-3
# caveat documented there) so T3's reserve span is serialized with the same
# discipline. Keyed on (connection, source, locale) — one localized document.
from app.services.connectors.strategy import _write_lock_for

# Status values (kept as plain strings for SQLite friendliness, like the rest of
# the model). Exported so the strategy + connectors reference them symbolically.
BUILDING = "building"
DONE = "done"
ORPHANED = "orphaned"
NEEDS_LINK = "needs_link"


def write_lock(connection_id, source_id: str, target_locale: str):
    """The striped mutex guarding the read-decide-reserve-fill span for ONE
    (connection, source doc, locale). Same key -> same lock within this process."""
    return _write_lock_for(connection_id, source_id, target_locale)


def get(connection_id: int, source_id: str, target_locale: str) -> ConnectorIdMap | None:
    with Session(engine) as s:
        return s.exec(
            select(ConnectorIdMap).where(
                ConnectorIdMap.connection_id == connection_id,
                ConnectorIdMap.source_id == str(source_id),
                ConnectorIdMap.target_locale == target_locale,
            )
        ).first()


def reserve(
    connection_id: int,
    source_id: str,
    target_locale: str,
    target_id: str | None,
    status: str = BUILDING,
) -> ConnectorIdMap:
    """Upsert the map row for a reserved/found target. Returns the persisted row.

    Insert-or-update keyed on the unique triple. If a concurrent writer already
    inserted (IntegrityError on the unique index), we roll back and UPDATE the
    existing row instead — so the two never create two rows (the cross-process
    backstop; the in-process write_lock already serializes same-process callers)."""
    with Session(engine) as s:
        row = s.exec(
            select(ConnectorIdMap).where(
                ConnectorIdMap.connection_id == connection_id,
                ConnectorIdMap.source_id == str(source_id),
                ConnectorIdMap.target_locale == target_locale,
            )
        ).first()
        if row is None:
            row = ConnectorIdMap(
                connection_id=connection_id,
                source_id=str(source_id),
                target_locale=target_locale,
                target_id=target_id,
                status=status,
                updated_at=datetime.utcnow(),
            )
            s.add(row)
            try:
                s.commit()
                s.refresh(row)
                return row
            except IntegrityError:
                s.rollback()
                row = s.exec(
                    select(ConnectorIdMap).where(
                        ConnectorIdMap.connection_id == connection_id,
                        ConnectorIdMap.source_id == str(source_id),
                        ConnectorIdMap.target_locale == target_locale,
                    )
                ).first()
                if row is None:  # pragma: no cover — the losing INSERT proves it exists
                    raise
        row.target_id = target_id
        row.status = status
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def set_status(
    connection_id: int,
    source_id: str,
    target_locale: str,
    status: str,
    target_id: str | None = None,
) -> None:
    """Flip an existing row's status (and optionally its target_id). No-op if the
    row is gone."""
    with Session(engine) as s:
        row = s.exec(
            select(ConnectorIdMap).where(
                ConnectorIdMap.connection_id == connection_id,
                ConnectorIdMap.source_id == str(source_id),
                ConnectorIdMap.target_locale == target_locale,
            )
        ).first()
        if row is None:
            return
        row.status = status
        if target_id is not None:
            row.target_id = target_id
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()


def delete(connection_id: int, source_id: str, target_locale: str) -> bool:
    """Delete one row (operator 'ignore' of a needs_link, or a stale-row drop).
    Returns True when a row was removed."""
    with Session(engine) as s:
        row = s.exec(
            select(ConnectorIdMap).where(
                ConnectorIdMap.connection_id == connection_id,
                ConnectorIdMap.source_id == str(source_id),
                ConnectorIdMap.target_locale == target_locale,
            )
        ).first()
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def list_for_connection(connection_id: int, status: str | None = None) -> list[ConnectorIdMap]:
    """All map rows for a connection (optionally filtered to one status) — the
    'shared pages' status panel + the needs_link operator worklist."""
    with Session(engine) as s:
        q = select(ConnectorIdMap).where(ConnectorIdMap.connection_id == connection_id)
        if status is not None:
            q = q.where(ConnectorIdMap.status == status)
        return list(
            s.exec(q.order_by(ConnectorIdMap.source_id, ConnectorIdMap.target_locale)).all()
        )


def status_counts(connection_id: int) -> dict:
    """{status: count} for the connection — the connection-page summary panel."""
    out = {BUILDING: 0, DONE: 0, ORPHANED: 0, NEEDS_LINK: 0}
    for row in list_for_connection(connection_id):
        out[row.status] = out.get(row.status, 0) + 1
    return out


def delete_for_connection(connection_id: int, session: Session | None = None) -> int:
    """Remove every map row for a connection. Called from store.delete's child
    sweep (connection deletion is the erase path for a connection's content — the
    map rows reference external customer content ids, spec GDPR Guardrail) AND
    keeps the Postgres FK valid (connection.id is the parent, no ON DELETE CASCADE).
    When a `session` is passed the caller owns the commit (so it participates in
    the connection-delete transaction); otherwise a self-contained session is used."""

    def _run(s: Session) -> int:
        rows = s.exec(
            select(ConnectorIdMap).where(ConnectorIdMap.connection_id == connection_id)
        ).all()
        for r in rows:
            s.delete(r)
        return len(rows)

    if session is not None:
        return _run(session)
    with Session(engine) as s:
        n = _run(s)
        s.commit()
        return n


def remap_references(connection_id: int, source_ids, target_locale: str) -> list:
    """Remap a list of SOURCE-locale document ids to their TARGET-locale ids via the
    id map — the reusable T3 cross-link helper (SPEC-t3 `related[]` remap; Help Scout
    is the first consumer, GitBook/WordPress/AEM inherit it).

    A source id that HAS a `done` map row for this locale is replaced by that row's
    target_id (the localized document). An UNMAPPED id — no row, or a row not yet
    `done` (building/needs_link/orphaned) — is LEFT AS-IS, still pointing at the
    SOURCE document. That is the deliberate, documented choice (SPEC-t3): we never
    INVENT a target id, so an unremapped cross-link degrades to linking at the
    source-locale article rather than a fabricated/dangling reference. Only `done`
    counts, mirroring exists_target (a half-built target must not be linked to).
    Order and duplicates are preserved so the reference list round-trips 1:1."""
    out: list = []
    for sid in source_ids or []:
        sid = str(sid)
        row = get(connection_id, sid, target_locale)
        if row is not None and row.status == DONE and row.target_id:
            out.append(str(row.target_id))
        else:
            out.append(sid)  # unmapped -> stays pointing at source (never invented)
    return out


# SENTINEL for an UNRESOLVED reference in a suppression digest (a source ref that has
# no `done` target row yet). Chosen as a byte a real target id can never contain, so a
# resolved id and "unresolved" can never collide in the digest.
_UNRESOLVED = "\x00"


def resolved_reference_digest(connection_id: int, source_ids, target_locales) -> str:
    """A deterministic digest of how an article's cross-references (`related[]`)
    CURRENTLY resolve, across the given target locales — folded into the T3
    per-(article,locale) suppression hash so a referrer re-processes (and HEALS its
    links) the run AFTER a referent finally reaches `done` (MED-1). Without it, a
    referrer written before its referent was `done` links to the SOURCE article forever,
    because content-hash suppression never revisits an article whose own source never
    changed.

    Empty string when there are NO references — the caller appends this to the hash
    input ONLY when it is non-empty, so a zero-references article's suppression hash is
    byte-identical to the pre-digest hash and those articles never re-translate on
    upgrade. Deterministic: locales normalized + sorted, source-reference ORDER
    preserved (references are ordered), SENTINEL for an unresolved ref. Combining ALL
    target locales into one per-article digest is what lets the single per-article
    `chash` the flow computes still heal every locale (a referent going `done` in ANY
    locale changes the digest -> the referrer re-fills, remapping every locale's link;
    an already-correct locale just re-writes identical content, near-free). Reused by
    every T3 connector that has cross-links (Help Scout is the first consumer)."""
    if not source_ids:
        return ""
    locales = sorted({str(l).strip().lower() for l in (target_locales or []) if str(l).strip()})  # noqa: E741
    tokens: list[str] = []
    for loc in locales:
        for sid in source_ids:
            sid = str(sid)
            row = get(connection_id, sid, loc)
            resolved = (
                str(row.target_id)
                if row is not None and row.status == DONE and row.target_id
                else _UNRESOLVED
            )
            tokens.append(f"{loc}\x1d{sid}\x1d{resolved}")
    return "\x1e".join(tokens)


def resolve_needs_link(
    connection_id: int, source_id: str, target_locale: str, action: str
) -> tuple[bool, str]:
    """Operator resolution of a needs_link row (adopt-or-ignore, SPEC-t3 rule 6).

      adopt  — accept the pre-existing external target as ours: flip the row to
               `done` so the next run UPDATES it in place (rule 2). This is the
               only sanctioned way a pre-existing human translation enters the map.
      ignore — drop the row: the next run treats the (source, locale) as brand-new
               and (re-)collides on that same target, re-flagging needs_link. Use
               when the external target is NOT the correct localized document.

    Returns (ok, message). Refuses to act on a row that is not needs_link (so this
    can't be used to force-adopt/clobber a live building/done target)."""
    row = get(connection_id, source_id, target_locale)
    if row is None:
        return False, "No such id-map row."
    if row.status != NEEDS_LINK:
        return False, (
            f"Row {source_id}/{target_locale} is '{row.status}', not "
            f"needs_link — nothing to resolve."
        )
    act = (action or "").strip().lower()
    if act == "adopt":
        set_status(connection_id, source_id, target_locale, DONE)
        return True, (
            f"Adopted the existing target for {source_id}/{target_locale} "
            f"(id {row.target_id}); the next run updates it in place."
        )
    if act == "ignore":
        delete(connection_id, source_id, target_locale)
        return True, (
            f"Ignored the pre-existing target for {source_id}/"
            f"{target_locale}; it will re-flag on the next run."
        )
    return False, "Action must be 'adopt' or 'ignore'."
