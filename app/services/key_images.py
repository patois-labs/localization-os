"""Per-string in-context image storage + lifecycle (Batch 14 Train T1).

The service layer BOTH the web routes, the /api routes and the `keys image` CLI call, so
the confinement + validation + orphan-free storage ordering live in exactly ONE place
(the dual-surface parity rule). This module NEVER decides tenancy: the caller resolves the
key through `web/keys.py::_grid_key` / `_api_key_row` (the full triple, LAW-IMG-2) and
hands this module a key it already proved the caller may write; `attach_image` sets
`KeyImage.project_id` SERVER-SIDE from that key's project, never from caller input (T-2).

Storage (LAW-IMG-13/16): the blob key is a server-derived flat `keyimage_<uuid4hex>.png`
(passes `blobstore._safe_key`; no caller input reaches a path). The row is committed with
`ready=False` BEFORE the blob is put, then flipped `ready=True` after -- so a crash between
the two leaves a findable, skippable stub, never a blob no committed row references. The
stored blob is the upload-time downscaled + re-encoded PNG (<= CONTEXT_MAX_EDGE), so
send-time is a pure blob read (M-5).

NO OCR, NO bytes in any prompt/log/tap (LAW-IMG-5): this module only stores + serves
pixels; the vision injection into the translation call is Train T2's job (providers /
orchestrator), which this module never touches.

Documented in: docs/architecture/image-localization.md
"""

import hashlib
import io
import json
import re
import tempfile
import unicodedata
import uuid
import zipfile
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import config
from app.models import KeyImage, StringKey
from app.services import image_localization, pii, secrets_scrub, settings
from app.services import keys as keys_svc

# --- constants (section 4.2) ------------------------------------------------
#: Hard per-key image cap, enforced server-side atomically (LAW-IMG-11).
MAX_IMAGES_PER_KEY = 5
#: Images sent on ONE translation request by default (S3: the primary only). Raised to
#: MAX_IMAGES_PER_KEY for a key whose send_all_images is set. (Consumed by Train T2.)
MAX_IMAGES_PER_REQUEST = 1
#: Per-image usage-fallback token allowance so the spend ledger is not blind to vision
#: cost when a provider reports no usage (M-3). (Consumed by Train T2.)
KEY_IMAGE_TOKEN_ESTIMATE = 1300


class KeyImageError(Exception):
    """A clean, expected upload/lifecycle failure (bad image, over caps, duplicate) --
    the caller turns it into a 4xx. Never leaks a blob or a half-written row.

    `code` is an OPTIONAL stable machine-readable error code (L4): the export-cap refusals
    carry `code="key_image_export_max"` so the `--remote` CLI arm can re-class the exit code
    from a STRUCTURED field the server owns, not a substring sniff on the human message (which
    a reword would silently break). `_api_error` surfaces it as a `code` field in the JSON."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


def enabled() -> bool:
    """The Batch-14 image-context kill switch (setting key_image_context_enabled, default
    on). An unreadable Setting table (bare import-time contexts) reads as the DEFAULT (on),
    the same fail-to-default posture image_localization.enabled / keys.enabled document."""
    try:
        return str(settings.get("key_image_context_enabled", "1")) == "1"
    except Exception:
        return True


# --- validation + downscale (LAW-IMG-10, section 4.4) -----------------------
def validate_and_render(data: bytes) -> dict:
    """RAW upload bytes -> a dict of the send-ready PNG rendition + metadata, or raise
    KeyImageError. Order (LAW-IMG-10): sniff_kind (raster png/jpeg only, S1 -- svg + all
    else rejected) -> open_raster (pixel-bomb header guard BEFORE decode) -> content_hash
    of the RAW bytes (Fernet-independent dedup) -> downscale ONCE to <= CONTEXT_MAX_EDGE +
    re-encode PNG (M-5). Never stores; the caller owns persistence."""
    kind = image_localization.sniff_kind(data)
    if kind not in ("png", "jpeg"):
        raise KeyImageError(
            "unsupported image: only PNG or JPEG raster images are accepted (SVG and other "
            "formats are rejected)."
        )
    try:
        im = image_localization.open_raster(data)  # pixel-bomb guard, original dims
    except image_localization.ImageLocalizationError as exc:
        raise KeyImageError(f"that image could not be read: {exc}") from exc
    orig_w, orig_h = im.size
    content_hash = hashlib.sha256(data).hexdigest()
    # Downscale ONCE at upload + re-encode PNG (the context_brief_for_image idiom): the
    # stored blob is send-ready, so a 6-plural x 10-locale batch never re-decodes it.
    im = im.convert("RGB")
    if max(im.size) > image_localization.CONTEXT_MAX_EDGE:
        scale = image_localization.CONTEXT_MAX_EDGE / max(im.size)
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png = buf.getvalue()
    return {
        "png": png,
        "content_hash": content_hash,
        "orig_fmt": ("jpg" if kind == "jpeg" else "png"),
        "orig_width": int(orig_w),
        "orig_height": int(orig_h),
        "width": int(im.width),
        "height": int(im.height),
        "byte_size": len(png),
    }


# --- queries ----------------------------------------------------------------
def _live_rows(session: Session, key_id: int, project_id: int | None = None) -> list[KeyImage]:
    """Non-deleted rows for a key (ready or stub), ordinal-then-id ordered. `project_id`,
    when given, adds the LAW-IMG-2 tenant-anchor leg (F-2: the list path asserts the full
    triple like every sibling, defense-in-depth even though project_id is server-set)."""
    q = select(KeyImage).where(KeyImage.key_id == key_id, KeyImage.deleted_at.is_(None))
    if project_id is not None:
        q = q.where(KeyImage.project_id == project_id)
    return list(session.exec(q.order_by(KeyImage.ordinal, KeyImage.id)).all())


def list_images(session: Session, key: StringKey, *, ready_only: bool = True) -> list[KeyImage]:
    """The metadata list the list surfaces show: non-deleted rows for a key, confined by the
    LAW-IMG-2 triple (key.id + key.project_id, F-2). Only ready=True rows by default; pass
    ready_only=False to also surface a `ready=False` stub (a failed-upload row the caller
    can then delete -- M-2, so a stub is never an unreachable, unremovable slot-eater)."""
    rows = _live_rows(session, key.id, key.project_id)
    return [r for r in rows if r.ready] if ready_only else rows


def images_for_keys(
    session: Session, key_ids: list[int], project_id: int
) -> dict[int, list[KeyImage]]:
    """{key_id: [ready images]} for a batch of keys in ONE grouped query -- the grid
    renders the per-key badge + gallery from this without an N+1 (M-3). Ready rows only,
    confined by the project_id tenant leg (LAW-IMG-2)."""
    if not key_ids:
        return {}
    rows = session.exec(
        select(KeyImage)
        .where(
            KeyImage.key_id.in_(key_ids),
            KeyImage.project_id == project_id,
            KeyImage.deleted_at.is_(None),
            KeyImage.ready == True,  # noqa: E712
        )
        .order_by(KeyImage.key_id, KeyImage.ordinal, KeyImage.id)
    ).all()
    out: dict[int, list[KeyImage]] = {}
    for r in rows:
        out.setdefault(r.key_id, []).append(r)
    return out


def get_image(
    session: Session, key: StringKey, image_id: int, *, include_pending: bool = False
) -> KeyImage | None:
    """One image, CONFINED to `key` by the FULL triple (LAW-IMG-2): the row must exist,
    be non-deleted, belong to THIS key (image.key_id == key.id) AND carry the same tenant
    anchor (image.project_id == key.project_id -- the leg `_key_project`/`_api_project`
    alone does NOT give). A mismatch returns None -> the caller answers a non-enumerable
    404, so a foreign image_id paired with an owned key_id is indistinguishable from a
    never-existed id (T-1). By default a `ready=False` stub is NOT returned (L-2: it must
    not become primary or be served); a DELETE passes include_pending=True so a stub is
    reachable for removal (M-2)."""
    img = session.get(KeyImage, image_id)
    if (
        img is None
        or img.deleted_at is not None
        or img.key_id != key.id
        or img.project_id != key.project_id
    ):
        return None
    if not include_pending and not img.ready:
        return None
    return img


# --- attach (LAW-IMG-11/13/16) ----------------------------------------------
def _next_ordinal(existing: list[KeyImage]) -> int:
    """The lowest free ordinal in 0..MAX-1 among the LIVE rows (L-1): gap-filling keeps
    ordinals unique among live rows AND inside 0..MAX-1 even after a soft-delete opened a
    gap, so `_ensure_primary`'s "lowest surviving ordinal" and the ux_keyimage_key_ordinal
    partial-unique backstop both stay well-defined. The cap guarantees a free slot exists."""
    used = {r.ordinal for r in existing}
    for i in range(MAX_IMAGES_PER_KEY):
        if i not in used:
            return i
    return len(existing)  # unreachable while the cap holds; a safe monotone fallback


def _ensure_primary(session: Session, key_id: int) -> None:
    """Guarantee EXACTLY-one primary among a key's ready rows (M-1): if some ready row is
    already primary, nothing changes; otherwise the lowest-ordinal ready row is promoted.
    Called at the END of every attach and delete, so a key that HAS a ready image can never
    end up with no primary (which would resolve zero images on a vision engine -> a silent
    image loss handed to T2, T-9)."""
    ready = [r for r in _live_rows(session, key_id) if r.ready]
    if not ready or any(r.is_primary for r in ready):
        return
    ready[0].is_primary = True
    session.add(ready[0])


def _finalize_blob(
    session: Session, key: StringKey, row: KeyImage, png: bytes, uploaded_by: str
) -> KeyImage:
    """Put the blob, flip ready, recompute the primary, announce on the feed -- the steps
    shared by a fresh attach AND a stub resume (M-2). L-6: a BlobStore failure is wrapped
    into KeyImageError so the route answers a clean 4xx ('please retry') and the row stays a
    findable, re-uploadable ready=False stub -- never a raw OSError 500."""
    from app.ports import blobstore

    try:
        blobstore.get().put(row.blob_key, png)  # Fernet-at-rest free via the port
    except OSError as exc:
        raise KeyImageError(
            f"the image could not be stored; the upload is saved as pending, please retry: {exc}"
        ) from exc
    row.ready = True
    session.add(row)
    _ensure_primary(session, key.id)  # M-1: a ready key always has exactly one primary
    keys_svc._log(session, key, entity="key", op="updated", actor=uploaded_by or None)
    session.commit()
    session.refresh(row)
    return row


def attach_image(
    session: Session,
    key: StringKey,
    data: bytes,
    *,
    caption: str = "",
    uploaded_by: str = "",
) -> KeyImage:
    """Validate -> resume-stub-or-atomic-insert -> blob put -> ready. `project_id` is set
    SERVER-SIDE from `key.project_id` (T-2). Raises KeyImageError on any validation / cap /
    dedup failure (no row, no blob written).

    ATOMICITY (LAW-IMG-11, H-1). The cap + dedup are enforced by BOTH a per-key serialize
    AND two DB-level partial-unique backstops, so a check-then-insert race cannot exceed the
    cap or duplicate bytes on ANY backend:
      * the parent StringKey row is locked FOR UPDATE (serializes concurrent attaches per
        key on Postgres READ COMMITTED; a no-op on SQLite, which serializes writers anyway);
      * ux_keyimage_key_hash `(key_id, content_hash) WHERE deleted_at IS NULL` closes the
        dedup race (two concurrent identical inserts -> one IntegrityError);
      * ux_keyimage_key_ordinal `(key_id, ordinal) WHERE deleted_at IS NULL` closes the cap
        race -- ordinals live only in 0..MAX-1, so at most MAX rows can coexist per key; a
        racing insert that would exceed it collides on the ordinal and is refused.
    A duplicate/cap IntegrityError on commit is rolled back and surfaced as KeyImageError.
    """
    meta = validate_and_render(data)

    # Serialize concurrent attaches on THIS key (Postgres row lock; SQLite no-op). The
    # count+insert below is then race-free on Postgres, and the two partial-unique indexes
    # backstop both backends regardless (H-1). SQLite has no row locks -- the suppressed
    # error path there is fine because the DB backstop indexes carry the guarantee.
    import contextlib as _ctx

    with _ctx.suppress(Exception):
        session.exec(select(StringKey.id).where(StringKey.id == key.id).with_for_update())

    existing = _live_rows(session, key.id, key.project_id)
    # Dedup, but a `ready=False` stub of the SAME bytes is RESUMED, not refused (M-2): the
    # stub is a failed prior upload of exactly this image, so re-uploading it finishes it.
    for r in existing:
        if r.content_hash == meta["content_hash"]:
            if r.ready:
                raise KeyImageError("that image is already attached to this key.")
            return _finalize_blob(session, key, r, meta["png"], uploaded_by)
    if len(existing) >= MAX_IMAGES_PER_KEY:
        raise KeyImageError(
            f"this key already has the maximum of {MAX_IMAGES_PER_KEY} images; delete one "
            "before adding another."
        )
    row = KeyImage(
        key_id=key.id,
        project_id=key.project_id,  # SERVER-SIDE tenant anchor (T-2)
        blob_key=f"keyimage_{uuid.uuid4().hex}.png",
        content_hash=meta["content_hash"],
        mime="image/png",
        fmt="png",
        orig_fmt=meta["orig_fmt"],
        byte_size=meta["byte_size"],
        width=meta["width"],
        height=meta["height"],
        orig_width=meta["orig_width"],
        orig_height=meta["orig_height"],
        caption=caption or "",
        ordinal=_next_ordinal(existing),  # L-1: unique among live rows, in 0..MAX-1
        is_primary=False,  # M-1: the primary is recomputed over ready rows in _finalize
        ready=False,
        uploaded_by=uploaded_by or "",
    )
    session.add(row)
    try:
        session.commit()  # (2) committed BEFORE the blob put (LAW-IMG-16); DB backstops fire here
    except IntegrityError:
        session.rollback()
        raise KeyImageError(
            "that image could not be attached (a duplicate or the per-key cap was hit under "
            "a concurrent write); please retry."
        ) from None
    session.refresh(row)
    return _finalize_blob(session, key, row, meta["png"], uploaded_by)


# --- lifecycle --------------------------------------------------------------
def delete_image(session: Session, key: StringKey, image: KeyImage, *, actor: str = "") -> None:
    """SOFT-delete one image (the blob is reaped by project delete / the retention
    backstop, not here -- LAW-IMG-15), promote a new primary if this was the primary, and
    append a KeyChangeLog(entity=key, op=updated). The caller has already confined `image`
    to `key` via get_image (the triple)."""
    from app.models import _utcnow

    image.deleted_at = _utcnow()
    image.is_primary = False
    session.add(image)
    session.flush()
    _ensure_primary(session, key.id)  # promote the lowest surviving ready ordinal (M-1)
    keys_svc._log(session, key, entity="key", op="updated", actor=actor or None)
    session.commit()


def set_primary(session: Session, key: StringKey, image: KeyImage, *, actor: str = "") -> None:
    """Make `image` the key's primary (the one sent by default, S3), clearing the flag on
    every sibling. `image` is already confined to `key` by the caller (get_image), which
    also refuses a `ready=False` stub (L-2). NO KeyChangeLog: §4.4 specifies delta rows on
    attach/detach only, so a selection-only change appends none (I-1)."""
    for r in _live_rows(session, key.id, key.project_id):
        if r.is_primary and r.id != image.id:
            r.is_primary = False
            session.add(r)
    image.is_primary = True
    session.add(image)
    session.commit()


# --- serve (LOW-1) ----------------------------------------------------------
def read_bytes(image: KeyImage) -> bytes | None:
    """The STORED PNG bytes for one image, or None when the row is a not-ready stub OR the
    blob is absent (a crash between row-commit and put, LAW-IMG-16). The caller turns None
    into a non-enumerable 404, never a 500 -- mirroring the send-path swallow (LOW-1)."""
    if not image.ready or not image.blob_key:
        return None
    from app.ports import blobstore

    try:
        return blobstore.get().get(image.blob_key)
    except (OSError, FileNotFoundError):
        return None


def view(image: KeyImage) -> dict:
    """The metadata dict every list/detail surface returns (NEVER bytes)."""
    return {
        "id": image.id,
        "key_id": image.key_id,
        "caption": image.caption,
        "mime": image.mime,
        "fmt": image.fmt,
        "byte_size": image.byte_size,
        "width": image.width,
        "height": image.height,
        "orig_width": image.orig_width,
        "orig_height": image.orig_height,
        "orig_fmt": image.orig_fmt,
        "ordinal": image.ordinal,
        "is_primary": image.is_primary,
        "send_all_images": image.send_all_images,
        "ready": image.ready,
        "uploaded_at": image.uploaded_at.isoformat() if image.uploaded_at else None,
    }


# Re-exported cap so the JSON attach route (web/keys.py) and the read_capped_upload call
# read one name (the config-backed byte ceiling, section 4.2).
MAX_BYTES = config.KEY_IMAGE_MAX_BYTES


# --- include_string_images export bundle (Batch 14 Phase 1.5, §10) ----------
#: The manifest schema tag stamped into a bundle's manifest.json, so an importer/auditor
#: can recognise the shape by value, not by position.
EXPORT_BUNDLE_SCHEMA = "loc-studio-key-image-export/v1"
#: The in-ZIP subtree that carries the image blobs. Blobs are placed under it by their
#: server-derived flat blob_key (``keyimage_<uuid>.png``) -- NOT by key name: a key name is
#: caller-authored (dotted paths, ``::``, arbitrary unicode) and would be a ZIP path-traversal
#: + collision hazard, whereas the blob_key is uuid-flat and already ``_safe_key``-clean. The
#: manifest is what links a key to its image path, so the path need not be human-legible.
EXPORT_IMAGE_PREFIX = "images/"

#: Above this many bytes the in-progress ZIP spills from RAM to a temp file on disk, so a
#: whole-project bundle at the byte cap is never fully resident in memory (M2). A modest
#: threshold keeps the common small bundle in memory (fast) while a large one streams.
EXPORT_SPOOL_MAX_IN_MEMORY = 8 * 1024 * 1024

#: A ZIP-member path segment reduced to a value that can never walk an extractor out of its
#: target directory: control chars stripped, leading dots removed (so ``..`` / ``.hidden``
#: cannot traverse or hide), path separators are the split boundary (never survive INSIDE a
#: segment). An empty / ``.`` / ``..`` segment is dropped by the caller. The range covers BOTH
#: the C0 controls + DEL (``\x00-\x1f\x7f``) AND the C1 controls (``\x80-\x9f``), which some
#: filesystems/extractors also reject in a name (L7).
_ZIP_SEG_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: Characters that are illegal in a filename on a Windows extractor (and the drive-letter
#: colon), stripped from every ZIP-member SEGMENT so the artifact -- documented as a vendor/CI
#: hand-off -- extracts cleanly on Windows too (L10), and so a ``C:``-style drive segment is
#: reduced to a plain relative name (L1). ``/`` and ``\`` are NOT here: they are the split
#: boundary and can never survive inside a segment.
_ZIP_SEG_WIN_ILLEGAL = re.compile(r'[?:*<>|"]')

#: Windows RESERVED DEVICE NAMES (CON, PRN, AUX, NUL, COM1-9, LPT1-9), reserved WITH OR WITHOUT
#: an extension -- a Windows extractor refuses ``CON``, ``CON.txt`` and ``CON.en-US.json`` alike.
#: A segment whose first dot-delimited component is one of these has an underscore folded in after
#: that component (``CON.en-US.json`` -> ``CON_.en-US.json``) so it extracts cleanly (L6).
_ZIP_SEG_WIN_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)")

#: Credential ASSIGNMENTS a caption can carry that are NOT key-shaped (so scrub_secrets'
#: prefix patterns miss them) and are NOT structured PII (so the pii regex seam misses them):
#: ``secret=hunter2``, ``password: ...``, ``token = ...``. Confined to the caption export path.
_CAPTION_SECRET_ASSIGN = re.compile(
    r"(?i)\b(?:secret|password|passwd|pwd|token|api[-_]?key)\b\s*[:=]\s*\S+"
)


def _zip_safe_member(arcname: str, *, fallback: str = "export") -> str:
    """Reduce a proposed ZIP member name to a traversal-proof RELATIVE path.

    THE bundle security boundary (H1). keys.md documents the ZIP as a vendor/CI hand-off, so
    the extractor is routinely third-party tooling, NOT Python's guarded ``extractall``. Every
    segment is basenamed (split on ``/`` and ``\\`` so neither can survive inside a segment),
    stripped of control chars and leading dots, and any ``.`` / ``..`` / empty segment is
    dropped. So ``../../../../evil.json`` -> ``evil.json`` and ``/etc/cron.d/pwn.json`` ->
    ``etc/cron.d/pwn.json`` (relative, cannot climb). An entirely unusable name -> ``fallback``.
    Applied to EVERY member by ``_zip_write``; the key-file member is additionally basenamed to a
    single segment by ``_zip_safe_key_file`` so it can never sprout subdirectories.

    Compatibility hardenings (gate rounds 2/3, defence in depth): the name is first NFKC-folded so
    a FULLWIDTH dot (``\uff0e``) or fullwidth solidus (``\uff0f``) collapses to its ASCII form and
    cannot smuggle a ``..`` traversal or a hidden separator past the ASCII checks below (L1); each
    segment is stripped of Windows-illegal filename chars incl. the drive-letter colon, so
    ``C:\\evil\\pwn`` becomes the relative ``C/evil/pwn``-shaped name and ``?:*<>|"`` never reach a
    Windows extractor (L1/L10); C0+DEL and C1 control chars are stripped (L7); and a Windows
    RESERVED DEVICE NAME segment (``CON``, ``COM1``, ...) is de-reserved with an underscore so it
    too extracts cleanly on Windows (L6).
    """
    # NFKC folds fullwidth/compatibility forms to ASCII BEFORE we split, so a fullwidth solidus
    # becomes a real split boundary and a fullwidth dot-dot becomes a droppable ``..`` (L1).
    normalized = unicodedata.normalize("NFKC", str(arcname or ""))
    segs: list[str] = []
    for raw in re.split(r"[\\/]+", normalized):
        seg = _ZIP_SEG_CTRL.sub("", raw)  # L7: C0 + DEL + C1 controls
        seg = _ZIP_SEG_WIN_ILLEGAL.sub("", seg)  # L1/L10: drive colons + Windows-illegal chars
        seg = seg.strip().lstrip(".").strip()
        if not seg or seg in (".", ".."):
            continue
        # L6: de-reserve a Windows device name (CON.en-US.json -> CON_.en-US.json).
        seg = _ZIP_SEG_WIN_RESERVED.sub(lambda m: m.group(1) + "_" + m.group(2), seg)
        segs.append(seg)
    return "/".join(segs) or fallback


def _zip_safe_key_file(name: str) -> str:
    """The key-FILE member name: the download filename reduced to ONE safe basename (H1). A
    caller-authored namespace reaches ``download_filename`` as the stem, and that header
    sanitiser leaves ``/`` and ``..`` intact (harmless in a header the consumer basenames, a
    ZIP-slip in a member name). We basename it here so the member -- and the manifest's
    ``key_file`` and the ``.zip`` download name derived from it -- share one sanitised value.

    L5: NFKC-fold BEFORE the basename split. A COMPATIBILITY separator (fullwidth solidus, etc.)
    is not an ASCII path separator, so it would survive this split and only fold to a real
    separator later inside ``_zip_safe_member`` -- sprouting a second segment and breaking the
    ONE-basename invariant. Folding first makes it a boundary this split consumes, so it stays
    one segment."""
    normalized = unicodedata.normalize("NFKC", str(name or ""))
    last = re.split(r"[\\/]+", normalized)[-1]
    return _zip_safe_member(last, fallback="export.json")


def _scrub_caption(text: str) -> str:
    """Scrub a caption before it rides out in a vendor bundle (L2). THREE seams, because no one
    of them covers the others: ``secrets_scrub`` catches key-SHAPED tokens (``sk-ant...``); the
    ``pii`` regex seam catches structured PII (email, phone, SSN, card, IBAN, IP) -- its
    regex layer runs regardless of the ``pii_redaction`` toggle; and ``_CAPTION_SECRET_ASSIGN``
    catches bare credential assignments (``secret=hunter2``) that are neither key-shaped nor PII."""
    if not text:
        return ""
    scrubbed = secrets_scrub.scrub_secrets(text)
    scrubbed = pii.redact(scrubbed)[0]
    return _CAPTION_SECRET_ASSIGN.sub("[redacted]", scrubbed)


def export_bundle_caps() -> tuple[int, int]:
    """(max_files, max_bytes) for ONE export bundle's image subtree -- settings-backed
    (key_image_export_max_files / key_image_export_max_bytes) with the config fallback. A
    non-numeric setting falls back to the config default rather than crashing an export."""

    def _int(key: str, fallback: int) -> int:
        # Broad guard (the enabled() posture): a non-numeric value OR an unreadable Setting
        # table (bare import-time contexts) both fall back to the config default.
        try:
            return int(settings.get(key, str(fallback)))
        except Exception:
            return fallback

    return (
        _int("key_image_export_max_files", config.KEY_IMAGE_EXPORT_MAX_FILES),
        _int("key_image_export_max_bytes", config.KEY_IMAGE_EXPORT_MAX_BYTES),
    )


def _bundle_key_name(key: StringKey, *, qualify: bool) -> str:
    """The manifest's `key` field, matching the shape the key FILE used: a whole-project
    export qualifies every name (``namespace::name``); a namespace-scoped one keeps bare
    names. Kept in lockstep with key_files.export_locale's own `qualify` derivation so the
    manifest linkage lines up 1:1 with the entries in the key file. key_files is imported
    lazily (it imports keys_svc which this module also imports -- avoid an import cycle)."""
    if not qualify:
        return key.name
    from app.services import key_files

    return key_files.qualified_name(key.namespace or "", key.name)


def build_export_bundle(
    session: Session,
    project,
    locale: str,
    *,
    key_file_bytes: bytes,
    key_file_name: str,
    exported_keys: list[StringKey],
    qualify: bool,
    generated_at: datetime,
) -> tuple[tempfile.SpooledTemporaryFile, dict]:
    """Assemble the include_string_images ZIP bundle and return ``(spooled_file, info)``.

    The returned file-like is a ``SpooledTemporaryFile`` seeked to 0: read it in chunks (see
    ``iter_bundle``) and CLOSE it. It is NOT bytes -- a whole-project bundle at the byte cap is
    never fully resident in memory (M2): the ZIP is written to a spooled temp file that spills to
    disk past ``EXPORT_SPOOL_MAX_IN_MEMORY``, and image blobs are read ONE at a time (each blob is
    transient in the loop, never accumulated).

    Members:
      * ``<key_file_name>``           -- the exact key file bytes export_locale produced
        (json/po), so the bundle is a strict SUPERSET of the plain export. The member name is
        the SANITISED download filename (``_zip_safe_key_file``, H1).
      * ``images/<blob_key>``         -- one member per ready KeyImage of each EXPORTED key.
      * ``manifest.json``             -- the linkage: schema tag + per-image
        ``{key, namespace, name, path, mime, fmt, width, height, ordinal, is_primary,
        caption}``. ``caption`` is scrubbed through THREE seams (``_scrub_caption``: secrets +
        the pii regex seam + credential assignments) before it rides out in the vendor bundle.

    Determinism (M3): the archive is a pure function of ``(keys, images, generated_at)``. Member
    timestamps are fixed (``_zip_write``) and ``generated_at`` is INJECTED by the caller (the
    export's own timestamp) rather than read off the wall clock here, so two builds of the same
    input with the same ``generated_at`` are byte-identical -- which is how the local and
    ``--remote`` CLI arms are made to agree.

    Confinement (LAW-IMG-2): images are fetched via images_for_keys(project_id=project.id),
    and every row in `exported_keys` belongs to `project`, so a foreign key's / foreign org's
    image can never enter the bundle. Caps (export_bundle_caps): the aggregate FILE COUNT and
    total BYTES are checked from the row metadata BEFORE any blob is read or any ZIP byte is
    produced -- an over-cap request raises KeyImageError with no partial artifact.
    """
    proj = keys_svc._project(session, project)
    max_files, max_bytes = export_bundle_caps()
    key_ids = [k.id for k in exported_keys]
    by_key = images_for_keys(session, key_ids, proj.id) if key_ids else {}

    # Pre-flight the caps from the stored row metadata (byte_size), so the refusal is BEFORE
    # any blob read or ZIP write (no partial artifact leaves this function). Ordered walk =
    # deterministic manifest + member order. A duplicate blob_key across two live rows (DB
    # drift, L6) is de-duped so the ZIP never carries two members with the same name.
    planned: list[tuple[StringKey, KeyImage]] = []
    total_bytes = 0
    seen_blob_keys: set[str] = set()
    for key in exported_keys:
        for img in by_key.get(key.id, []):
            if img.blob_key in seen_blob_keys:
                continue
            seen_blob_keys.add(img.blob_key)
            planned.append((key, img))
            total_bytes += int(img.byte_size or 0)
    if len(planned) > max_files:
        raise KeyImageError(
            f"this export would bundle {len(planned)} images, over the per-export limit of "
            f"{max_files} (key_image_export_max_files); narrow the export (a namespace or a "
            "status filter) or raise the limit.",
            code="key_image_export_max",  # L4: structured code the --remote arm re-classes on
        )
    if total_bytes > max_bytes:
        raise KeyImageError(
            f"this export's images total {total_bytes} bytes, over the per-export limit of "
            f"{max_bytes} (key_image_export_max_bytes); narrow the export or raise the limit.",
            code="key_image_export_max",  # L4: structured code the --remote arm re-classes on
        )

    safe_key_file = _zip_safe_key_file(key_file_name)
    manifest_images: list[dict] = []
    image_ids: list[int] = []
    written_bytes = 0
    # Deliberately NOT context-managed: the spool is the return value; the caller streams it
    # (iter_bundle) and closes it. A build-time failure closes it in the except below.
    spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115
        max_size=EXPORT_SPOOL_MAX_IN_MEMORY, suffix=".zip"
    )
    try:
        # DEFLATED keeps the key file (json/po) + manifest small; the PNG blobs are already
        # compressed and pass through near-verbatim. Written to the SPOOL, never a BytesIO (M2).
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
            _zip_write(zf, safe_key_file, key_file_bytes)
            for key, img in planned:
                data = read_bytes(img)  # ONE blob in hand at a time (M2)
                if data is None:
                    # A ready row whose blob vanished between commit and now (LAW-IMG-16): skip
                    # it as the send path swallows an absent blob -- never fail the export.
                    continue
                arc = EXPORT_IMAGE_PREFIX + img.blob_key
                _zip_write(zf, arc, data)
                written_bytes += len(data)
                image_ids.append(img.id)
                manifest_images.append(
                    {
                        "key": _bundle_key_name(key, qualify=qualify),
                        "namespace": key.namespace or "",
                        "name": key.name,
                        "path": arc,
                        "mime": img.mime,
                        "fmt": img.fmt,
                        "width": img.width,
                        "height": img.height,
                        "ordinal": img.ordinal,
                        "is_primary": img.is_primary,
                        "caption": _scrub_caption(img.caption or ""),
                    }
                )
            manifest = {
                "schema": EXPORT_BUNDLE_SCHEMA,
                "project_id": proj.id,
                "locale": locale,
                "key_file": safe_key_file,
                "qualified": bool(qualify),
                "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
                "image_count": len(manifest_images),
                "image_bytes": written_bytes,
                "images": manifest_images,
            }
            _zip_write(
                zf,
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
            )
    except Exception:
        spool.close()
        raise
    # The final compressed archive size is known now (the spool is complete): report it so the
    # web/api routes can set a real Content-Length on the streamed response (L2) -- a bundle up to
    # the 256 MiB cap otherwise downloads with no size and no browser progress bar.
    spool.seek(0, io.SEEK_END)
    bundle_bytes = spool.tell()
    spool.seek(0)
    return spool, {
        "image_count": len(manifest_images),
        "image_bytes": written_bytes,
        "bundle_bytes": bundle_bytes,  # L2: the streamed ZIP's Content-Length
        "key_file": safe_key_file,
        # The .zip download name shares the SANITISED key-file base (H1): header == member base.
        "download_name": safe_key_file.rsplit(".", 1)[0] + ".zip",
        "image_ids": image_ids,
    }


def iter_bundle(fh, chunk: int = 64 * 1024):
    """Yield a bundle file-like in chunks and CLOSE it at the end -- the streaming-response
    body for the web/api ZIP surfaces (M2), so the deflated archive is never re-loaded whole
    into memory to hand to a Response."""
    try:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            yield block
    finally:
        fh.close()


def _zip_write(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    """Write one member with a SANITISED name (H1, defense in depth: every arcname's segments
    run through ``_zip_safe_member`` so no member can carry ``..`` / an absolute path / control
    chars) and a FIXED timestamp so a given (keys, images) input yields a byte-stable archive
    (no wall-clock in the member headers, M3)."""
    info = zipfile.ZipInfo(_zip_safe_member(arcname), date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    zf.writestr(info, data)
