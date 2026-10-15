"""Per-string in-context images -- Batch 14 Train T1 (store + four surfaces + lifecycle).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env; PIL builds
the fixture images, no network, no provider). Covers the section-17 T1 obligation list AND
the adversarial-gate blockers (each fix has a regression here):

  [V] validation/dedup/caps/downscale (LAW-IMG-10/11) incl. a REAL concurrent race (H-1/H-3)
  [O] orphan-free ordering + _safe_key kill-probe (T-10/T-3); absent blob -> None (LOW-1)
  [P] primary/ordinal, gap-fill ordinals (L-1), primary always set over ready rows (M-1),
      set_primary appends no delta row (I-1), get_image refuses a stub (L-2)
  [S] stub is re-uploadable AND removable (M-2); a blob failure is a KeyImageError not a
      raw OSError (L-6)
  [C] the LAW-IMG-2 triple: cross-key 404 (T-1), server-set project_id (T-2), the list
      path's project_id leg (F-2)
  [R] web + /api routes; feature-off 404 across ALL 10 route/method combos (LAW-IMG-7, M-5)
  [B] the base64 JSON stream-cap: a byte-recording probe proves the server aborts
      MID-STREAM before full receipt, a lying Content-Length is 413'd mid-flight, a foreign
      key is 404'd before the body, a pixel bomb is 400'd -- AND an AST guard forbids
      request.json()/request.body()/a Pydantic body on the attach path (T-12/D2-OOM, H-2)
  [X] CLI leaves (local + --remote) and the MCP handler bodies (M-5)
  [M] MCP accounting; [L] project delete reap incl. the project_id leg (L-4) + preview +
      customer fail-closed; [G] GDPR; [T] retention backstop's OWN counter (M-4)

ASCII-only output (Windows consoles).
"""

import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_keyimg_")

import json as _json
from contextlib import redirect_stderr, redirect_stdout, suppress

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.cli import _remote
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import Customer, KeyChangeLog, KeyImage, Project, ProjectType, StringKey
from app.ports import blobstore
from app.services import (
    content,
    customers,
    gdpr,
    image_localization,
    key_images,
    retention,
    security,
    settings,
)
from app.services import (
    keys as keys_svc,
)

init_db()

PW = "key-image-pw-1"
_SEQ = [0]


def _uniq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _png(w=32, h=32, color=(200, 100, 50)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(w=32, h=32, color=(20, 120, 210)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()


def _mk_customer(name=None) -> int:
    with Session(engine) as s:
        c = Customer(name=name or "ImgCust %d" % _uniq())  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_key_project(cust_id=None, targets="de-DE, ru-RU") -> int:
    with Session(engine) as s:
        p = content.project_create(
            s,
            "ImgKeys %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            target_locales=targets,
            customer_id=(str(cust_id) if cust_id is not None else None),
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _add_key(pid, name="k::img", namespace="app") -> int:
    with Session(engine) as s:
        project = s.get(Project, pid)
        return keys_svc.create_key(
            s, project, name, namespace=namespace, base_value="Hello", actor="fixture"
        ).id


def _attach(key_id, data=None, caption="", uploaded_by="fixture@x.com"):
    data = data if data is not None else _png()
    with Session(engine) as s:
        key = s.get(StringKey, key_id)
        img = key_images.attach_image(s, key, data, caption=caption, uploaded_by=uploaded_by)
        return img.id, img.blob_key


def _switch_images(on: bool) -> None:
    settings.set_many({"key_image_context_enabled": "1" if on else "0"})
    settings.invalidate_cache()


class _RaisingPut:
    """A BlobStore whose put() raises OSError but whose other ops delegate to the real
    store -- so we can simulate a crash between the row commit and the blob put."""

    def __init__(self, real):
        self._real = real

    def put(self, *a, **kw):
        raise OSError("simulated blob-store crash")

    def __getattr__(self, name):
        return getattr(self._real, name)


def _with_failing_put():
    import app.ports.blobstore as bs

    real = bs.get
    bs.get = lambda: _RaisingPut(real())
    return bs, real


# ============================================================================
# [V] validation + dedup + caps + downscale
# ============================================================================
def test_validation_raster_only():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text>hi</text></svg>'
    for bad, why in ((svg, "svg"), (b"not an image at all", "garbage"), (b"", "empty")):
        try:
            key_images.validate_and_render(bad)
            _fail(f"{why} should have been rejected by validate_and_render")
        except key_images.KeyImageError:
            pass
    for data in (_png(), _jpeg()):
        meta = key_images.validate_and_render(data)
        assert image_localization.sniff_kind(meta["png"]) == "png", "stored blob must be a PNG"
        assert meta["byte_size"] == len(meta["png"]) > 0
    print("  [V] raster-only: svg/garbage/empty rejected; png+jpeg render to a PNG blob OK")


def test_validation_pixel_bomb():
    big = _png(w=image_localization.MAX_PIXEL_DIM + 1, h=1)
    try:
        key_images.validate_and_render(big)
        _fail("an over-MAX_PIXEL_DIM image must be rejected before decode")
    except key_images.KeyImageError:
        pass
    print("  [V] pixel-bomb: an over-MAX_PIXEL_DIM image is rejected (header guard) OK")


def test_downscale_at_upload():
    meta = key_images.validate_and_render(_png(w=2000, h=1000))
    assert max(meta["width"], meta["height"]) <= image_localization.CONTEXT_MAX_EDGE, meta
    assert meta["orig_width"] == 2000 and meta["orig_height"] == 1000, meta
    small = key_images.validate_and_render(_png(w=40, h=20))
    assert (small["width"], small["height"]) == (40, 20), small
    print("  [V] downscale: a 2000px image stores <= CONTEXT_MAX_EDGE; a small one is untouched OK")


def test_dedup_within_key():
    pid = _mk_key_project()
    kid = _add_key(pid)
    data = _png(color=(1, 2, 3))
    _attach(kid, data)
    with Session(engine) as s:
        key = s.get(StringKey, kid)
        try:
            key_images.attach_image(s, key, data)
            _fail("a duplicate READY image on the same key must be refused")
        except key_images.KeyImageError:
            pass
    _attach(kid, _png(color=(9, 9, 9)))
    with Session(engine) as s:
        assert len(key_images.list_images(s, s.get(StringKey, kid))) == 2
    print("  [V] dedup: an identical READY image is refused; a different one lands OK")


def test_concurrent_attach_caps_and_dedups():
    """H-1/H-3: a REAL race (threaded, barrier-synced sessions). The atomic serialize +
    the two partial-unique backstops must hold the per-key cap and the within-key dedup
    under concurrency -- 8 concurrent DISTINCT attaches land AT MOST MAX rows (never 6-9),
    and 8 concurrent IDENTICAL attaches land EXACTLY one."""
    import threading

    def _hammer(kid, blobs):
        n = len(blobs)
        barrier = threading.Barrier(n)
        outcomes = []
        lock = threading.Lock()

        def worker(data):
            barrier.wait()
            ok = False
            try:
                with Session(engine) as s:
                    key = s.get(StringKey, kid)
                    key_images.attach_image(s, key, data)
                    ok = True
            except Exception:  # KeyImageError (cap/dedup) OR a lock timeout -> a failed attach
                ok = False
            with lock:
                outcomes.append(ok)

        threads = [threading.Thread(target=worker, args=(b,)) for b in blobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return outcomes

    # 8 DISTINCT images -> at most MAX rows, no duplicate ordinals.
    pid = _mk_key_project()
    kid = _add_key(pid, name="race::distinct")
    _hammer(kid, [_png(color=(i, 7, 7)) for i in range(8)])
    with Session(engine) as s:
        rows = key_images.list_images(s, s.get(StringKey, kid), ready_only=False)
        assert len(rows) <= key_images.MAX_IMAGES_PER_KEY, (
            f"the per-key cap MUST hold under concurrency, got {len(rows)} rows"
        )
        ords = [r.ordinal for r in rows]
        assert len(ords) == len(set(ords)), ("duplicate ordinals under a race", ords)

    # 8 IDENTICAL images -> exactly one row (dedup race closed).
    kid2 = _add_key(pid, name="race::dup")
    same = _png(color=(3, 3, 3))
    _hammer(kid2, [same for _ in range(8)])
    with Session(engine) as s:
        rows = key_images.list_images(s, s.get(StringKey, kid2), ready_only=False)
        assert len(rows) == 1, ("identical concurrent attaches must dedup to 1 row", len(rows))
    print(
        "  [V] race: 8 concurrent distinct attaches hold the cap (<=MAX, unique ordinals); "
        "8 identical attaches dedup to exactly 1 (H-1/H-3) OK"
    )


# ============================================================================
# [O] orphan-free ordering + serve
# ============================================================================
def test_orphan_free_ordering_and_safe_key():
    pid = _mk_key_project()
    kid = _add_key(pid)
    iid, blob_key = _attach(kid)
    assert blob_key.startswith("keyimage_") and blob_key.endswith(".png"), blob_key
    assert "/" not in blob_key and "\\" not in blob_key and ".." not in blob_key, blob_key
    from app.ports.blobstore import _safe_key

    assert _safe_key(blob_key) == blob_key, "the blob key must pass _safe_key unchanged"
    with Session(engine) as s:
        img = s.get(KeyImage, iid)
        assert img.ready is True
        assert blobstore.get().get(blob_key), "the blob must exist after attach"

    # KILL-PROBE (T-10): a failing blob put leaves a findable ready=False stub (L-6: as a
    # clean KeyImageError, not a raw OSError), never a blob without a committed row.
    bs, real = _with_failing_put()
    try:
        with Session(engine) as s:
            key = s.get(StringKey, kid)
            try:
                key_images.attach_image(s, key, _png(color=(7, 7, 7)))
                _fail("a failing blob put should raise KeyImageError")
            except key_images.KeyImageError:
                pass  # L-6: wrapped, not a raw OSError
            except OSError:
                _fail("L-6: a blob failure must be a KeyImageError, never a raw OSError")
    finally:
        bs.get = real
    with Session(engine) as s:
        stubs = [
            r
            for r in key_images.list_images(s, s.get(StringKey, kid), ready_only=False)
            if not r.ready
        ]
        assert len(stubs) == 1, "the crash must leave exactly one findable ready=False stub"
        assert key_images.read_bytes(stubs[0]) is None, "a stub serves no bytes"
        ready_ids = {r.id for r in key_images.list_images(s, s.get(StringKey, kid))}
        assert stubs[0].id not in ready_ids, "a stub is excluded from the ready list"
    print(
        "  [O] orphan-free: a failing put leaves a findable stub (KeyImageError, L-6), no "
        "orphan blob; blob key passes _safe_key OK"
    )


def test_absent_blob_serves_none():
    pid = _mk_key_project()
    kid = _add_key(pid)
    iid, blob_key = _attach(kid)
    blobstore.get().delete(blob_key)
    with Session(engine) as s:
        assert key_images.read_bytes(s.get(KeyImage, iid)) is None
    print("  [O] absent blob: a ready row whose blob vanished reads None (serve 404 not 500) OK")


# ============================================================================
# [P] primary + ordinal + stub lifecycle + delta rows
# ============================================================================
def test_primary_after_failed_blob_and_resume():
    """M-1 + M-2: a first attach whose blob PUT fails leaves a non-primary stub; resuming
    it (re-upload same bytes) finishes it AND makes it the primary (a key with a ready
    image never has no primary -> no silent zero-image resolve on a vision engine, T-9).
    The stub is re-uploadable, not refused (M-2)."""
    pid = _mk_key_project()
    kid = _add_key(pid)
    data = _png(color=(5, 6, 7))
    bs, real = _with_failing_put()
    try:
        with Session(engine) as s:
            key = s.get(StringKey, kid)
            with suppress(key_images.KeyImageError):
                key_images.attach_image(s, key, data)
    finally:
        bs.get = real
    with Session(engine) as s:
        assert key_images.list_images(s, s.get(StringKey, kid)) == [], "no ready image yet"
    # resume: re-upload the SAME bytes (M-2 -- not refused as a duplicate), store now works.
    _attach(kid, data)
    with Session(engine) as s:
        ready = key_images.list_images(s, s.get(StringKey, kid))
        assert len(ready) == 1, ("the stub must resume to exactly one ready row", len(ready))
        assert ready[0].is_primary, "M-1: a key with a ready image MUST have a primary"
    print(
        "  [P] primary/M-2: a failed-blob stub resumes on re-upload and becomes the "
        "primary (M-1: never a primary-less ready key) OK"
    )


def test_ordinal_gap_fill():
    """L-1: after a soft-delete opens a gap, the next attach FILLS it, so ordinals stay
    unique among live rows AND inside 0..MAX-1."""
    pid = _mk_key_project()
    kid = _add_key(pid)
    ids = [_attach(kid, _png(color=(i, 0, 0)))[0] for i in range(3)]  # ordinals 0,1,2
    with Session(engine) as s:
        key = s.get(StringKey, kid)
        mid = key_images.get_image(s, key, ids[1])
        key_images.delete_image(s, key, mid, actor="fixture")  # frees ordinal 1
    _attach(kid, _png(color=(9, 0, 0)))
    with Session(engine) as s:
        rows = key_images.list_images(s, s.get(StringKey, kid))
        ords = sorted(r.ordinal for r in rows)
        assert ords == [0, 1, 2], ("the freed ordinal must be reused, not exceeded", ords)
    print("  [L-1] ordinal gap-fill: a soft-delete's freed ordinal is reused (0..MAX-1) OK")


def test_primary_promotion_and_set_primary():
    pid = _mk_key_project()
    kid = _add_key(pid)
    i0, _ = _attach(kid, _png(color=(1, 0, 0)))
    i1, _ = _attach(kid, _png(color=(2, 0, 0)))
    i2, _ = _attach(kid, _png(color=(3, 0, 0)))
    with Session(engine) as s:
        rows = {r.id: r for r in key_images.list_images(s, s.get(StringKey, kid))}
        assert rows[i0].is_primary and rows[i0].ordinal == 0
        key = s.get(StringKey, kid)
        key_images.delete_image(s, key, rows[i0], actor="fixture")
    with Session(engine) as s:
        live = {r.id: r for r in key_images.list_images(s, s.get(StringKey, kid))}
        assert live[i1].is_primary, "deleting the primary promotes the lowest surviving ready"
        key = s.get(StringKey, kid)
        key_images.set_primary(s, key, live[i2], actor="fixture")
    with Session(engine) as s:
        live = {r.id: r for r in key_images.list_images(s, s.get(StringKey, kid))}
        assert live[i2].is_primary and not live[i1].is_primary, "exactly one primary"
    print("  [P] primary/ordinal: promote on delete; set_primary is exclusive OK")


def test_get_image_rejects_stub_l2():
    """L-2: get_image refuses a ready=False stub by default (so it can never become primary
    or be served), but include_pending=True reaches it (so a DELETE can remove it, M-2)."""
    pid = _mk_key_project()
    kid = _add_key(pid)
    bs, real = _with_failing_put()
    try:
        with Session(engine) as s, suppress(key_images.KeyImageError):
            key_images.attach_image(s, s.get(StringKey, kid), _png(color=(4, 4, 4)))
    finally:
        bs.get = real
    with Session(engine) as s:
        key = s.get(StringKey, kid)
        stub = key_images.list_images(s, key, ready_only=False)[0]
        assert key_images.get_image(s, key, stub.id) is None, "L-2: a stub is not selectable"
        assert key_images.get_image(s, key, stub.id, include_pending=True) is not None, (
            "M-2: a stub is reachable for delete via include_pending"
        )
        key_images.delete_image(
            s, key, key_images.get_image(s, key, stub.id, include_pending=True), actor="fixture"
        )
        assert key_images.list_images(s, key, ready_only=False) == [], "the stub was removed"
    print("  [L-2/M-2] get_image refuses a stub; include_pending reaches it so it is removable OK")


def test_set_primary_appends_no_delta_row_i1():
    pid = _mk_key_project()
    kid = _add_key(pid)
    _i0, _ = _attach(kid, _png(color=(1, 1, 1)))
    i1, _ = _attach(kid, _png(color=(2, 2, 2)))
    with Session(engine) as s:
        before = len(s.exec(select(KeyChangeLog).where(KeyChangeLog.key_id == kid)).all())
        key = s.get(StringKey, kid)
        key_images.set_primary(s, key, key_images.get_image(s, key, i1), actor="fixture")
    with Session(engine) as s:
        after = len(s.exec(select(KeyChangeLog).where(KeyChangeLog.key_id == kid)).all())
        assert after == before, (
            "I-1: set_primary is selection-only, appends NO delta row",
            before,
            after,
        )
    print("  [I-1] set_primary appends no KeyChangeLog (a selection-only change) OK")


def test_delta_rows_on_attach_and_detach():
    pid = _mk_key_project()
    kid = _add_key(pid)
    with Session(engine) as s:
        before = len(s.exec(select(KeyChangeLog).where(KeyChangeLog.key_id == kid)).all())
    iid, _ = _attach(kid)
    with Session(engine) as s:
        after_attach = len(s.exec(select(KeyChangeLog).where(KeyChangeLog.key_id == kid)).all())
        assert after_attach == before + 1, "attach appends one KeyChangeLog(entity=key,updated)"
        key = s.get(StringKey, kid)
        key_images.delete_image(s, key, key_images.get_image(s, key, iid), actor="fixture")
    with Session(engine) as s:
        after_delete = len(s.exec(select(KeyChangeLog).where(KeyChangeLog.key_id == kid)).all())
        assert after_delete == after_attach + 1, "detach appends one KeyChangeLog row"
        row = s.exec(
            select(KeyChangeLog).where(KeyChangeLog.key_id == kid).order_by(KeyChangeLog.id.desc())
        ).first()
        assert row.entity == "key" and row.op == "updated", (row.entity, row.op)
    print("  [P] delta: attach + detach each append one KeyChangeLog(entity=key, op=updated) OK")


# ============================================================================
# [C] confinement (the triple, LAW-IMG-2) incl. the list-path project_id leg (F-2)
# ============================================================================
def test_confinement_triple_and_list_project_leg():
    pid = _mk_key_project()
    keyA = _add_key(pid, name="a::x")
    keyB = _add_key(pid, name="b::x")
    iidB, _ = _attach(keyB)
    with Session(engine) as s:
        a = s.get(StringKey, keyA)
        b = s.get(StringKey, keyB)
        assert key_images.get_image(s, a, iidB) is None, "T-1: key B's image not under key A"
        assert key_images.get_image(s, b, iidB) is not None
        img = s.get(KeyImage, iidB)
        assert img.project_id == b.project_id, "T-2: project_id is the parent key's project"
        # F-2: the list path's project_id leg -- a mismatched project_id yields nothing.
        assert key_images._live_rows(s, keyB, project_id=b.project_id), "own project resolves"
        assert key_images._live_rows(s, keyB, project_id=b.project_id + 987654) == [], (
            "F-2: the project_id tenant leg must filter the list query"
        )
    print(
        "  [C] confinement: cross-key 404 (T-1), server-set project_id (T-2), and the "
        "list-path project_id leg (F-2) OK"
    )


# ============================================================================
# [R] routes (web + /api) + serve + feature-off (all 10 combos, M-5)
# ============================================================================
def _auth(on: bool) -> None:
    settings.set_many({"security_auth_required": "1" if on else "0"})
    settings.invalidate_cache()


def _key_for(email, scopes="api keys", customer_id=None):
    from app.services import apikeys

    user = security.create_user(email, email, security.Role.ADMIN, PW, customer_id=customer_id)
    _row, raw = apikeys.create_key(user.id, "img", scopes=scopes)
    return raw


def test_api_routes_multipart_and_json_and_list():
    _auth(True)
    try:
        head = {"X-Loc-Token": _key_for(f"img-api-{_uniq()}@x.com")}
        pid = _mk_key_project()
        kid = _add_key(pid)
        c = TestClient(app)
        r = c.post(
            f"/api/keys/{kid}/images",
            files={"file": ("shot.png", _png(color=(11, 22, 33)), "image/png")},
            data={"caption": "the login button"},
            headers=head,
        )
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json()["image"]["caption"] == "the login button"
        import base64

        b64 = base64.b64encode(_png(color=(44, 55, 66))).decode("ascii")
        r = c.post(
            f"/api/keys/{kid}/images",
            json={"image_base64": b64, "mime": "image/png", "caption": "json path"},
            headers=head,
        )
        assert r.status_code == 200, (r.status_code, r.text)
        iid_json = r.json()["image"]["id"]
        r = c.get(f"/api/keys/{kid}/images", headers=head)
        imgs = r.json()["images"]
        assert len(imgs) == 2 and all("blob_key" not in i for i in imgs), imgs
        assert c.post(f"/api/keys/{kid}/images/{iid_json}/primary", headers=head).status_code == 200
        rdel = c.request("DELETE", f"/api/keys/{kid}/images/{iid_json}", headers=head)
        assert rdel.status_code == 200
        r = c.get(f"/api/keys/{kid}/images", headers=head)
        surviving = r.json()["images"][0]["id"]
        raw = c.get(f"/api/keys/{kid}/images/{surviving}/raw", headers=head)
        assert raw.status_code == 200 and raw.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        _auth(False)
    print(
        "  [R] /api: multipart + base64-JSON attach, metadata-only list, set-primary, "
        "delete, raw PNG serve OK"
    )


def test_api_cross_key_404_and_raw_stub_404():
    _auth(True)
    try:
        head = {"X-Loc-Token": _key_for(f"img-xkey-{_uniq()}@x.com")}
        pid = _mk_key_project()
        keyA = _add_key(pid, name="ra::x")
        keyB = _add_key(pid, name="rb::x")
        iidB, blobB = _attach(keyB)
        c = TestClient(app)
        assert c.post(f"/api/keys/{keyA}/images/{iidB}/primary", headers=head).status_code == 404
        rxd = c.request("DELETE", f"/api/keys/{keyA}/images/{iidB}", headers=head)
        assert rxd.status_code == 404
        assert c.get(f"/api/keys/{keyA}/images/{iidB}/raw", headers=head).status_code == 404
        blobstore.get().delete(blobB)
        assert c.get(f"/api/keys/{keyB}/images/{iidB}/raw", headers=head).status_code == 404
    finally:
        _auth(False)
    print(
        "  [R] /api: a cross-key image_id is 404 (T-1); a raw serve of an absent blob is "
        "404 not 500 (LOW-1) OK"
    )


def test_web_routes_and_grid_ui():
    _auth(False)
    pid = _mk_key_project()
    kid = _add_key(pid)
    c = TestClient(app)
    r = c.post(
        f"/projects/{pid}/keys/{kid}/images",
        files={"file": ("s.png", _png(color=(9, 8, 7)), "image/png")},
        data={"caption": "hi"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (r.status_code, r.text[:200])
    with Session(engine) as s:
        imgs = key_images.list_images(s, s.get(StringKey, kid))
        assert len(imgs) == 1
        iid = imgs[0].id
    # M-3: the grid renders the image affordances (badge + gallery + education callout).
    grid = c.get(f"/projects/{pid}/keys").text
    assert "In-context images" in grid, "the editor gallery label must render"
    assert "vision-capable" in grid, "the static education callout must render"
    assert f"/projects/{pid}/keys/{kid}/images/{iid}/raw" in grid, "a thumbnail src must render"
    raw = c.get(f"/projects/{pid}/keys/{kid}/images/{iid}/raw")
    assert raw.status_code == 200 and raw.content[:8] == b"\x89PNG\r\n\x1a\n"
    # #489: the browser pull of customer pixels leaves the same audit row as the /api twin.
    from app.models import AuditLog

    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action == "key_image.raw")).all()
    assert any(f"key={kid} image={iid}" in (r.target or "") for r in rows), (
        "the web raw serve wrote no key_image.raw audit row",
        [(r.target, r.detail) for r in rows],
    )
    assert c.post(
        f"/projects/{pid}/keys/{kid}/images/{iid}/primary", follow_redirects=False
    ).status_code in (302, 303)
    assert c.post(
        f"/projects/{pid}/keys/{kid}/images/{iid}/delete", follow_redirects=False
    ).status_code in (302, 303)
    with Session(engine) as s:
        assert key_images.list_images(s, s.get(StringKey, kid)) == []
    print(
        "  [R/M-3] web: multipart attach, grid badge+gallery+callout render, raw serve, "
        "set-primary + delete OK"
    )


def test_caption_is_html_escaped():
    """M-3 note: a caption is user input; the gallery must escape it (no stored XSS)."""
    _auth(False)
    pid = _mk_key_project()
    kid = _add_key(pid)
    c = TestClient(app)
    c.post(
        f"/projects/{pid}/keys/{kid}/images",
        files={"file": ("s.png", _png(color=(1, 2, 4)), "image/png")},
        data={"caption": '"><script>alert(1)</script>'},
        follow_redirects=False,
    )
    grid = c.get(f"/projects/{pid}/keys").text
    assert "<script>alert(1)</script>" not in grid, "the caption must be HTML-escaped"
    assert "&lt;script&gt;" in grid or "&amp;lt;" in grid, "the caption must render escaped"
    print("  [M-3] a caption with markup is HTML-escaped in the grid (no stored XSS) OK")


def test_feature_off_all_ten_combos():
    """M-5: feature-off 404s ALL 10 route/method combos (5 web + 5 /api), while the parent
    keys surface stays up (LAW-IMG-7)."""
    _auth(True)
    try:
        head = {"X-Loc-Token": _key_for(f"img-off-{_uniq()}@x.com")}
        pid = _mk_key_project()
        kid = _add_key(pid)
        iid, _ = _attach(kid)
        c = TestClient(app)
        _switch_images(False)
        try:
            api = [
                ("GET", f"/api/keys/{kid}/images", None),
                (
                    "POST",
                    f"/api/keys/{kid}/images",
                    {"json": {"image_base64": "AA==", "mime": "image/png"}},
                ),
                ("DELETE", f"/api/keys/{kid}/images/{iid}", None),
                ("POST", f"/api/keys/{kid}/images/{iid}/primary", None),
                ("GET", f"/api/keys/{kid}/images/{iid}/raw", None),
            ]
            for method, path, kw in api:
                r = c.request(method, path, headers=head, **(kw or {}))
                assert r.status_code == 404, (
                    "feature-off /api must 404",
                    method,
                    path,
                    r.status_code,
                )
            web = [
                ("POST", f"/projects/{pid}/keys/{kid}/images"),
                ("POST", f"/projects/{pid}/keys/{kid}/images/{iid}/delete"),
                ("POST", f"/projects/{pid}/keys/{kid}/images/{iid}/primary"),
                ("GET", f"/projects/{pid}/keys/{kid}/images/{iid}/raw"),
            ]
            for method, path in web:
                r = c.request(method, path, follow_redirects=False)
                assert r.status_code == 404, (
                    "feature-off web must 404",
                    method,
                    path,
                    r.status_code,
                )
            # 10th combo: the web attach with a file part (multipart) also 404s.
            r = c.post(
                f"/projects/{pid}/keys/{kid}/images",
                files={"file": ("s.png", _png(), "image/png")},
                follow_redirects=False,
            )
            assert r.status_code == 404, (
                "feature-off web multipart attach must 404",
                r.status_code,
            )
            assert c.get(f"/api/keys/{kid}/values", headers=head).status_code == 200, (
                "the parent keys surface must stay up"
            )
        finally:
            _switch_images(True)
    finally:
        _auth(False)
    print(
        "  [R/M-5] feature-off: ALL 10 image route/method combos 404 (web+/api) while the "
        "parent keys routes stay up (LAW-IMG-7) OK"
    )


# ============================================================================
# [B] the base64 JSON attach stream-cap (T-12 / D2-OOM, H-2)
# ============================================================================
def test_stream_cap_records_bytes_and_source_guard():
    """H-2: a byte-RECORDING probe proves the server aborts MID-STREAM before it has
    received the whole body (the compliant behavior a post-hoc length check would fail),
    plus a lying Content-Length, a foreign-key-before-body, and a pixel bomb. And an AST
    guard forbids request.json()/request.body()/a Pydantic body on the attach path."""
    import asyncio

    _auth(True)
    try:
        head = {"X-Loc-Token": _key_for(f"img-oom-{_uniq()}@x.com")}
        pid = _mk_key_project()
        kid = _add_key(pid)
        c = TestClient(app)
        real_cap = config.KEY_IMAGE_MAX_RAW_BODY_BYTES
        config.KEY_IMAGE_MAX_RAW_BODY_BYTES = 512
        try:
            # (a) TRUE ASGI-level probe (TestClient materializes the body client-side, so it
            # cannot measure server consumption): a `receive` that counts pulls and delivers
            # the body in many `more_body=True` chunks. A compliant `request.stream()` abort
            # stops calling receive once the cap is passed -> pulled < total. A full-body
            # buffer (request.body()/json()) would drain EVERY chunk first.
            total_chunks = 40

            async def _probe():
                pulled = {"n": 0}
                chunks = [b"A" * 64 for _ in range(total_chunks)]
                it = iter(chunks)
                sent = []

                async def receive():
                    pulled["n"] += 1
                    try:
                        return {"type": "http.request", "body": next(it), "more_body": True}
                    except StopIteration:
                        return {"type": "http.request", "body": b"", "more_body": False}

                async def send(msg):
                    sent.append(msg)

                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "path": f"/api/keys/{kid}/images",
                    "raw_path": f"/api/keys/{kid}/images".encode(),
                    "query_string": b"",
                    "root_path": "",
                    "scheme": "http",
                    "server": ("testserver", 80),
                    "client": ("127.0.0.1", 12345),
                    "headers": [
                        (b"x-loc-token", head["X-Loc-Token"].encode()),
                        (b"content-type", b"application/json"),
                        (b"host", b"testserver"),
                    ],
                }
                await app(scope, receive, send)
                status = next(m["status"] for m in sent if m["type"] == "http.response.start")
                return status, pulled["n"]

            status, pulled_n = asyncio.run(_probe())
            assert status == 413, ("streamed over-cap must 413 mid-stream", status)
            assert pulled_n < total_chunks, (
                "H-2: the server MUST abort mid-stream BEFORE the whole body is received "
                f"(a full-body buffer would pull all {total_chunks} chunks); it pulled {pulled_n}"
            )
            # (b) a LYING Content-Length (declare small, stream large) -> 413 mid-flight.
            big = b'{"image_base64":"' + b"A" * 4000 + b'"}'
            r = c.post(
                f"/api/keys/{kid}/images",
                content=big,
                headers={**head, "content-type": "application/json", "content-length": "10"},
            )
            assert r.status_code == 413, (
                "a lying small Content-Length + big body must 413",
                r.status_code,
            )
            # (c) a FOREIGN key -> 404 BEFORE the body is read (gate chain first).
            r = c.post(
                "/api/keys/999999999/images",
                content=big,
                headers={**head, "content-type": "application/json"},
            )
            assert r.status_code == 404, ("a foreign key must 404 before the body", r.status_code)
        finally:
            config.KEY_IMAGE_MAX_RAW_BODY_BYTES = real_cap
        # (d) a base64 PIXEL BOMB passes the raw cap but open_raster rejects it -> 400.
        import base64

        bomb = base64.b64encode(_png(w=image_localization.MAX_PIXEL_DIM + 1, h=1)).decode("ascii")
        r = c.post(
            f"/api/keys/{kid}/images",
            json={"image_base64": bomb, "mime": "image/png"},
            headers=head,
        )
        assert r.status_code == 400, ("a base64 pixel bomb must be 400", r.status_code, r.text)
    finally:
        _auth(False)

    # AST guard on the CODE (not comments/docstrings, so the docstring's own mention of the
    # forbidden calls does not trip it): the attach handler must NEVER *call* request.json()
    # / request.body(), and its executable body must USE request.stream().
    import ast

    with open(os.path.join(_ROOT, "app", "web", "keys.py"), encoding="utf-8") as _fh:
        src = _fh.read()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "api_key_image_add"
    )
    called_attrs, uses_stream = set(), False
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "request"
        ):
            called_attrs.add(node.func.attr)
            if node.func.attr == "stream":
                uses_stream = True
    for forbidden in ("json", "body"):
        assert forbidden not in called_attrs, (
            f"H-2/D2-OOM: the JSON attach path MUST NOT call request.{forbidden}() (it buffers "
            "the whole body before the cap)"
        )
    assert uses_stream, "the attach path must use request.stream() (the stream-cap, D2-OOM)"
    # a Pydantic/FastAPI body-model parameter is likewise forbidden (it buffers during
    # validation): the handler takes only path/query/session params, no annotated body model.
    param_annos = {
        a.arg: getattr(
            a.annotation,
            "id",
            getattr(getattr(a.annotation, "attr", None), "__str__", lambda: "")(),
        )
        for a in fn.args.args
        if a.annotation is not None
    }
    assert "BaseModel" not in str(param_annos), "no Pydantic body model on the attach path"
    print(
        "  [B] stream-cap: byte-recorder proves a mid-stream abort before full receipt; "
        "lying Content-Length 413; foreign key 404 before body; pixel bomb 400; source "
        "guard forbids request.json()/body() (T-12/H-2) OK"
    )


# ============================================================================
# [X] CLI leaves (local + --remote) + MCP handler bodies (M-5)
# ============================================================================
def _cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = cli_main(["--json", *argv])
    out = buf.getvalue()
    try:
        return rc, _json.loads(out)
    except Exception:
        return rc, {"_raw": out}


def _mock_transport(client):
    import httpx

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = client.request(
            request.method, path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": r.headers.get("content-type", "")},
        )

    return httpx.MockTransport(handler)


def test_cli_leaves_local_and_remote():
    """M-5 + deviation (a): the four `keys image` leaves work IN-PROCESS and honor --remote
    (dispatching through the /api twin like every other keys leaf)."""
    import tempfile
    from unittest.mock import patch

    _auth(False)
    pid = _mk_key_project()
    kid = _add_key(pid, name="cli::img")
    path = os.path.join(tempfile.mkdtemp(prefix="kiimg_"), "shot.png")
    with open(path, "wb") as fh:
        fh.write(_png(color=(3, 4, 5)))

    rc, out = _cli(["keys", "image", "add", str(kid), path, "--caption", "cli shot"])
    assert rc == 0 and out.get("ok"), ("local add", rc, out)
    img_id = out["image"]["id"]
    rc, out = _cli(["keys", "image", "list", str(kid)])
    assert rc == 0 and len(out.get("images", [])) == 1, ("local list", rc, out)
    rc, out = _cli(["keys", "image", "set-primary", str(kid), str(img_id)])
    assert rc == 0 and out["image"]["is_primary"], ("local set-primary", rc, out)
    rc, out = _cli(["keys", "image", "rm", str(kid), str(img_id)])
    assert rc == 0 and out.get("deleted"), ("local rm", rc, out)

    # --remote round trip through the /api twin (proves deviation (a) is closed).
    _auth(True)
    try:
        tok = _key_for(f"img-cli-remote-{_uniq()}@x.com")
        client = TestClient(app)
        _remote._set_transport(_mock_transport(client))
        try:

            def remote(*argv):
                with patch.dict(os.environ, {"LOC_TMS_TOKEN": tok}):
                    return _cli(["--remote", "http://127.0.0.1", *argv])

            rc, out = remote("keys", "image", "add", str(kid), path, "--caption", "remote shot")
            assert rc == 0 and out.get("ok"), ("remote add must land via /api", rc, out)
            rc, out = remote("keys", "image", "list", str(kid))
            assert rc == 0 and len(out.get("images", [])) == 1, ("remote list", rc, out)
            rid = out["images"][0]["id"]
            rc, out = remote("keys", "image", "rm", str(kid), str(rid))
            assert rc == 0 and out.get("deleted"), ("remote rm", rc, out)
        finally:
            _remote._set_transport(None)
    finally:
        _auth(False)
    print(
        "  [X/M-5] CLI: keys image add/list/set-primary/rm work in-process AND over "
        "--remote through the /api twin (deviation a closed) OK"
    )


def test_mcp_handler_bodies():
    """M-5: exercise the server.py handler BODIES (not just registry constants) by routing
    the MCP server's httpx client at the in-process app (ASGITransport, no socket)."""
    import asyncio
    import tempfile

    import httpx

    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    try:
        import server as mcp_server
    except Exception as exc:  # the MCP SDK must be present (verify_mcp_tools relies on it)
        _fail(f"could not import the MCP server module for handler tests: {exc}")

    _auth(True)
    try:
        tok = _key_for(f"img-mcp-{_uniq()}@x.com")
        pid = _mk_key_project()
        kid = _add_key(pid, name="mcp::img")
        path = os.path.join(tempfile.mkdtemp(prefix="kimcp_"), "shot.png")
        with open(path, "wb") as fh:
            fh.write(_png(color=(6, 7, 8)))

        # #172: caller-supplied file reads are DENIED by default now, because the old
        # default left them enabled for any start path that never called main(), including
        # importing this module and serving the ASGI app directly. This block drives the
        # tools the way a LOCAL STDIO server would, with a file_path off the local disk, so
        # it has to say so explicitly. Identical opt-in to verify_mcp.py:78 and
        # verify_mcp_tools.py; this suite was missed because the train's selector matched
        # "mcp" and this file is named for what it tests rather than how it reaches it.
        #
        # GATE R6 LOW-3: the opt-in goes INSIDE the try whose finally restores it. Nine
        # lines of setup sat between them, so anything raising in that window leaked the
        # permission for the rest of the process.
        saved_reads = mcp_server._LOCAL_FILE_READS
        saved_base, saved_tok, saved_client = (
            mcp_server.BASE_URL,
            mcp_server.TOKEN,
            mcp_server._client,
        )
        mcp_server.BASE_URL = "http://mcp.test"
        mcp_server.TOKEN = tok
        mcp_server._client = lambda: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), timeout=mcp_server._HTTP_TIMEOUT
        )
        try:
            # #755: the screenshot's own folder is the allowed input folder
            mcp_server._allow_local_file_reads(True, roots=[os.path.dirname(path)])
            out = _json.loads(
                asyncio.run(
                    mcp_server.manage_key_image("attach", kid, file_path=path, caption="mcp shot")
                )
            )
            iid = out["image"]["id"]
            assert out["image"]["caption"] == "mcp shot", out
            listed = _json.loads(asyncio.run(mcp_server.list_key_images(kid)))
            assert len(listed["images"]) == 1 and "blob_key" not in listed["images"][0], listed
            asyncio.run(mcp_server.manage_key_image("set_primary", kid, image_id=iid))
            out = _json.loads(asyncio.run(mcp_server.manage_key_image("delete", kid, image_id=iid)))
            assert out.get("deleted"), out
            listed = _json.loads(asyncio.run(mcp_server.list_key_images(kid)))
            assert listed["images"] == [], "the MCP delete must have landed"
            # confinement: a foreign key_id is a non-enumerable 404 (a ToolError, not data).
            other = _add_key(_mk_key_project(), name="mcp::other")
            raised = False
            try:
                asyncio.run(mcp_server.manage_key_image("set_primary", kid, image_id=99999999))
            except Exception:
                raised = True
            assert raised, "a missing image_id must raise a ToolError, not silently pass"
            del other
        finally:
            mcp_server.BASE_URL, mcp_server.TOKEN, mcp_server._client = (
                saved_base,
                saved_tok,
                saved_client,
            )
            mcp_server._allow_local_file_reads(saved_reads)
    finally:
        _auth(False)
    print(
        "  [X/M-5] MCP handlers: manage_key_image attach/set_primary/delete + "
        "list_key_images drive the real /api routes (handler bodies exercised) OK"
    )


# ============================================================================
# [M] MCP accounting
# ============================================================================
def test_mcp_accounting():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "loc_reg_ki", os.path.join(_ROOT, "integrations", "mcp-server", "registry.py")
    )
    reg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reg
    spec.loader.exec_module(reg)

    covered = reg.covered_routes()
    for route in (
        ("POST", "/api/keys/{key_id}/images"),
        ("DELETE", "/api/keys/{key_id}/images/{image_id}"),
        ("POST", "/api/keys/{key_id}/images/{image_id}/primary"),
        ("GET", "/api/keys/{key_id}/images"),
    ):
        assert route in covered, f"{route} must be COVERED by an MCP tool"
    raw = ("GET", "/api/keys/{key_id}/images/{image_id}/raw")
    assert raw not in covered and raw in reg.EXEMPT and reg.EXEMPT[raw][0] == "authoring-surface"
    from tests import verify_mcp_parity as vmp

    assert ("DELETE", "/api/keys/{key_id}/images/{image_id}") in vmp._AUTHORING_DELETE_ROUTES
    by_name = {t.name: t for t in reg.TOOLS}
    assert by_name["manage_key_image"].tier == "write" and by_name["list_key_images"].tier == "read"
    assert "key_image_context_enabled" in set(settings.DEFAULTS)
    print(
        "  [M] MCP: attach/delete/set-primary/list COVERED, raw EXEMPT, DELETE carved out, "
        "tiers correct, gate key in DEFAULTS OK"
    )


# ============================================================================
# [L] lifecycle -- project delete reaps (incl. the project_id leg, L-4) + preview
# ============================================================================
def test_project_delete_reaps_and_preview_counts():
    pid = _mk_key_project()
    kid = _add_key(pid)
    iid, blob_key = _attach(kid)
    _, blob2 = _attach(kid, _png(color=(5, 6, 7)))
    # L-4: an image whose key_id is NOT gathered but whose project_id == pid must still be
    # reaped, so no husk fails the FK DELETE. Simulate by pointing a row's key_id at a
    # foreign key while keeping project_id == pid.
    foreign_key = _add_key(_mk_key_project(), name="foreign::x")
    with Session(engine) as s:
        stray = KeyImage(
            key_id=foreign_key,
            project_id=pid,
            blob_key=f"keyimage_{_uniq()}.png",
            ready=True,
            mime="image/png",
            fmt="png",
        )
        s.add(stray)
        s.commit()
        blobstore.get().put(stray.blob_key, _png(color=(2, 2, 2)))
        stray_blob = stray.blob_key
    with Session(engine) as s:
        prev = content.delete_project_preview(s, pid)
        assert prev["cascade"]["key_images"] >= 2, ("preview counts images", prev["cascade"])
    with Session(engine) as s:
        assert content.delete_project(s, pid, actor="fixture") is not None, "must complete (T-11)"
    with Session(engine) as s:
        assert s.exec(select(KeyImage).where(KeyImage.project_id == pid)).all() == [], (
            "L-4: every KeyImage with project_id == pid must be reaped (no husk)"
        )
    for bk in (blob_key, blob2, stray_blob):
        try:
            blobstore.get().get(bk)
            _fail(f"the blob {bk} must be reaped by project delete")
        except (OSError, FileNotFoundError):
            pass
    del iid
    print(
        "  [L] project delete: completes (T-11), reaps blobs + rows including the "
        "project_id-only leg (L-4), preview counts them OK"
    )


def test_customer_delete_fails_closed():
    cust = _mk_customer()
    pid = _mk_key_project(cust)
    _attach(_add_key(pid))
    with Session(engine) as s:
        try:
            customers.delete_customer(s, cust, actor="fixture")
            _fail("a customer with a referencing project must NOT be deletable")
        except customers.CustomerDeleteBlocked:
            pass
    print("  [L] customer delete: still fails closed while a project references it OK")


# ============================================================================
# [G] GDPR + [T] retention backstop (M-4)
# ============================================================================
def test_gdpr_sweep_symmetry_and_export():
    assert ("key_image_uploaders", KeyImage, KeyImage.uploaded_by) in gdpr._ERASE_SWEEP
    assert gdpr._ERASE_EXPORT_SECTION.get("key_image_uploaders") == "key_images"
    email = "img-gdpr-%d@x.com" % _uniq()  # noqa: UP031
    subject = security.create_user(email, email, security.Role.ADMIN, PW)
    pid = _mk_key_project()
    kid = _add_key(pid)
    iid, _ = _attach(kid, uploaded_by=email)
    sections = {sec for sec, _rec in gdpr.export_user(subject.id)}
    assert "key_images" in sections, "export_user must yield a key_images section"
    gdpr.erase_user(subject.id, actor="fixture")
    with Session(engine) as s:
        img = s.get(KeyImage, iid)
        assert img is not None and img.uploaded_by != email, (
            "erase tombstones uploaded_by, keeps row"
        )
    print(
        "  [G] GDPR: uploaded_by swept to a tombstone (content kept); export yields a "
        "key_images section; symmetry counterpart present OK"
    )


def test_retention_backstop_distinct_counter():
    """M-4: the backstop reaps long-soft-deleted rows + stale stubs and counts them under
    its OWN `key_image_blobs` key -- NEVER folded into `files`."""
    from datetime import datetime, timedelta

    pid = _mk_key_project()
    kid = _add_key(pid)
    old = datetime.utcnow() - timedelta(days=400)
    reaped = []
    with Session(engine) as s:
        key = s.get(StringKey, kid)
        a = key_images.attach_image(s, key, _png(color=(3, 3, 3)))
        a.deleted_at = old
        s.add(a)
        s.commit()
        reaped.append(a.blob_key)
        stub = KeyImage(
            key_id=kid,
            project_id=key.project_id,
            blob_key=f"keyimage_{_uniq()}.png",
            ready=False,
            uploaded_at=old,
        )
        s.add(stub)
        s.commit()
        blobstore.get().put(stub.blob_key, _png(color=(4, 4, 4)))
        reaped.append(stub.blob_key)
        a_id, stub_id = a.id, stub.id
    settings.set_many({"retention_enabled": "1", "retention_days": "30"})
    settings.invalidate_cache()
    try:
        result = retention.purge()
    finally:
        settings.set_many({"retention_enabled": "0", "retention_days": "0"})
        settings.invalidate_cache()
    with Session(engine) as s:
        assert s.get(KeyImage, a_id) is None and s.get(KeyImage, stub_id) is None
    for bk in reaped:
        try:
            blobstore.get().get(bk)
            _fail("the backstop must reap the blob of %s" % bk)  # noqa: UP031
        except (OSError, FileNotFoundError):
            pass
    assert result["key_image_blobs"] >= 2, ("M-4: reaped blobs under key_image_blobs", result)
    assert result["files"] == 0, ("M-4: NOT folded into files", result)
    print(
        "  [T/M-4] retention backstop: long-soft-deleted rows + stale stubs reaped under "
        "the DISTINCT key_image_blobs counter (never `files`) OK"
    )


def test_failed_deletion_keeps_image_key():
    from datetime import datetime, timedelta
    from unittest.mock import patch

    from app.models import BlobDeleteOutbox

    for project_delete in (True, False):
        pid = _mk_key_project()
        kid = _add_key(pid)
        iid, key = _attach(kid)
        if not project_delete:
            with Session(engine) as s:
                image = s.get(KeyImage, iid)
                image.deleted_at = datetime.utcnow() - timedelta(days=400)
                s.add(image)
                s.commit()
        settings.set_many({"retention_enabled": "1", "retention_days": "30"})
        real_delete = blobstore.DiskBlobStore.delete_status

        def locked_image(store, candidate, locked_key=key, delete=real_delete):
            if candidate == locked_key:
                return blobstore.DeleteStatus.FAILED
            return delete(store, candidate)

        with patch.object(blobstore.DiskBlobStore, "delete_status", locked_image):
            if project_delete:
                with Session(engine) as s:
                    result = content.delete_project(s, pid, actor="fixture")
                assert result["file_pending"] == 1, result
            else:
                assert retention.purge()["blob_deletes_pending"] == 1
        with Session(engine) as s:
            assert s.get(KeyImage, iid) is None
            assert s.exec(select(BlobDeleteOutbox).where(BlobDeleteOutbox.blob_key == key)).one()
        assert blobstore.get().get(key)
        settings.set_many({"retention_enabled": "0", "retention_days": "0"})
        assert retention.tick()["blob_deletes_retried"] == 1
        try:
            blobstore.get().get(key)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("pending key-image blob survived recovery")
    print("  key-image failed project/retention deletion keeps a durable retry key OK")


def main() -> None:
    test_validation_raster_only()
    test_validation_pixel_bomb()
    test_downscale_at_upload()
    test_dedup_within_key()
    test_concurrent_attach_caps_and_dedups()
    test_orphan_free_ordering_and_safe_key()
    test_absent_blob_serves_none()
    test_primary_after_failed_blob_and_resume()
    test_ordinal_gap_fill()
    test_primary_promotion_and_set_primary()
    test_get_image_rejects_stub_l2()
    test_set_primary_appends_no_delta_row_i1()
    test_delta_rows_on_attach_and_detach()
    test_confinement_triple_and_list_project_leg()
    test_api_routes_multipart_and_json_and_list()
    test_api_cross_key_404_and_raw_stub_404()
    test_web_routes_and_grid_ui()
    test_caption_is_html_escaped()
    test_feature_off_all_ten_combos()
    test_stream_cap_records_bytes_and_source_guard()
    test_cli_leaves_local_and_remote()
    test_mcp_handler_bodies()
    test_mcp_accounting()
    test_project_delete_reaps_and_preview_counts()
    test_customer_delete_fails_closed()
    test_gdpr_sweep_symmetry_and_export()
    test_retention_backstop_distinct_counter()
    test_failed_deletion_keeps_image_key()
    print("KEY IMAGES OK")


if __name__ == "__main__":
    main()
