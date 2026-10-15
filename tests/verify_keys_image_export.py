"""include_string_images key-export option -- Batch 14 Phase 1.5.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env; PIL builds
the fixture images, no network, no provider). Covers the section-10 obligation, the task's
counterfactuals AND the round-1 gate findings (H1/H2/M1-M4/L1-L10):

  [F] OFF is byte-identical to today's plain export (the exported_keys out-param is inert).
  [Z] ON returns a ZIP bundle: the key file + images/<blob_key> members + manifest.json,
      whose entries LINK each key to its image path (LAW-IMG-8 lifecycle).
  [C] two-org confinement, LOAD-BEARING (M1): a project_id/key_id DRIFT image never rides
      out; the fixture is built so each of images_for_keys' two legs is individually needed.
  [H1] every ZIP member is a traversal-proof sanitised basename (hostile namespace on the
      service + /api + web + CLI).
  [H2] the option is ABSENT from the MCP export_keys tool (no param, no server signature).
  [M2] the bundle is a streamed spooled file; blobs are read one at a time.
  [M3] a given (keys, images, generated_at) yields byte-identical archives.
  [L2] the manifest caption is scrubbed (secrets + the pii seam + credential assignments).
  [L3] soft-deleted / pending images are excluded.
  [L6] a duplicate blob_key across live rows is de-duped (no duplicate ZIP member).
  [L10] the CLI --json carries the bundle's img_info.
  [K] the caps refuse WHOLE (no partial artifact).
  [O] feature OFF -> include_string_images is ignored (plain file, not a ZIP).
  [S] the surfaces: web GET, /api GET, the CLI leaf (--include-string-images, requires -o).

ASCII-only output (Windows consoles).
"""

import io
import os
import sys
import zipfile
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_kiexport_")

import json as _json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import Customer, KeyImage, Project, ProjectType, StringKey, _utcnow
from app.services import content, key_files, key_images, security, settings
from app.services import keys as keys_svc

init_db()

PW = "ki-export-pw-1"
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


def _mk_customer(name=None) -> int:
    with Session(engine) as s:
        c = Customer(name=name or "KIExp %d" % _uniq())  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _mk_key_project(cust_id=None, targets="de-DE") -> int:
    with Session(engine) as s:
        p = content.project_create(
            s,
            "KIExpKeys %d" % _uniq(),  # noqa: UP031
            source_locale="en-US",
            target_locales=targets,
            customer_id=(str(cust_id) if cust_id is not None else None),
            project_type=ProjectType.KEY_BASED,
        )
        return p.id


def _add_key(pid, name, namespace="app", base="Hello") -> int:
    with Session(engine) as s:
        project = s.get(Project, pid)
        return keys_svc.create_key(
            s, project, name, namespace=namespace, base_value=base, actor="fixture"
        ).id


def _attach(key_id, data=None, caption="") -> str:
    data = data if data is not None else _png()
    with Session(engine) as s:
        key = s.get(StringKey, key_id)
        img = key_images.attach_image(s, key, data, caption=caption, uploaded_by="f@x.com")
        return img.blob_key


def _attach_then_drift(key_id, drift_project_id, color) -> str:
    """Attach a real (validated) image to `key_id`, then DRIFT its tenant anchor to
    `drift_project_id`. The exact `get_image`/`images_for_keys` triple exists to catch this
    (LAW-IMG-2), so it is the right fixture to make the confinement legs load-bearing (M1)."""
    blob = _attach(key_id, _png(color=color))
    with Session(engine) as s:
        row = s.exec(select(KeyImage).where(KeyImage.blob_key == blob)).one()
        row.project_id = drift_project_id
        s.add(row)
        s.commit()
    return blob


def _switch_images(on: bool) -> None:
    settings.set_many({"key_image_context_enabled": "1" if on else "0"})
    settings.invalidate_cache()


def _auth(on: bool) -> None:
    settings.set_many({"security_auth_required": "1" if on else "0"})
    settings.invalidate_cache()


def _key_for(email, scopes="api keys", customer_id=None):
    from app.services import apikeys

    user = security.create_user(email, email, security.Role.ADMIN, PW, customer_id=customer_id)
    _row, raw = apikeys.create_key(user.id, "kiexp", scopes=scopes)
    return raw


def _web_login(email):
    from tests._csrf import csrf_for

    security.create_user(email, email, security.Role.ADMIN, PW)
    c = TestClient(app)
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": email, "password": PW, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, (email, r.status_code)
    csrf_for(c)
    return c


def _cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = cli_main(["--json", *argv])
    out = buf.getvalue()
    try:
        return rc, _json.loads(out)
    except Exception:
        return rc, {"_raw": out}


def _build(pid, locale="", namespace=None, when=None):
    """Build a bundle through the service and return (zip_bytes, info, key_file_name). Reads the
    streamed spooled file to bytes via iter_bundle (which also closes it)."""
    when = when or datetime.now(timezone.utc)
    with Session(engine) as s:
        proj = s.get(Project, pid)
        ek: list = []
        data = key_files.export_locale(s, proj, locale, namespace=namespace, exported_keys=ek)
        fname = key_files.download_filename(namespace or "project", locale or "en-US", "json")
        bundle, info = key_images.build_export_bundle(
            s,
            proj,
            locale or "en-US",
            key_file_bytes=data,
            key_file_name=fname,
            exported_keys=ek,
            qualify=(namespace is None),
            generated_at=when,
        )
    zbytes = b"".join(key_images.iter_bundle(bundle))
    return zbytes, info, fname


# ============================================================================
# [F] OFF is byte-identical to today (the out-param is inert)
# ============================================================================
def test_off_is_byte_identical():
    _switch_images(True)
    pid = _mk_key_project()
    _add_key(pid, "btn.save")
    _add_key(pid, "btn.cancel")
    with Session(engine) as s:
        proj = s.get(Project, pid)
        plain_none = key_files.export_locale(s, proj, "", exported_keys=None)
        plain_empty = key_files.export_locale(s, proj, "", exported_keys=[])
    assert plain_none == plain_empty, "the exported_keys out-param must not change the file bytes"
    kid = _add_key(pid, "btn.help")
    _attach(kid)
    with Session(engine) as s:
        proj = s.get(Project, pid)
        after = key_files.export_locale(s, proj, "", exported_keys=None)
    assert b"keyimage_" not in after and b"PNG" not in after, "no image bytes may enter the file"
    print("  [F] OFF: exported_keys is inert; plain export never carries image bytes OK")


# ============================================================================
# [Z] ON -> a ZIP bundle: key file + images/ + manifest linking key -> path
# ============================================================================
def test_on_bundle_shape_and_manifest_link():
    _switch_images(True)
    pid = _mk_key_project()
    k1 = _add_key(pid, "btn.save")
    _add_key(pid, "btn.plain")  # a key with NO image
    b1 = _attach(k1, _png(color=(10, 20, 30)), caption="the save button")
    zbytes, info, fname = _build(pid)
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    names = set(zf.namelist())
    assert fname in names, ("key file must be a member", names)
    assert "manifest.json" in names, ("manifest must be a member", names)
    assert f"images/{b1}" in names, ("the image blob must be under images/", names)
    assert info["image_count"] == 1, info
    assert info["key_file"] == fname, ("info.key_file is the sanitised member name", info)
    assert info["download_name"] == "project.en-US.zip", info
    manifest = _json.loads(zf.read("manifest.json"))
    assert manifest["schema"] == key_images.EXPORT_BUNDLE_SCHEMA, manifest
    assert manifest["key_file"] == fname, manifest
    assert manifest["image_count"] == 1 and len(manifest["images"]) == 1, manifest
    entry = manifest["images"][0]
    assert entry["path"] == f"images/{b1}", entry
    assert entry["name"] == "btn.save" and entry["namespace"] == "app", entry
    assert entry["key"] == key_files.qualified_name("app", "btn.save"), entry
    assert zf.read(f"images/{b1}")[:8] == b"\x89PNG\r\n\x1a\n", "bundled blob must be a PNG"
    assert all(e["name"] != "btn.plain" for e in manifest["images"]), manifest
    print("  [Z/N] ON: ZIP has key file + images/<blob_key> + manifest link; imageless key OK")


# ============================================================================
# [C] two-org confinement -- LOAD-BEARING (M1)
# ============================================================================
def test_two_org_confinement():
    _switch_images(True)
    cust_a = _mk_customer("A")
    cust_b = _mk_customer("B")
    pid_a = _mk_key_project(cust_a)
    pid_b = _mk_key_project(cust_b)
    ka = _add_key(pid_a, "shared.name")
    kb = _add_key(pid_b, "shared.name")
    own = _attach(ka, _png(color=(1, 1, 1)))
    foreign = _attach(kb, _png(color=(2, 2, 2)))
    assert own != foreign
    # DRIFT 1 (tenant leg): an image whose key is A's but whose project_id is B's. It is in
    # A's key_ids, so through `build_export_bundle` ONLY the `KeyImage.project_id == project_id`
    # leg of `images_for_keys` excludes it -- drop that leg and it rides out, turning this test
    # red. (M1: round-1's vacuous version could not observe even this.)
    drift_tenant = _attach_then_drift(ka, pid_b, color=(3, 3, 3))
    # DRIFT 2 (key leg, DEFENCE IN DEPTH through the bundle): an image whose project_id is A's
    # but whose key is B's. Through `build_export_bundle` this is excluded by the bundle loop
    # itself (`for key in exported_keys: by_key.get(key.id, [])` never looks up a FOREIGN key
    # id), so dropping the query's `KeyImage.key_id.in_(key_ids)` leg alone does NOT turn THIS
    # assertion red -- the exported_keys loop is the primary exclusion here. The query's key leg
    # is where a SAME-project non-exported key is confined, and that is proven load-bearing
    # directly against `images_for_keys` in test_key_leg_is_loadbearing_at_the_service (M1).
    drift_key = _attach_then_drift(kb, pid_a, color=(4, 4, 4))
    zbytes, info, _ = _build(pid_a)
    names = set(zipfile.ZipFile(io.BytesIO(zbytes)).namelist())
    assert f"images/{own}" in names, ("own image present", names)
    assert f"images/{foreign}" not in names, ("FOREIGN image must never be bundled", names)
    assert f"images/{drift_tenant}" not in names, ("tenant-leg drift must be excluded", names)
    assert f"images/{drift_key}" not in names, ("key-leg drift must be excluded", names)
    assert info["image_count"] == 1, info
    print("  [C/M1] confinement: own image only; tenant-leg drift excluded (key-leg d-in-depth)")


def test_key_leg_is_loadbearing_at_the_service():
    """M1 (gate round 2): the `KeyImage.key_id.in_(key_ids)` leg of `images_for_keys` is where a
    SAME-project, NON-EXPORTED key's image is confined -- and it is genuinely load-bearing THERE
    (drop the key leg and the extra image is returned), which the bundle loop cannot observe (it
    re-derives its allowed set from `exported_keys`, so it is a second, independent exclusion).

    Fixture: ONE project A, two keys in DIFFERENT namespaces. A namespace-scoped export of "app"
    includes only k_app; k_other belongs to the SAME project (so the tenant leg passes for it) but
    is NOT in the exported key_ids (so ONLY the key leg excludes it). We assert BOTH:
      (a) the SERVICE (`images_for_keys`, called with the exported key_ids) excludes k_other's
          image -- this is the assertion the key leg makes load-bearing; drop
          `KeyImage.key_id.in_(key_ids)` and k_other's image is returned, turning this red;
      (b) the namespace-scoped BUNDLE excludes it too (defence in depth via the exported_keys loop).
    """
    _switch_images(True)
    pid = _mk_key_project()
    k_app = _add_key(pid, "btn.save", namespace="app")
    k_other = _add_key(pid, "btn.save", namespace="other")
    img_app = _attach(k_app, _png(color=(11, 22, 33)))
    img_other = _attach(k_other, _png(color=(44, 55, 66)))
    assert img_app != img_other
    # (a) SERVICE leg: call images_for_keys with ONLY the exported (k_app) key id. Both keys are
    # in project `pid`, so the tenant leg (project_id == pid) admits BOTH -- only the key leg keeps
    # k_other out. This is the load-bearing observation the bundle loop cannot make.
    with Session(engine) as s:
        by_key = key_images.images_for_keys(s, [k_app], pid)
    returned_blobs = {img.blob_key for imgs in by_key.values() for img in imgs}
    assert img_app in returned_blobs, ("the exported key's image must be returned", returned_blobs)
    assert k_other not in by_key, ("a non-exported same-project key must not be returned", by_key)
    assert img_other not in returned_blobs, (
        "the key leg must exclude a same-project non-exported key's image; dropping "
        "KeyImage.key_id.in_(key_ids) would surface it",
        returned_blobs,
    )
    # (b) BUNDLE leg: a namespace="app" export bundles only k_app's image (defence in depth).
    zbytes, info, _ = _build(pid, namespace="app")
    names = set(zipfile.ZipFile(io.BytesIO(zbytes)).namelist())
    assert f"images/{img_app}" in names, ("exported key's image present", names)
    assert f"images/{img_other}" not in names, ("non-exported key's image excluded", names)
    assert info["image_count"] == 1, info
    print("  [C/M1] key leg load-bearing at the service; namespace bundle excludes the other key")


# ============================================================================
# [H1] every ZIP member is a traversal-proof sanitised basename
# ============================================================================
def _assert_all_members_safe(names, where):
    for n in names:
        assert not n.startswith("/"), (where, "absolute member", n)
        assert "\\" not in n, (where, "backslash member", n)
        segs = n.split("/")
        assert ".." not in segs and "." not in segs, (where, "traversal segment", n)
        for seg in segs:
            assert not seg.startswith("."), (where, "leading-dot segment", n)
            # L10: no Windows-illegal filename chars survive in a member segment.
            for bad in '?:*<>|"':
                assert bad not in seg, (where, f"windows-illegal {bad!r} in member", n)


def test_zip_safe_member_drive_fullwidth_and_windows_illegal():
    """L1/L10 (defence in depth): `_zip_safe_member` -- the documented bundle security boundary --
    neutralises a Windows drive prefix, a fullwidth dot-dot / fullwidth solidus, and the
    Windows-illegal filename chars, none of which the ASCII-only checks caught before."""
    zsm = key_images._zip_safe_member
    # a drive-absolute Windows path -> relative, no colon, cannot climb.
    got = zsm("C:\\evil\\pwn")
    assert not got.startswith("/") and ":" not in got and "\\" not in got, got
    assert ".." not in got.split("/"), got
    # fullwidth dot-dot (U+FF0E) is NFKC-folded to ".." then dropped as a traversal segment.
    assert zsm("\uff0e\uff0e/evil") == "evil", zsm("\uff0e\uff0e/evil")
    # fullwidth solidus (U+FF0F) is folded to a real separator, so it splits (never survives).
    assert "\uff0f" not in zsm("a\uff0f..\uff0fb"), zsm("a\uff0f..\uff0fb")
    # each Windows-illegal char is stripped from a segment.
    out = zsm('a?b:c*d<e>f|g"h')
    for bad in '?:*<>|"':
        assert bad not in out, (bad, out)
    # an entirely unusable name still yields the fallback, never empty.
    assert zsm(":::") == "export", zsm(":::")
    # L7: C1 control chars (U+0080..U+009F) are stripped like the C0 controls + DEL.
    assert zsm("x\x85y") == "xy", repr(zsm("x\x85y"))
    assert zsm("a\x80\x9fb") == "ab", repr(zsm("a\x80\x9fb"))
    # L6: a Windows RESERVED DEVICE NAME is de-reserved with an underscore, with OR without an
    # extension, so a `namespace=CON` export extracts cleanly on Windows.
    assert zsm("CON") == "CON_", zsm("CON")
    assert zsm("CON.en-US.json") == "CON_.en-US.json", zsm("CON.en-US.json")
    assert zsm("com1") == "com1_", zsm("com1")
    assert zsm("LPT9.txt") == "LPT9_.txt", zsm("LPT9.txt")
    # a name that merely CONTAINS a device name is untouched (not reserved).
    assert zsm("CONSOLE.json") == "CONSOLE.json", zsm("CONSOLE.json")
    # L5: the key-FILE member stays ONE segment even for a COMPATIBILITY separator -- the NFKC
    # fold now runs BEFORE the basename split, so a fullwidth solidus / reverse solidus / small
    # solidus folds to a boundary the split consumes rather than sprouting a subdirectory.
    zkf = key_images._zip_safe_key_file
    # fullwidth solidus (U+FF0F), fullwidth reverse solidus (U+FF3C), small solidus (U+FE68).
    for cp in (0xFF0F, 0xFF3C, 0xFE68):
        got = zkf("a" + chr(cp) + "b.json")
        assert "/" not in got, ("key-file member must stay one segment (L5)", hex(cp), got)
    # U+2100 NFKC-folds to "a/c"; the key-file member still collapses to a single basename.
    assert "/" not in zkf("x" + chr(0x2100) + "y.json"), zkf("x" + chr(0x2100) + "y.json")
    print("  [L1/L10/L5/L6/L7] _zip_safe_member folds drive/fullwidth/illegal/control/reserved")


def test_zip_member_names_are_traversal_safe():
    _switch_images(True)
    pid = _mk_key_project()
    kid = _add_key(pid, "trav.k", namespace="../../../../evil")
    _attach(kid)
    # service leg: a hostile key_file_name never becomes a traversal member; the key-file member
    # is a flat basename shared with the download name (H1).
    with Session(engine) as s:
        proj = s.get(Project, pid)
        ek: list = []
        data = key_files.export_locale(s, proj, "", namespace="../../../../evil", exported_keys=ek)
        fname = key_files.download_filename("../../../../evil", "en-US", "json")
        bundle, info = key_images.build_export_bundle(
            s,
            proj,
            "en-US",
            key_file_bytes=data,
            key_file_name=fname,
            exported_keys=ek,
            qualify=False,
            generated_at=datetime.now(timezone.utc),
        )
    zbytes = b"".join(key_images.iter_bundle(bundle))
    names = zipfile.ZipFile(io.BytesIO(zbytes)).namelist()
    _assert_all_members_safe(names, "service")
    assert info["key_file"] == "evil.en-US.json", ("basenamed key-file member", info)
    assert info["key_file"] in names, names
    assert info["download_name"] == "evil.en-US.zip", info

    # wire legs: hostile namespace query on /api, web and CLI.
    _auth(True)
    try:
        head = {"X-Loc-Token": _key_for(f"ki-trav-{_uniq()}@x.com")}
        c = TestClient(app)
        for ns in ("../../../../evil", "/etc/cron.d/pwn"):
            r = c.get(
                f"/api/projects/{pid}/keys/export",
                params={"include_string_images": "1", "namespace": ns},
                headers=head,
            )
            assert r.status_code == 200 and r.headers["content-type"] == "application/zip", (
                ns,
                r.status_code,
            )
            _assert_all_members_safe(
                zipfile.ZipFile(io.BytesIO(r.content)).namelist(), f"/api ns={ns}"
            )
        # web
        wc = _web_login(f"ki-travweb-{_uniq()}@x.com")
        rw = wc.get(
            f"/projects/{pid}/keys/export",
            params={"include_string_images": "1", "namespace": "../../../../evil"},
            follow_redirects=False,
        )
        assert rw.status_code == 200 and rw.headers["content-type"] == "application/zip", rw
        _assert_all_members_safe(zipfile.ZipFile(io.BytesIO(rw.content)).namelist(), "web")
    finally:
        _auth(False)

    # CLI
    import tempfile

    _switch_images(True)
    outpath = os.path.join(tempfile.mkdtemp(prefix="kiexp_trav_"), "b.zip")
    rc, out = _cli(
        [
            "keys",
            "export",
            "--project",
            str(pid),
            "--namespace",
            "/etc/cron.d/pwn",
            "--include-string-images",
            "-o",
            outpath,
        ]
    )
    assert rc == 0 and out.get("ok"), (rc, out)
    with open(outpath, "rb") as fh:
        _assert_all_members_safe(zipfile.ZipFile(io.BytesIO(fh.read())).namelist(), "cli")
    print("  [H1] ZIP members are traversal-proof basenames (service + /api + web + CLI) OK")


# ============================================================================
# [H2] the MCP export_keys tool has NO image option
# ============================================================================
def test_mcp_export_keys_has_no_image_option():
    import inspect

    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    import registry as mcp_registry
    import server as mcp_server

    tool = next(t for t in mcp_registry.TOOLS if t.name == "export_keys")
    props = (tool.input_schema or {}).get("properties") or {}
    assert "include_string_images" not in props, ("MCP must not expose the option (H2)", props)
    sig = inspect.signature(mcp_server.export_keys)
    assert "include_string_images" not in sig.parameters, (
        "server.export_keys must not accept the option (H2)",
        list(sig.parameters),
    )
    assert any("never exported through MCP" in p for p in tool.pins), tool.pins
    assert "never exported through MCP" in tool.summary, tool.summary
    print("  [H2] MCP export_keys has no include_string_images (no param, no signature) OK")


# ============================================================================
# [M2] the bundle streams a spooled file; blobs are read one at a time
# ============================================================================
def test_bundle_streams_and_reads_one_blob_at_a_time():
    _switch_images(True)
    pid = _mk_key_project()
    blobs = []
    for i in range(3):
        kid = _add_key(pid, f"stream.k{i}")
        blobs.append(_attach(kid, _png(color=(i + 5, i + 5, i + 5))))
    real = key_images.read_bytes
    live = {"cur": 0, "max": 0, "calls": 0}

    def wrapped(img):
        live["cur"] += 1
        live["calls"] += 1
        live["max"] = max(live["max"], live["cur"])
        try:
            return real(img)
        finally:
            live["cur"] -= 1

    key_images.read_bytes = wrapped
    try:
        with Session(engine) as s:
            proj = s.get(Project, pid)
            ek: list = []
            data = key_files.export_locale(s, proj, "", exported_keys=ek)
            fname = key_files.download_filename("project", "en-US", "json")
            bundle, _info = key_images.build_export_bundle(
                s,
                proj,
                "en-US",
                key_file_bytes=data,
                key_file_name=fname,
                exported_keys=ek,
                qualify=True,
                generated_at=datetime.now(timezone.utc),
            )
    finally:
        key_images.read_bytes = real
    assert not isinstance(bundle, (bytes, bytearray)), "bundle must be a streamed file-like"
    assert hasattr(bundle, "read") and hasattr(bundle, "seek"), bundle
    assert live["calls"] == 3, ("one read per planned image", live)
    assert live["max"] == 1, ("blobs must be read ONE at a time, never accumulated", live)
    chunks = b"".join(key_images.iter_bundle(bundle))
    assert chunks[:2] == b"PK", "iter_bundle yields a real ZIP"
    names = set(zipfile.ZipFile(io.BytesIO(chunks)).namelist())
    for b in blobs:
        assert f"images/{b}" in names, names
    print("  [M2] bundle is a streamed spool; blobs read one at a time (peak concurrency 1) OK")


# ============================================================================
# [M3] byte-stable given the same (keys, images, generated_at)
# ============================================================================
def test_archive_byte_stable():
    _switch_images(True)
    pid = _mk_key_project()
    kid = _add_key(pid, "stable.k")
    _attach(kid, _png(color=(7, 8, 9)), caption="cap")
    fixed = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    z1, _i1, _f1 = _build(pid, when=fixed)
    z2, _i2, _f2 = _build(pid, when=fixed)
    assert z1 == z2, "same input + same generated_at must be byte-identical (M3)"
    # generated_at is the ONLY wall-clock input: a different one changes the manifest bytes.
    z3, _i3, _f3 = _build(pid, when=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert z3 != z1, "a different generated_at must change the archive (proves it is injected)"
    m1 = _json.loads(zipfile.ZipFile(io.BytesIO(z1)).read("manifest.json"))
    assert m1["generated_at"] == "2026-09-05T12:00:00Z", m1
    print("  [M3] archive byte-stable for a fixed generated_at; injected, not wall-clock OK")


# ============================================================================
# [L2] the manifest caption is scrubbed
# ============================================================================
def test_caption_scrubbed_in_manifest():
    _switch_images(True)
    pid = _mk_key_project()
    kid = _add_key(pid, "cap.k")
    _attach(
        kid,
        caption="token sk-ant-api03-DEADBEEFDEADBEEFDEADBEEF secret=hunter2 bob@example.com",
    )
    zbytes, _info, _ = _build(pid)
    m = _json.loads(zipfile.ZipFile(io.BytesIO(zbytes)).read("manifest.json"))
    cap = m["images"][0]["caption"]
    assert "sk-ant-api03-DEADBEEF" not in cap, ("key-shaped secret must be scrubbed", cap)
    assert "bob@example.com" not in cap, ("email (pii seam) must be scrubbed", cap)
    assert "hunter2" not in cap, ("credential assignment must be scrubbed", cap)
    print("  [L2] caption scrubbed through secrets + pii seam + credential assignments OK")


# ============================================================================
# [L3] soft-deleted / pending images are excluded
# ============================================================================
def test_soft_deleted_and_pending_excluded():
    _switch_images(True)
    pid = _mk_key_project()
    k_ok = _add_key(pid, "life.ok")
    ok_blob = _attach(k_ok)
    k_del = _add_key(pid, "life.del")
    del_blob = _attach(k_del)
    with Session(engine) as s:
        row = s.exec(select(KeyImage).where(KeyImage.blob_key == del_blob)).one()
        row.deleted_at = _utcnow()
        s.add(row)
        s.commit()
    # a pending (ready=False) stub of a DIFFERENT key.
    k_stub = _add_key(pid, "life.stub")
    stub_blob = _attach(k_stub)
    with Session(engine) as s:
        row = s.exec(select(KeyImage).where(KeyImage.blob_key == stub_blob)).one()
        row.ready = False
        s.add(row)
        s.commit()
    zbytes, info, _ = _build(pid)
    names = set(zipfile.ZipFile(io.BytesIO(zbytes)).namelist())
    assert f"images/{ok_blob}" in names, names
    assert f"images/{del_blob}" not in names, ("soft-deleted image must not be bundled", names)
    assert f"images/{stub_blob}" not in names, ("pending stub must not be bundled", names)
    assert info["image_count"] == 1, info
    print("  [L3] soft-deleted and ready=False images are excluded from the bundle OK")


# ============================================================================
# [L6] a duplicate blob_key across live rows is de-duped
# ============================================================================
def test_duplicate_blob_key_deduped():
    _switch_images(True)
    pid = _mk_key_project()
    k1 = _add_key(pid, "dup.k1")
    b1 = _attach(k1)
    k2 = _add_key(pid, "dup.k2")
    _attach(k2)
    # DB drift: two live rows share one blob_key. The bundle must not emit two members with the
    # same name (zipfile would warn + double-write); the manifest lists it once.
    with Session(engine) as s:
        rows = s.exec(select(KeyImage).where(KeyImage.project_id == pid)).all()
        rows[1].blob_key = rows[0].blob_key
        s.add(rows[1])
        s.commit()
    zbytes, info, _ = _build(pid)
    names = zipfile.ZipFile(io.BytesIO(zbytes)).namelist()
    dups = [n for n, c in Counter(names).items() if c > 1]
    assert not dups, ("no duplicate ZIP member names", names)
    assert names.count(f"images/{b1}") == 1, names
    assert info["image_count"] == 1, info
    print("  [L6] a duplicate blob_key across live rows is de-duped (one member) OK")


# ============================================================================
# [K] the caps refuse WHOLE (no partial artifact)
# ============================================================================
def test_caps_refuse_whole():
    _switch_images(True)
    pid = _mk_key_project()
    for i in range(3):
        kid = _add_key(pid, f"cap.k{i}")
        _attach(kid, _png(color=(i + 1, i + 1, i + 1)))
    settings.set_many({"key_image_export_max_files": "2"})
    settings.invalidate_cache()
    try:
        _build(pid)
        _fail("over the file-count cap must be refused")
    except key_images.KeyImageError as exc:
        assert "key_image_export_max_files" in str(exc), exc
    finally:
        settings.set_many({"key_image_export_max_files": str(config.KEY_IMAGE_EXPORT_MAX_FILES)})
        settings.invalidate_cache()
    settings.set_many({"key_image_export_max_bytes": "10"})
    settings.invalidate_cache()
    try:
        _build(pid)
        _fail("over the byte cap must be refused")
    except key_images.KeyImageError as exc:
        assert "key_image_export_max_bytes" in str(exc), exc
    finally:
        settings.set_many({"key_image_export_max_bytes": str(config.KEY_IMAGE_EXPORT_MAX_BYTES)})
        settings.invalidate_cache()
    print("  [K] caps: over file-count OR byte ceiling is refused whole, no partial ZIP OK")


# ============================================================================
# [O] feature OFF -> include_string_images ignored (plain file)
# ============================================================================
def test_feature_off_ignores_option():
    _auth(True)
    try:
        _switch_images(False)
        pid = _mk_key_project()
        kid = _add_key(pid, "off.k")
        _switch_images(True)
        _attach(kid)
        _switch_images(False)
        head = {"X-Loc-Token": _key_for(f"ki-off-{_uniq()}@x.com")}
        c = TestClient(app)
        r = c.get(
            f"/api/projects/{pid}/keys/export",
            params={"include_string_images": "1"},
            headers=head,
        )
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.headers["content-type"].startswith("application/json"), r.headers
        assert r.content[:2] != b"PK", "feature off must NOT return a ZIP"
    finally:
        _switch_images(True)
        _auth(False)
    print("  [O] feature OFF: include_string_images is ignored, a plain JSON file comes back OK")


# ============================================================================
# [S] the surfaces
# ============================================================================
def test_api_surface_returns_zip():
    _auth(True)
    try:
        _switch_images(True)
        pid = _mk_key_project()
        kid = _add_key(pid, "api.k")
        blob = _attach(kid)
        head = {"X-Loc-Token": _key_for(f"ki-api-{_uniq()}@x.com")}
        c = TestClient(app)
        r = c.get(
            f"/api/projects/{pid}/keys/export",
            params={"include_string_images": "1"},
            headers=head,
        )
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.headers["content-type"] == "application/zip", r.headers
        assert ".zip" in r.headers.get("content-disposition", ""), r.headers
        assert r.headers.get("x-key-image-count") == "1", r.headers
        names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
        assert f"images/{blob}" in names and "manifest.json" in names, names
        # a low cap on the wire -> a clean 400, never a partial download. L5: the restore is in
        # a finally so a mid-test failure cannot leak the "0" cap into every later test.
        settings.set_many({"key_image_export_max_files": "0"})
        settings.invalidate_cache()
        try:
            r2 = c.get(
                f"/api/projects/{pid}/keys/export",
                params={"include_string_images": "1"},
                headers=head,
            )
            assert r2.status_code == 400, (r2.status_code, r2.text)
        finally:
            settings.set_many(
                {"key_image_export_max_files": str(config.KEY_IMAGE_EXPORT_MAX_FILES)}
            )
            settings.invalidate_cache()
    finally:
        _auth(False)
    print("  [S/api] GET /api/.../keys/export?include_string_images=1 -> zip; over-cap 400 OK")


def test_web_surface_returns_zip():
    _auth(True)
    try:
        _switch_images(True)
        pid = _mk_key_project()
        kid = _add_key(pid, "web.k")
        blob = _attach(kid)
        c = _web_login(f"ki-web-{_uniq()}@x.com")
        r = c.get(
            f"/projects/{pid}/keys/export",
            params={"include_string_images": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.headers["content-type"] == "application/zip", r.headers
        names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
        assert f"images/{blob}" in names and "manifest.json" in names, names
    finally:
        _auth(False)
    print("  [S/web] GET /projects/.../keys/export?include_string_images=1 -> application/zip OK")


def test_cli_surface_writes_zip_and_requires_out():
    import tempfile

    _auth(False)
    _switch_images(True)
    pid = _mk_key_project()
    kid = _add_key(pid, "cli.k")
    blob = _attach(kid)
    outdir = tempfile.mkdtemp(prefix="kiexp_")
    outpath = os.path.join(outdir, "bundle.zip")
    rc, out = _cli(
        ["keys", "export", "--project", str(pid), "--include-string-images", "-o", outpath]
    )
    assert rc == 0 and out.get("ok"), ("local export bundle", rc, out)
    # L10: the CLI --json carries the bundle facts (image count/bytes), which web/api put in
    # the audit row -- otherwise no CLI surface tells the caller how many images shipped.
    assert out.get("img_info", {}).get("image_count") == 1, ("--json carries img_info", out)
    assert out["img_info"]["image_bytes"] > 0, out
    with open(outpath, "rb") as fh:
        names = set(zipfile.ZipFile(io.BytesIO(fh.read())).namelist())
    assert f"images/{blob}" in names and "manifest.json" in names, names
    rc2, _out2 = _cli(["keys", "export", "--project", str(pid), "--include-string-images"])
    assert rc2 == 2, ("stdout bundle must be refused", rc2, _out2)
    # L8 parity: the flag without -o is refused (exit 2) even with the image feature OFF -- the
    # guard fires on the flag, not on want_images, so local matches the remote arm's refusal.
    _switch_images(False)
    try:
        rc3, _out3 = _cli(["keys", "export", "--project", str(pid), "--include-string-images"])
        assert rc3 == 2, ("flag without -o must exit 2 even feature-off (L8)", rc3, _out3)
    finally:
        _switch_images(True)
    print("  [S/cli] keys export --include-string-images -o writes a ZIP; --json img_info OK")


def _remote_runner(token):
    """A `--json --remote` CLI runner whose httpx transport forwards to the in-process app
    (no live network). Unlike the workflow-authoring harness it forwards ALL response headers
    except content-length/transfer-encoding, so the bundle-fact headers (X-Key-Image-*) and
    content-type reach the CLI arm -- which is exactly what L9 must exercise."""
    import logging

    import httpx

    from app.cli import _remote
    from app.cli.__main__ import main as cli_main

    # httpx logs each request at INFO; keep it out of the captured stdout so the --json line
    # parses cleanly (the local `_cli` helper never sees these because it makes no http call).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    tc = TestClient(app)

    def handler(request):
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode()
        r = tc.request(request.method, path, headers=dict(request.headers), content=request.content)
        fwd = {
            k: v
            for k, v in r.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding")
        }
        return httpx.Response(r.status_code, content=r.content, headers=fwd)

    prev = _remote._TRANSPORT
    _remote._TRANSPORT = httpx.MockTransport(handler)

    def run(argv):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = cli_main(["--json", "--remote", "http://127.0.0.1", "--token", token, *argv])
        out = buf.getvalue()
        try:
            return rc, _json.loads(out)
        except Exception:
            # Fall back to the last JSON-object line (any stray log line is ignored).
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return rc, _json.loads(line)
                    except Exception:
                        continue
            return rc, {"_raw": out}

    return run, (lambda: setattr(_remote, "_TRANSPORT", prev))


def test_cli_remote_surface_parity():
    """L9: the `--remote` arm has its own coverage. Proven over the in-process transport:
    happy path (streamed to disk, img_info carried), over-cap (structured `code` -> exit 2,
    L4/L7), feature-off (plain file, NO img_info -- L5 parity with the local arm), and the
    flag-without-`-o` guard (exit 2, L8). The streamed write (L8) is what produces the file."""
    import tempfile

    _switch_images(True)
    pid = _mk_key_project()
    kid = _add_key(pid, "rem.k")
    blob = _attach(kid)
    _auth(False)  # the transport carries the token; isolate the round-trip from the login wall
    token = _key_for(f"ki-remote-{_uniq()}@x.com")
    run, restore = _remote_runner(token)
    try:
        outdir = tempfile.mkdtemp(prefix="kiexp_rem_")
        out = os.path.join(outdir, "bundle.zip")
        # happy path: streamed to disk, img_info reported from the response headers (L8/L10).
        rc, o = run(["keys", "export", "--project", str(pid), "--include-string-images", "-o", out])
        assert rc == 0 and o.get("ok"), ("remote export bundle", rc, o)
        assert o.get("img_info", {}).get("image_count") == 1, ("remote --json img_info", o)
        assert o["img_info"]["image_bytes"] > 0, o
        with open(out, "rb") as fh:
            names = set(zipfile.ZipFile(io.BytesIO(fh.read())).namelist())
        assert f"images/{blob}" in names and "manifest.json" in names, names

        # over-cap: the server refuses whole; the remote arm re-classes to exit 2 off the
        # STRUCTURED server code (L4), not a message substring.
        settings.set_many({"key_image_export_max_files": "0"})
        settings.invalidate_cache()
        try:
            out2 = os.path.join(outdir, "over.zip")
            rc2, o2 = run(
                ["keys", "export", "--project", str(pid), "--include-string-images", "-o", out2]
            )
            assert rc2 == 2, ("over-cap remote must exit 2 (L4/L7)", rc2, o2)
            assert not os.path.exists(out2), ("no partial artifact on refusal", out2)
        finally:
            settings.set_many(
                {"key_image_export_max_files": str(config.KEY_IMAGE_EXPORT_MAX_FILES)}
            )
            settings.invalidate_cache()

        # feature OFF: the server returns the PLAIN file; the remote arm emits the SAME shape the
        # local arm does -- notably WITHOUT img_info (L5 parity).
        _switch_images(False)
        try:
            out3 = os.path.join(outdir, "plain.json")
            rc3, o3 = run(
                ["keys", "export", "--project", str(pid), "--include-string-images", "-o", out3]
            )
            assert rc3 == 0 and o3.get("ok"), ("feature-off remote plain export", rc3, o3)
            assert "img_info" not in o3, ("feature-off must NOT carry img_info (L5)", o3)
            assert os.path.exists(out3), out3
            # the local arm, feature-off + -o, is the same shape (no img_info).
            out3b = os.path.join(outdir, "plain_local.json")
            rc3b, o3b = _cli(
                ["keys", "export", "--project", str(pid), "--include-string-images", "-o", out3b]
            )
            assert rc3b == 0 and "img_info" not in o3b, ("local feature-off shape matches", o3b)
        finally:
            _switch_images(True)

        # flag without -o: refused (exit 2) on the remote arm too (L8 guard).
        rc4, _o4 = run(["keys", "export", "--project", str(pid), "--include-string-images"])
        assert rc4 == 2, ("remote flag without -o must exit 2 (L8)", rc4, _o4)
    finally:
        restore()
        _auth(False)
    print("  [S/remote] --remote arm: bundle+img_info, over-cap exit 2, feature-off no img_info OK")


def test_remote_stream_failure_leaves_prior_bundle_intact():
    """MED-1: a mid-stream transport fault on the `--remote` download must NOT leave a truncated /
    0-byte artifact and must NOT destroy a previously downloaded bundle at the same path. The
    stream lands in `<out>.part` and is os.replace'd onto `out` only after the last byte. The
    suite's MockTransport-over-TestClient harness cannot see this (it materialises the whole body
    before the CLI streams), so this drives RemoteClient.stream_to_file directly with a byte stream
    that yields one block and then raises."""
    import tempfile

    import httpx

    from app.cli import _remote
    from app.cli._common import EXIT_UNREACHABLE, CliError

    class _FailingStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"PK\x03\x04truncated-partial-bundle"
            raise httpx.ReadTimeout("connection dropped mid-stream")

        def close(self):
            pass

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/zip"}, stream=_FailingStream()
        )

    client = _remote.RemoteClient(
        "http://127.0.0.1", "lsu_x", transport=httpx.MockTransport(handler)
    )
    outdir = tempfile.mkdtemp(prefix="kiexp_fail_")

    # (A) a NEW destination: the fault leaves NO file (no truncated/0-byte artifact) + no .part.
    new_dest = os.path.join(outdir, "new.zip")
    raised = False
    try:
        client.stream_to_file("/api/whatever", new_dest)
    except CliError as exc:
        raised = True
        assert exc.code == EXIT_UNREACHABLE, ("mid-stream fault -> exit 3", exc.code)
    assert raised, "a mid-stream fault must raise"
    assert not os.path.exists(new_dest), ("no truncated/0-byte artifact on a new dest", new_dest)
    assert not os.path.exists(new_dest + ".part"), "the .part temp must be cleaned up"

    # (B) an EXISTING good bundle: a failed re-download leaves it byte-for-byte intact.
    existing = os.path.join(outdir, "good.zip")
    with open(existing, "wb") as fh:
        fh.write(b"PREVIOUS-GOOD-BUNDLE" * 100)
    with open(existing, "rb") as fh:
        before = fh.read()
    raised = False
    try:
        client.stream_to_file("/api/whatever", existing)
    except CliError as exc:
        raised = True
        assert exc.code == EXIT_UNREACHABLE, exc.code
    assert raised, "a mid-stream fault must raise"
    with open(existing, "rb") as fh:
        after = fh.read()
    assert after == before, (
        "the previous bundle must be untouched on a failed re-download",
        len(after),
    )
    assert not os.path.exists(existing + ".part"), "the .part temp must be cleaned up"
    print("  [MED-1] a mid-stream --remote fault leaves no partial artifact; prior bundle intact")


def test_response_headers_content_length_and_web_bundle_facts():
    """L3: the two response-header fixes get DIRECT coverage at the route level (not through the
    `--remote` harness, which strips content-length). The /api and web ZIP responses BOTH carry an
    explicit Content-Length equal to the streamed body and no chunked transfer-encoding, and the
    WEB leg carries the X-Key-Image-* bundle facts -- previously asserted only on the /api leg."""
    _auth(True)
    try:
        _switch_images(True)
        pid = _mk_key_project()
        kid = _add_key(pid, "hdr.k")
        _attach(kid)
        # /api leg: an explicit Content-Length that matches the body, no chunked encoding.
        head = {"X-Loc-Token": _key_for(f"ki-hdr-api-{_uniq()}@x.com")}
        c = TestClient(app)
        r = c.get(
            f"/api/projects/{pid}/keys/export",
            params={"include_string_images": "1"},
            headers=head,
        )
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip", r.headers
        assert "transfer-encoding" not in r.headers, (
            "no chunked encoding on the bundle",
            r.headers,
        )
        assert int(r.headers["content-length"]) == len(r.content), (
            "explicit Content-Length must equal the body (L2)",
            r.headers.get("content-length"),
            len(r.content),
        )
        # web leg: the bundle-fact headers ride here too (L3), plus the same exact Content-Length.
        wc = _web_login(f"ki-hdr-web-{_uniq()}@x.com")
        rw = wc.get(
            f"/projects/{pid}/keys/export",
            params={"include_string_images": "1"},
            follow_redirects=False,
        )
        assert rw.status_code == 200 and rw.headers["content-type"] == "application/zip", rw.headers
        assert rw.headers.get("x-key-image-count") == "1", (
            "web X-Key-Image-Count (L3)",
            rw.headers,
        )
        assert int(rw.headers.get("x-key-image-bytes", "0")) > 0, (
            "web X-Key-Image-Bytes (L3)",
            rw.headers,
        )
        assert int(rw.headers["content-length"]) == len(rw.content), (
            rw.headers.get("content-length"),
            len(rw.content),
        )
    finally:
        _auth(False)
    print(
        "  [L3] /api + web ZIP carry an exact Content-Length; web carries the X-Key-Image-* facts"
    )


def main() -> None:
    test_off_is_byte_identical()
    test_on_bundle_shape_and_manifest_link()
    test_two_org_confinement()
    test_key_leg_is_loadbearing_at_the_service()
    test_zip_safe_member_drive_fullwidth_and_windows_illegal()
    test_zip_member_names_are_traversal_safe()
    test_mcp_export_keys_has_no_image_option()
    test_bundle_streams_and_reads_one_blob_at_a_time()
    test_archive_byte_stable()
    test_caption_scrubbed_in_manifest()
    test_soft_deleted_and_pending_excluded()
    test_duplicate_blob_key_deduped()
    test_caps_refuse_whole()
    test_feature_off_ignores_option()
    test_api_surface_returns_zip()
    test_web_surface_returns_zip()
    test_cli_surface_writes_zip_and_requires_out()
    test_cli_remote_surface_parity()
    test_remote_stream_failure_leaves_prior_bundle_intact()
    test_response_headers_content_length_and_web_bundle_facts()
    print("KEYS IMAGE EXPORT OK")


if __name__ == "__main__":
    main()
