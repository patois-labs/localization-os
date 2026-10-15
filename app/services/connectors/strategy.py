"""LocaleMappingStrategy — the polymorphic write-back seam of the connector
framework (CONNECTOR_FRAMEWORK_DESIGN §2).

Every source system localizes content in one of three TOPOLOGIES; the topology —
not the vendor — determines the read/write/create-vs-update logic. The framework
puts that logic behind ONE interface with (eventually) three implementations:

    T1  FieldMapStrategy      — one record, per-field {locale: value} map   (later phase)
    T2  SubResourceStrategy   — a translations sub-resource per locale       (THIS phase)
    T3  DocPerLocaleStrategy  — a separate doc/page per locale + our id-map  (later phase)

Only the T2 SubResourceStrategy is implemented tonight — it is what Zendesk Guide
needs (a `translations` collection per Article/Section/Category, addressed by
locale). The interface is the one from the design doc so T1/T3 slot in unchanged.

The strategy is deliberately transport-shaped, not model-shaped: it operates on a
small `SubResourceClient` protocol (get_translation / create_translation /
update_translation) that a connector implements over its own authed httpx client.
That keeps the strategy vendor-neutral (Freshdesk/Zoho/Document360 reuse it by
supplying their own client) while the connector owns endpoints + auth.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("loc_tms.connectors")

# --- TOCTOU write serialization (adversarial audit D1) -----------------------
# write_target is an exists->create/update span: it GETs the target translation
# and, based on ABSENT-vs-present, POSTs a new locale object or PUTs the existing
# one. The job workers are IN-PROCESS THREADS on a single machine
# (jobs.WORKER_COUNT == 2), so a scheduled run and a webhook-triggered run can
# drive write_target for the SAME (connection, record, locale) concurrently and
# race the probe: both see ABSENT and both POST -> a DUPLICATE translation
# sub-resource on the external system (or one silently clobbers a human's edit).
#
# A fixed pool of STRIPED mutexes serializes the two writers whose key maps to the
# same stripe, so the second thread re-checks existence WHILE HOLDING THE LOCK and
# UPDATEs instead of double-CREATEing. Striped (vs a per-key registry) is chosen
# deliberately: memory is bounded by construction (no registry to prune, no
# guard-lock held across the network span, no unbounded growth), and the only cost
# of a hash collision is that two unrelated writes serialize briefly -- harmless
# for correctness at WORKER_COUNT == 2. hash() of the key is per-process stable,
# which is all a single-process guarantee needs.
#
# THIS IS A SINGLE-PROCESS GUARANTEE ONLY. It does NOT coordinate across processes
# or machines. That gap is not only a future multi-machine concern: a SECOND
# SAME-MACHINE PROCESS exists TODAY -- the CLI (app/cli/cmd_connector.py, e.g.
# `python -m app.cli connector zendesk-translate`) runs *_translate synchronously
# in its own OS process with its own stripe pool. A concurrent CLI *-translate
# against the same (connection, record, locale) is NOT serialized with the
# server; avoid running CLI translate flows against a connection the live
# server is actively writing. The cross-process conditional write (ETag /
# If-Match, mirroring Jira's `expected_current`) that would close both this CLI
# gap and true multi-machine scale-out is deferred to the multi-machine tier; do not mistake this
# lock for that guarantee.
_WRITE_STRIPE_COUNT = 64
_write_stripes = [threading.Lock() for _ in range(_WRITE_STRIPE_COUNT)]


def _write_lock_for(connection_id, record_id, target_locale) -> threading.Lock:
    """The striped mutex guarding the exists->create/update span for one
    (connection, record, locale). Same key -> same lock within this process."""
    key = (str(connection_id), str(record_id), str(target_locale))
    return _write_stripes[hash(key) % _WRITE_STRIPE_COUNT]


def _record_write_lock_for(connection_id, record_id) -> threading.Lock:
    """The striped mutex guarding a T1 read-merge-write span for one
    (connection, RECORD) — NOTE: locale-independent on purpose. T1's write is a
    GET-merge-PUT of ONE record's whole locale map, so the race is between two
    writers landing DIFFERENT locales of the SAME record (a locale-keyed lock, as
    used for T2, would NOT serialize them and an update would be lost). All locales
    of a record therefore collapse to a single stripe. Same single-process-only
    caveat as _write_lock_for (see the module comment above)."""
    key = (str(connection_id), str(record_id))
    return _write_stripes[hash(key) % _WRITE_STRIPE_COUNT]


# Sentinel a client read (get_translation / get_record) returns when the source
# system has NO translation for a (record, locale): distinct from an empty-string
# translation (which exists but is blank). exists_target must probe this, never
# content — see the Helpjuice "silent English fallback" gotcha in the design doc §3.5.
ABSENT = object()


class TargetUnverifiable(Exception):
    """verify_target could not tell whether the target exists (503/429/401/timeout).
    The strategy holds the pair for retry instead of treating it as deleted (#444)."""


@dataclass
class WriteResult:
    """Outcome of a single write_target. `created` True = a new locale object was
    POSTed; False = an existing one was PUT/updated. `target_id` is the source
    system's id for the written translation object when it returns one (used for
    DELETE, which Zendesk keys by translation_id, and for the source->target id
    map T3 will need)."""

    ok: bool
    created: bool
    target_id: str | None = None
    message: str = ""


class SubResourceClient(Protocol):
    """The minimal transport a SubResourceStrategy drives. A T2 connector
    implements these three over its own authed client; the strategy composes them
    into the 404-vs-200 create-or-update decision. Kept intentionally tiny so a
    new T2 connector is "supply these three methods" and nothing else.

    exists_target uses get_translation's ABSENT return — NOT a content read — so a
    system that silently falls back to the source language for a missing locale
    (Helpjuice) cannot fool the create-vs-update decision (design §3.5)."""

    def get_translation(self, record_id: str, locale: str):  # -> dict | ABSENT
        ...

    def create_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult: ...

    def update_translation(self, record_id: str, locale: str, fields: dict) -> WriteResult: ...


class LocaleMappingStrategy(ABC):
    """The topology-specific seam. Everything else in a connector (auth,
    extraction, HTTP, backoff) is shared; ONLY this differs per topology.

    The record here is opaque to the framework — a strategy is handed the
    connector's own record dicts and knows how to read fields off them. The
    framework only relies on the two methods below.
    """

    topology: str = ""  # "T1" | "T2" | "T3"

    # --- write ---
    @abstractmethod
    def write_target(
        self, client, record_id: str, target_locale: str, fields: dict, *, draft: bool = True
    ) -> WriteResult:
        """Land a translation for (record, target_locale). For T2 this is the
        404-vs-200 probe -> POST create or PUT update."""
        ...

    @abstractmethod
    def exists_target(self, client, record_id: str, target_locale: str) -> bool:
        """True when a translation object for the locale already exists (an
        explicit existence signal, never a content comparison)."""
        ...


class SubResourceStrategy(LocaleMappingStrategy):
    """T2 — entry/translation-per-locale (sub-resource).

    A master record owns a `translations` sub-resource addressed by locale; each
    locale is its own object linked to the master. write_target GETs the target
    translation: ABSENT -> POST create, present -> PUT update. This is the whole
    of Zendesk/Freshdesk/Zoho/Document360/Salesforce-Knowledge write-back.

    `fields` is the connector's already-extracted+translated payload (e.g.
    {"title": ..., "body": ...}); the strategy does not know or care which fields
    a given system carries. `draft` is threaded to create/update so per-locale
    draft-vs-publish (design §3.5) is honoured at the write.
    """

    topology = "T2"

    # A T2 strategy is stateless across records; the connector supplies its
    # source-locale hook at construction so the same class serves any T2 system
    # without subclassing.
    def __init__(self, *, source_locale_of=None):
        self._source_locale_of = source_locale_of  # callable(conn, record) -> locale | None

    def exists_target(self, client: SubResourceClient, record_id: str, target_locale: str) -> bool:
        return client.get_translation(record_id, target_locale) is not ABSENT

    def write_target(
        self,
        client: SubResourceClient,
        record_id: str,
        target_locale: str,
        fields: dict,
        *,
        draft: bool = True,
    ) -> WriteResult:
        payload = dict(fields or {})
        payload["draft"] = draft
        # Serialize concurrent writers for this (connection, record, locale) so the
        # exists-check and the create/update it decides are ONE atomic span (audit
        # D1). The connector carries `connection_id` (set by store.build/build_fresh);
        # None (e.g. a directly-constructed connector) still keys correctly, just
        # per (record, locale). The re-check runs INSIDE the lock -- classic TOCTOU
        # discipline -- so the second thread updates rather than duplicating.
        lock = _write_lock_for(getattr(client, "connection_id", None), record_id, target_locale)
        with lock:
            if self.exists_target(client, record_id, target_locale):
                return client.update_translation(record_id, target_locale, payload)
            return client.create_translation(record_id, target_locale, payload)


class FieldMapClient(Protocol):
    """The minimal transport a FieldMapStrategy (T1) drives. A T1 connector
    implements these two over its own authed client; the strategy composes them
    into the framework's CARDINAL T1/T3 write rule (design §2): read the CURRENT
    record, merge the target locale's block into the captured locale map, PUT the
    WHOLE map back.

    get_record returns the connector's FULL current record dict (so the merge never
    drops OTHER locales' content), or ABSENT when the record no longer exists.
    put_record writes the whole merged locale-map container back and reports created
    (a locale key that was absent before) vs updated."""

    connection_id = None  # set by store.build/build_fresh; keys the D1 record lock

    def get_record(self, record_id: str):  # -> dict | ABSENT
        ...

    def put_record(self, record_id: str, container: dict, *, created: bool) -> WriteResult: ...


class FieldMapStrategy(LocaleMappingStrategy):
    """T1 — field-level {locale: value} map carried on ONE record.

    Intercom is the reference consumer:
    an Article holds default-locale content at the TOP LEVEL plus a
    `translated_content` map keyed by locale; localizing locale X = writing X's key
    into that map. There is NO per-locale sub-resource to 404-probe (that is T2) —
    the create-vs-update decision is simply "is the locale key already present in
    the map?".

    write_target follows the framework's single most important rule (design §2, the
    T1/T3 branch): GET the current record, deep-merge the target locale's new field
    block into the captured map, and PUT the WHOLE map back. That is robust whether
    the source system merges or replaces the map (Intercom's merge-vs-replace is
    documented [UNCERTAIN]).

    Vendor-neutral, same posture as SubResourceStrategy: every system-specific shape
    is an injected callable, so no per-vendor subclass is needed —
      source_locale_of : callable(conn, record) -> the record's OWN default/source
                         locale (never written as a target — Intercom gotcha #1)
      locale_key       : callable(internal_locale) -> the source system's EXACT map
                         key (Intercom: 'de-de'->'de', 'pt-br'->'pt-BR', 'zh-cn'->
                         'zh-CN'). A wrong key is SILENTLY ignored by Intercom, so
                         this is the correctness linchpin; applied to the written
                         block key and the existence probe.
      finalize_block   : callable(block, *, draft) -> block, to stamp system-required
                         per-block fields after the merge (Intercom: state=draft/
                         published + the `type` discriminator)
      map_key          : the record attribute holding the locale map
                         ('translated_content')
    """

    topology = "T1"

    def __init__(
        self,
        *,
        source_locale_of=None,
        locale_key=None,
        finalize_block=None,
        map_key="translated_content",
    ):
        self._source_locale_of = source_locale_of
        self._locale_key = locale_key
        self._finalize_block = finalize_block
        self._map_key = map_key

    def _key(self, target_locale: str) -> str:
        """The source system's exact map key for an internal locale (identity when
        no locale_key hook is supplied)."""
        return self._locale_key(target_locale) if self._locale_key else target_locale

    def exists_target(self, client: FieldMapClient, record_id: str, target_locale: str) -> bool:
        """A locale exists iff its key is present in the record's locale map — an
        explicit key-presence signal, never a content comparison."""
        rec = client.get_record(str(record_id))
        if rec is ABSENT or not isinstance(rec, dict):
            return False
        block = (rec.get(self._map_key) or {}).get(self._key(target_locale))
        return isinstance(block, dict)

    def write_target(
        self,
        client: FieldMapClient,
        record_id: str,
        target_locale: str,
        fields: dict,
        *,
        draft: bool = True,
    ) -> WriteResult:
        # T1 read-merge-write IS an exists->act span across the network (GET record,
        # merge one locale, PUT the whole map). At jobs.WORKER_COUNT==2 a scheduled
        # run and a webhook run can drive it for the SAME record concurrently, each
        # merging its OWN locale into the pre-merge map, and the second PUT clobbers
        # the first locale (lost update). The D1 striped lock serializes them — keyed
        # on (connection, RECORD) ONLY (see _record_write_lock_for) so two DIFFERENT
        # locales of the same record serialize, which a locale-keyed lock would not.
        # Same single-process caveat as the T2 lock. T1-SPECIFIC widening of that
        # cross-process residual: because the write is a whole-map PUT, a concurrent
        # human edit to ANY OTHER locale made between our GET and PUT is overwritten
        # (T2's sub-resource PUT only races the SAME locale). Cross-process conditional
        # writes (ETag/If-Match) remain ADR-003 Tier 3.
        lock = _record_write_lock_for(getattr(client, "connection_id", None), record_id)
        with lock:
            current = client.get_record(str(record_id))
            if current is ABSENT or not isinstance(current, dict):
                return WriteResult(False, False, message=f"record {record_id} not found")
            key = self._key(target_locale)
            # CARDINAL T1 guard (Intercom gotcha #1): the record's TOP-LEVEL fields
            # ARE its default/source-locale content, so writing that same locale as a
            # "translation" corrupts the source. The flow's partition already drops
            # the CONFIGURED source locale globally; this is the per-RECORD belt-and-
            # suspenders for a workspace whose articles don't all share one
            # default_locale. Benign no-op success (no write, nothing to retry).
            src = ""
            if self._source_locale_of:
                src = self._source_locale_of(None, current) or ""
            if src and str(src).strip().lower() == str(key).strip().lower():
                return WriteResult(
                    True, False, message="skipped: target equals record default/source locale"
                )
            container = dict(current.get(self._map_key) or {})
            # Drop the container-level discriminator (Intercom:
            # 'type':'article_translated_content') so it is never PUT back as if it
            # were a locale key. [UNCERTAIN] whether Intercom requires it on write;
            # the documented update example omits it, so omitting is the
            # documented-safe choice — re-verify on a throwaway article.
            container.pop("type", None)
            existed = isinstance(container.get(key), dict)
            block = dict(container.get(key) or {})
            block.update(fields or {})
            if self._finalize_block:
                block = self._finalize_block(block, draft=draft)
            container[key] = block
            return client.put_record(str(record_id), container, created=not existed)


class T3DocClient(Protocol):
    """The minimal transport a DocPerLocaleStrategy (T3) drives. A T3 connector
    implements these four over its own authed client; the strategy composes them
    with the PERSISTED ID MAP into the reserve-then-fill + reconciliation lifecycle.
    The strategy owns all map/status logic; the connector owns only the external
    API calls (create the target page, fill its blocks, verify by id, detect a
    pre-existing collision).

    connection_id keys the map (per-connection) + the reserve lock; store.build/
    build_fresh sets it.

    create_target(source_id, locale, fields) -> (target_id | None, error) — CREATE
        (reserve) an empty/titled target doc/page and return its external id. Must
        NOT fill the body (that is fill_target) so a crash right after leaves a
        findable, near-empty partial. error is a human string on failure.
    fill_target(target_id, fields) -> WriteResult — write/replace the target's body
        (multi-batch if needed) + any property (title). Idempotent by target id
        (a resumed build re-runs it), so it is safe to call again on the SAME id.
    verify_target(target_id) -> bool — does the target STILL EXIST in the system?
        A fetch of the id; False ONLY on a definite 404/archived; raise
        TargetUnverifiable on any other failure (503/429/timeout) so the pair is
        held, not orphaned + recreated (#444). NEVER a title/content match (the
        SPEC-t3 reconciliation rule: verify = fetch the id).
    find_preexisting(source_id, locale, fields) -> target_id | None — the rule-6
        collision probe: is there ALREADY a target for this (source, locale) that
        our map never created (a pre-existing human translation)? Returns its id, or
        None when none is found OR the API can't detect one. A returned id makes the
        strategy fail closed (needs_link) — never overwrite."""

    connection_id = None

    def create_target(self, source_id: str, locale: str, fields: dict):  # -> (id|None, err)
        ...

    def fill_target(self, target_id: str, fields: dict) -> WriteResult: ...

    def verify_target(self, target_id: str) -> bool: ...

    def find_preexisting(self, source_id: str, locale: str, fields: dict):  # -> id|None
        ...


class DocPerLocaleStrategy(LocaleMappingStrategy):
    """T3 — a separate document/page per locale, keyed by OUR persisted id map.

    Notion is the reference consumer
    (§3): there is NO native localization — a "translation" is a duplicated page the
    connector creates, and the connector owns the entire source->target id map. This
    strategy implements the SPEC-t3 T3 invariant (reserve-then-fill, resumable) AND
    the 7-case ID-map reconciliation state table against `ConnectorIdMap` (via the
    idmap module), so every T3 connector inherits identical, reviewed idempotency.

    write_target is the whole of it — driven per (source, locale) by the shared
    flow engine's write-back stage. Reconciliation (map says / system says ->
    action), keyed on the PERSISTED row + a verify-by-id (never title/content):

      1  no row,       collision-free -> create -> reserve(building) -> fill -> done
      6  no row,       target pre-exists -> needs_link, FAIL CLOSED (never overwrite)
      2  done,         target present -> fill in place (update), keep id
      3  done,         target GONE -> orphan the dead id, then rule 1 (never resurrect)
      4  building,     target present -> resume fill, flip to done
      5  building,     target absent -> drop the stale row, then rule 1
      7  locale out of scope -> the caller never iterates it, so the row + target are
         left untouched (scope narrowing must not delete content).

    Rule 6 is load-bearing: T3's worst failure is overwriting human work the map
    never knew about. exists_target consults the MAP (a `done` row), never content,
    so a blank/partial translation can't masquerade as done.

    Vendor-neutral, same posture as the T1/T2 strategies: the connector supplies its
    source-locale hook plus the four T3DocClient methods + `connection_id`.
    """

    topology = "T3"

    def __init__(self, *, source_locale_of=None):
        self._source_locale_of = source_locale_of  # callable(conn, record) -> locale | None

    def exists_target(self, client: T3DocClient, record_id: str, target_locale: str) -> bool:
        """True ONLY when a `done` map row exists (SPEC-t3: exists_target reads the
        MAP, never content — the 'silent English fallback' gotcha means reading the
        target page can't prove a translation exists). A building/needs_link/orphaned
        row is deliberately NOT 'exists', so the pair falls through to write_target
        and the reconciliation table resolves it (resume / fail-closed / recreate)."""
        from app.services.connectors import idmap

        cid = getattr(client, "connection_id", None)
        if cid is None:
            return False
        row = idmap.get(cid, str(record_id), target_locale)
        return bool(row and row.status == idmap.DONE)

    def write_target(
        self,
        client: T3DocClient,
        record_id: str,
        target_locale: str,
        fields: dict,
        *,
        draft: bool = True,
    ) -> WriteResult:
        from app.services.connectors import idmap

        cid = getattr(client, "connection_id", None)
        if cid is None:
            return WriteResult(
                False,
                False,
                message="T3 write_target needs a connection_id (the id map is per-connection).",
            )
        src = str(record_id)
        # Serialize the whole read-decide-reserve-fill span for this (connection,
        # source, locale) so a concurrent run RESUMES the reserved row rather than
        # POSTing a second target (the reserve-idempotency point; the unique index is
        # the cross-process backstop). Single-process caveat = the T2 D1 lock's.
        with idmap.write_lock(cid, src, target_locale):
            row = idmap.get(cid, src, target_locale)
            if row is None:
                return self._create_path(client, cid, src, target_locale, fields)  # rule 1/6
            if row.status == idmap.NEEDS_LINK:
                # rule 6, still unresolved -> keep failing closed (never overwrite a
                # pre-existing human translation until an operator adopts/ignores it).
                return WriteResult(
                    False,
                    False,
                    target_id=row.target_id,
                    message=f"needs_link unresolved for {src}/{target_locale} (adopt or ignore it)",
                )
            if row.status == idmap.ORPHANED:
                # a prior done target was already found deleted -> recreate fresh.
                return self._create_path(client, cid, src, target_locale, fields)
            present = self._verify(client, row.target_id)
            if present is None:
                # Issue #444: the verify probe itself failed (503/429/401/timeout). That is
                # NOT "the target is gone": orphaning + recreating here would leave the live
                # (possibly published) translation untracked and mint a duplicate. Hold the
                # pair, change nothing, and let the next run verify again.
                return WriteResult(
                    False,
                    False,
                    target_id=row.target_id,
                    message=f"could not verify target {row.target_id} for {src}/{target_locale} "
                    f"(transient failure); held for retry, nothing was changed",
                )
            if row.status == idmap.DONE:
                if present:
                    return self._fill_existing(
                        client, cid, src, target_locale, row.target_id, fields, created=False
                    )  # rule 2
                # rule 3: done row, target GONE (deleted/404). Orphan the dead id
                # (never resurrect it) + recreate. Loud — a human deleted our page.
                idmap.set_status(cid, src, target_locale, idmap.ORPHANED)
                log.warning(
                    "T3 reconcile: %s/%s target %s is GONE (deleted in the "
                    "system) — orphaning + recreating.",
                    src,
                    target_locale,
                    row.target_id,
                )
                return self._create_path(client, cid, src, target_locale, fields)
            # row.status == building
            if present:
                # rule 4: resume the half-done build (PATCH the existing target), flip
                # to done when complete. `created` True: it reaches done this cycle.
                return self._fill_existing(
                    client, cid, src, target_locale, row.target_id, fields, created=True
                )
            # rule 5: building row but target absent (crash between create-intent and
            # reserve-persist, or deleted mid-build) -> drop the stale row + recreate.
            # Never PATCH a 404 id in a loop.
            log.warning(
                "T3 reconcile: %s/%s building row's target %s is absent — "
                "dropping the stale row + recreating.",
                src,
                target_locale,
                row.target_id,
            )
            idmap.delete(cid, src, target_locale)
            return self._create_path(client, cid, src, target_locale, fields)

    # --- reconciliation helpers ---
    def _verify(self, client: T3DocClient, target_id) -> bool | None:
        """True = present, False = definitively absent, None = could not tell (the probe
        failed transiently). Only a definite False may drive an orphan + recreate (#444)."""
        if not target_id:
            return False
        try:
            return bool(client.verify_target(str(target_id)))
        except TargetUnverifiable:
            log.warning("T3 verify_target could not verify %s; holding the pair.", target_id)
            return None
        except Exception:
            log.warning("T3 verify_target crashed for %s; holding the pair.", target_id)
            return None

    def _create_path(
        self, client: T3DocClient, cid: int, src: str, loc: str, fields: dict
    ) -> WriteResult:
        """Rule 1 (happy path) with the rule-6 pre-existing-target guard in front.
        Reserve (persist the id as `building`) BEFORE filling, so a crash leaves a
        findable partial rather than an orphan."""
        from app.services.connectors import idmap

        pre = self._find_preexisting(client, src, loc, fields)
        if pre is not None:
            # RULE 6: a target for (src, loc) already exists that our map never made
            # (a pre-existing human translation or a lost map). FAIL CLOSED — record
            # needs_link, do NOT create/overwrite/adopt. Silent adoption is how a
            # hand-written translation gets clobbered.
            idmap.reserve(cid, src, loc, pre, status=idmap.NEEDS_LINK)
            return WriteResult(
                False,
                False,
                target_id=pre,
                message=f"needs_link: a pre-existing target for {src}/"
                f"{loc} was found (id {pre}) — not overwritten; "
                f"adopt or ignore it",
            )
        try:
            tid, err = client.create_target(src, loc, fields)
        except Exception as e:
            return WriteResult(False, True, message=f"{type(e).__name__}: {e}")
        if err or not tid:
            return WriteResult(False, True, message=err or "create_target returned no id")
        # Reserve immediately: id persisted as `building` (the T3 invariant step 1).
        idmap.reserve(cid, src, loc, str(tid), status=idmap.BUILDING)
        return self._fill_existing(client, cid, src, loc, str(tid), fields, created=True)

    def _fill_existing(
        self,
        client: T3DocClient,
        cid: int,
        src: str,
        loc: str,
        target_id: str,
        fields: dict,
        *,
        created: bool,
    ) -> WriteResult:
        """Fill/replace the target's content by id, flipping the row to `done` only
        on a verified-complete write. A fill failure LEAVES the row `building` so the
        next run resumes it (never a duplicate POST)."""
        from app.services.connectors import idmap

        try:
            res = client.fill_target(str(target_id), fields)
        except Exception as e:
            return WriteResult(
                False, created, target_id=str(target_id), message=f"{type(e).__name__}: {e}"
            )
        if res.ok:
            idmap.set_status(cid, src, loc, idmap.DONE, target_id=str(target_id))
            return WriteResult(
                True, created, target_id=str(target_id), message="created" if created else "updated"
            )
        # Left `building` (the reserve row stands) -> resumable; report the failure.
        return WriteResult(
            False, created, target_id=str(target_id), message=res.message or "fill_target failed"
        )

    def _find_preexisting(self, client: T3DocClient, src: str, loc: str, fields: dict):
        try:
            return client.find_preexisting(src, loc, fields)
        except Exception:
            log.warning("T3 find_preexisting crashed for %s/%s; assuming none.", src, loc)
            return None
