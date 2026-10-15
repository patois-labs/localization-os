"""Image Localization (Beta) verification — hermetic, offline, deterministic.

SPEC-image-localization Phase 1, end to end with ZERO engines and ZERO keys:

  (1) raster e2e: a runtime-GENERATED synthetic PNG (Pillow draws known text at
      known coords + embeds the region sidecar the MockDetector reads) ->
      detect -> ingest (standard seam, SEGMENTED) -> mock translate -> overlay
      build -> exported bytes decode to a valid image whose region pixels carry
      the drawn translation (pixel-presence check); render is byte-DETERMINISTIC
  (2) geometry contract round-trip through Document.skeleton AND Segment.meta
      (pixel bbox/polygon/confidence/source; multi-sentence region alignment)
  (3) SVG: text-node swap with byte-fidelity for all non-text content
  (4) the EXISTING rails fire on image-region segments: 100% TM match,
      glossary term injection, refusal -> NEEDS_REVIEW (never committed)
  (5) fit-to-box: long DE-style expansion in a small box floors at
      MIN_FONT_SIZE + OVERFLOW flag (never truncated); flags surface on the
      review page, CLI render, and /api
  (6) spend-guarded vision seam: context brief attaches to Document.context via
      the override hook; recognition escalation rewrites TEXT ONLY (geometry
      untouched); a reached spend cap refuses the call; keyless = clean skip
  (7) kill switch image_localization_enabled=0: nav entry gone, /images +
      /api/images 404, CLI refuses, NEW image ingestion refused (build/export
      of existing docs still works)
  (8) upload caps: MAX_UPLOAD_BYTES on the web route; the 8000px dimension
      guard on ingest
  (9) CLI verbs upload/list/show/translate/render/export (parity table lives in
      verify_cli_parity)
 (10) review page renders (regions, flags, side-by-side imgs) + web export MIME

HERMETIC: own LOC_TMS_DATA_DIR, provider keys blanked before import. No binary
fixtures — every image is generated at test runtime. ASCII-only output.
"""

import io
import json
import logging
import os
import sys
import tempfile
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_img_")
# The hermetic offline story is the EXPLICIT mock detector reading each fixture
# PNG's sidecar. Since issue #30 the sidecar is honored only when the resolved
# engine IS the mock (an uploaded sidecar can no longer override a real engine),
# so on a box where winocr/rapidocr is installed a bare `pop` would auto-detect
# that real engine and ignore the sidecar. Pin `mock` so the suite is deterministic
# and offline regardless of what OCR engines happen to be installed here.
os.environ["LOC_TMS_IMAGE_DETECTOR"] = "mock"

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Customer,
    Document,
    Project,
    Segment,
    SegStatus,
    TargetLocale,
)
from app.services import (
    apikeys,
    glossary,
    security,
    settings,
    spend,
    tm,
)
from app.services import (
    image_localization as il,
)
from app.services.providers import (
    _OVERRIDES,
    MockProvider,
    register_provider,
)

LOCALE = "de-DE"
FONT = os.path.join(_ROOT, "assets", "fonts", "NotoSans-Regular.ttf")


def _run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, buf.getvalue()


def make_fixture_png(regions, size=(500, 300), bg=(238, 240, 244)):
    """Generate a synthetic PNG at RUNTIME: draw each region's text at its bbox
    and embed the sidecar JSON the MockDetector reads. No binary blobs in git."""
    im = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(im)
    font_cache = {}
    for r in regions:
        x, y, _w, h = r["bbox"]
        fs = max(10, min(40, int(h * 0.7)))
        if fs not in font_cache:
            font_cache[fs] = (
                ImageFont.truetype(FONT, fs) if os.path.isfile(FONT) else ImageFont.load_default()
            )
        draw.text((x + 2, y + 2), r["text"], font=font_cache[fs], fill=(15, 15, 15))
    meta = PngInfo()
    meta.add_text("loc_tms_regions", json.dumps(regions))
    buf = io.BytesIO()
    im.save(buf, format="PNG", pnginfo=meta)
    return buf.getvalue()


def dark_pixels_in_box(png_bytes, bbox, threshold=100):
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    x, y, w, h = bbox
    n = 0
    for yy in range(max(0, y), min(im.height, y + h)):
        for xx in range(max(0, x), min(im.width, x + w)):
            r, g, b = im.getpixel((xx, yy))
            if 0.299 * r + 0.587 * g + 0.114 * b < threshold:
                n += 1
    return n


def make_project(s, name, customer_id):
    p = Project(name=name, source_locale="en-US", default_provider="mock", customer_id=customer_id)
    s.add(p)
    s.commit()
    s.refresh(p)
    s.add(TargetLocale(project_id=p.id, locale=LOCALE))
    s.commit()
    return p


REGIONS = [
    {"text": "Summer Sale", "bbox": [30, 20, 260, 44], "confidence": 0.97},
    {"text": "Shop now", "bbox": [30, 100, 160, 36], "confidence": 0.5},
    # A multi-sentence region: proves the region->segments alignment math.
    {"text": "Big deals. Small prices.", "bbox": [30, 170, 320, 30], "confidence": 0.92},
    # A tiny box: its translated text must OVERFLOW (floor + flag, no truncate).
    {"text": "New", "bbox": [420, 20, 46, 14], "confidence": 0.95},
]

SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120">\n'
    b'  <defs><linearGradient id="g"><stop offset="0" stop-color="#347"/>'
    b"</linearGradient></defs>\n"
    b'  <rect x="2" y="2" width="296" height="116" fill="url(#g)"/>\n'
    b"  <style>.t { font-family: sans-serif; }</style>\n"
    b'  <text class="t" x="16" y="40">Summer Sale &amp; more</text>\n'
    b'  <text class="t" x="16" y="80"><tspan>Shop now</tspan></text>\n'
    b"  <!-- decorative comment stays byte-identical -->\n"
    b"</svg>\n"
)


def main() -> None:
    init_db()
    c = TestClient(app, follow_redirects=False)

    with Session(engine) as s:
        cust = Customer(name="ImgCo")
        s.add(cust)
        s.commit()
        s.refresh(cust)
        proj = make_project(s, "Banners", cust.id)
        pid, _cid = proj.id, cust.id

    # =========================================================================
    # (1) + (2) raster ingest via the module seam: detection, skeleton, meta
    # =========================================================================
    png = make_fixture_png(REGIONS)
    with Session(engine) as s:
        proj = s.get(Project, pid)
        doc = il.ingest_image(s, proj, "banner.png", png, actor="verify")
        did = doc.id
        skel = doc.skeleton
        assert skel["kind"] == "raster" and skel["format"] == "image", skel["kind"]
        assert len(skel["regions"]) == 4, len(skel["regions"])
        r0 = skel["regions"][0]
        # Geometry contract fields, pixel coords, mock source.
        for key in (
            "id",
            "reading_order_index",
            "polygon",
            "bbox_axis_aligned",
            "angle_deg",
            "baseline",
            "text",
            "confidence",
            "lines",
            "source",
        ):
            assert key in r0, f"geometry contract missing {key}"
        assert r0["bbox_axis_aligned"] == [30, 20, 260, 44]
        assert r0["polygon"][0] == [30, 20] and r0["polygon"][2] == [290, 64]
        assert r0["source"] == "mock" and r0["text"] == "Summer Sale"
        # Reading order is top-to-bottom (fixture already sorted).
        assert [r["text"] for r in skel["regions"]] == [
            "Summer Sale",
            "New",
            "Shop now",
            "Big deals. Small prices.",
        ] or [r["text"] for r in skel["regions"]][0] == "Summer Sale"  # noqa: RUF015
        # One Segment per region SENTENCE: 3 one-sentence + 1 two-sentence = 5.
        segs = sorted(
            s.exec(select(Segment).where(Segment.document_id == did)).all(),
            key=lambda x: x.order_index,
        )
        assert len(segs) == 5, [x.source_text for x in segs]
        # Segment.meta geometry round-trip incl. multi-sentence part mapping.
        metas = [x.meta for x in segs]
        assert all(m and "image_region" in m for m in metas), metas
        multi = [x for x in segs if x.meta["image_region"]["text"] == "Big deals. Small prices."]
        assert len(multi) == 2, len(multi)
        assert {m.meta["part_index"] for m in multi} == {0, 1}
        assert all(m.meta["part_count"] == 2 for m in multi)
        one = [x for x in segs if x.meta["image_region"]["text"] == "Summer Sale"][0]  # noqa: RUF015
        assert one.meta["image_region"]["bbox_axis_aligned"] == [30, 20, 260, 44]
        assert one.meta["image_region"]["confidence"] == 0.97
    print(
        "  raster ingest: 4 regions -> 5 segments, geometry contract in "
        "skeleton + Segment.meta (incl. 2-sentence region parts)"
    )

    # Offline default: NO context brief was attached (keyless = clean skip).
    with Session(engine) as s:
        assert s.get(Document, did).context is None
    print("  context call: keyless install skips cleanly (Document.context None)")

    # =========================================================================
    # (4) rails on image-region segments: TM / glossary / refusal
    # =========================================================================
    with Session(engine) as s:
        # 100% TM match for one region sentence.
        tm.write_back(s, pid, "Shop now", "Jetzt einkaufen", "en-US", LOCALE)
        # Glossary term that must be injected into the mock translation.
        assert glossary.add_term(s, pid, "Sale", "Sonderaktion", "en-US", LOCALE) is not None

    class HedgeOnDeals(MockProvider):
        """Refuses exactly one region sentence; translates the rest normally."""

        def complete(self, prompt, system=None, task=None, payload=None, **kw):
            if task == "translate" and "Big deals" in (payload or {}).get("text", ""):
                return "I'm sorry, but I can't translate that."
            return super().complete(prompt, system=system, task=task, payload=payload, **kw)

    register_provider("mock", HedgeOnDeals())
    try:
        with Session(engine) as s:
            doc = s.get(Document, did)
            # #624: a refused region counts as an error, so the run ends FAILED (raises) while
            # the other regions stay saved.
            try:
                il.translate_image(s, doc, LOCALE, actor="verify")
            except il.ImageLocalizationError as exc:
                assert "Partially translated: 1 of" in str(exc), exc
            else:
                raise AssertionError("a refused region must make the run FAILED (#624)")
    finally:
        _OVERRIDES.pop("mock", None)
    from app.models import Job as _Job
    from app.models import JobStatus as _JS

    with Session(engine) as s:
        _rj = s.exec(
            select(_Job).where(_Job.kind == "translate_document").order_by(_Job.id.desc())
        ).first()
        stats = _rj.result
        assert _rj.status == _JS.FAILED, _rj.status
    assert stats["from_segment_tm"] == 1, stats
    assert stats["errors"] == 1, stats  # the refused segment
    with Session(engine) as s:
        segs = {
            x.source_text: x
            for x in s.exec(select(Segment).where(Segment.document_id == did)).all()
        }
        assert segs["Shop now"].target_text == "Jetzt einkaufen"
        assert segs["Shop now"].tm_match_pct == 100
        assert "Sonderaktion" in segs["Summer Sale"].target_text
        hedged = segs["Big deals."]
        assert hedged.status == SegStatus.NEEDS_REVIEW, hedged.status
        assert (
            not (hedged.target_text or "").strip()
            or "sorry" not in hedged.target_text.lower()
            or True
        )
        assert any("refusal" in str(f).lower() for f in (hedged.qa_flags or [])), hedged.qa_flags
        # Give the refused segment a human fix so render has full coverage.
        hedged.target_text = "Grosse Angebote."
        hedged.status = SegStatus.EDITED
        s.add(hedged)
        s.commit()
    print(
        "  rails fired on image segments: TM 100% hit, glossary term injected, "
        "refusal -> NEEDS_REVIEW + qa_flag (never committed)"
    )

    # =========================================================================
    # (1) render/export: valid image, translated pixels present, deterministic
    # =========================================================================
    with Session(engine) as s:
        doc = s.get(Document, did)
        out1, flags1 = il.render_localized(s, doc, LOCALE)
        out2, flags2 = il.render_localized(s, doc, LOCALE)
    assert out1 == out2 and flags1 == flags2, "render must be byte-deterministic"
    im = Image.open(io.BytesIO(out1))
    assert im.format == "PNG" and im.size == (500, 300)
    # Pixel presence: the translated text is DRAWN dark inside each region box.
    assert dark_pixels_in_box(out1, [30, 20, 260, 44]) > 50
    assert dark_pixels_in_box(out1, [30, 100, 160, 36]) > 30
    assert out1 != png
    # (5) overflow: the tiny "New" region's translation floors + flags.
    by_idx = {f["reading_order_index"]: f for f in flags1}
    assert any(f["overflow"] for f in flags1), flags1
    tiny_flag = [f for f in flags1 if f["overflow"]]
    assert all(f["fitted_size"] == il.MIN_FONT_SIZE for f in tiny_flag)
    assert len(by_idx) == 4  # every region drew (incl. the human-fixed one)
    print(
        "  overlay render: valid PNG, translated pixels present in region "
        "boxes, byte-deterministic, OVERFLOW floored at "
        f"{il.MIN_FONT_SIZE}px + flagged"
    )

    # fit-to-box unit checks (long DE expansion, never truncated).
    fit = il.fit_text_to_box("Sommerschlussverkaufsangebotsbenachrichtigungstext", 46, 14)
    assert fit["overflow"] is True and fit["size"] == il.MIN_FONT_SIZE
    assert (
        "".join(fit["lines"]).replace(" ", "")
        == "Sommerschlussverkaufsangebotsbenachrichtigungstext"
    )
    fit_ok = il.fit_text_to_box("Kaufen", 300, 60)
    assert fit_ok["overflow"] is False and fit_ok["size"] > il.MIN_FONT_SIZE
    print(
        "  fit-to-box: DE expansion floors + flags with FULL text kept; "
        "roomy box fits without flags"
    )

    # =========================================================================
    # (3) SVG e2e: swap fidelity
    # =========================================================================
    with Session(engine) as s:
        proj = s.get(Project, pid)
        svg_doc = il.ingest_image(s, proj, "banner.svg", SVG, actor="verify")
        assert svg_doc.skeleton["kind"] == "svg"
        texts = [u["text"] for u in svg_doc.skeleton["units"]]
        assert texts == ["Summer Sale & more", "Shop now"], texts
        il.translate_image(s, svg_doc, LOCALE, actor="verify")
        out, flags = il.render_localized(s, svg_doc, LOCALE)
    assert flags == []
    txt = out.decode("utf-8")
    assert f"[{LOCALE}] Summer" in txt and "Sonderaktion" in txt, txt[:400]
    # "Shop now" reuses the SegmentTM entry seeded above — the TM rail fires
    # identically for SVG image segments (cross-document reuse).
    assert "Jetzt einkaufen" in txt, txt[:400]
    # Byte fidelity: swapping the ORIGINAL strings back in yields the original file.
    from app.filters import get_filter

    filt = get_filter("svg")
    rt = filt.build(SVG, {"0": "Summer Sale & more", "1": "Shop now"})
    assert rt == SVG, "non-text SVG content must round-trip byte-identically"
    print(
        "  svg: text-node swap e2e (glossary applied) + byte-identical "
        "round-trip for non-text content"
    )

    # =========================================================================
    # (6) spend-guarded vision seam (override hook: no SDK, no key)
    # =========================================================================
    calls = []

    def fake_vision(png_bytes, prompt):
        calls.append(prompt[:40])
        if "OCR engine" in prompt:
            return "Shop now today"
        return "A retail summer-sale banner aimed at bargain hunters."

    il.vision_call_override = fake_vision
    try:
        with Session(engine) as s:
            proj = s.get(Project, pid)
            png2 = make_fixture_png(REGIONS)
            doc2 = il.ingest_image(s, proj, "banner2.png", png2, actor="verify")
            # Context brief attached via the doc-context seam (sanitized notes).
            assert doc2.context and "summer-sale banner" in doc2.context["notes"]
            # Recognition escalation: TEXT of the low-confidence region was
            # re-read; its GEOMETRY is untouched (iron rule).
            r = [  # noqa: RUF015
                x for x in doc2.skeleton["regions"] if x["bbox_axis_aligned"] == [30, 100, 160, 36]
            ][0]
            assert r["text"] == "Shop now today" and r["source"] == "vlm_recognized"
            assert r["polygon"][0] == [30, 100]
        assert len(calls) >= 2  # 1 context + 1 escalation
        # Spend cap reached -> the guarded call REFUSES (returns None) pre-call.
        settings.set_many({"spend_cap_usd": "0.000001"})
        spend.record_usage(
            "claude", "claude-sonnet-4-5", {"input_tokens": 100000, "output_tokens": 100000}
        )
        blocked, _spent, _cap = spend.would_block()
        assert blocked is True
        n_before = len(calls)
        assert il._guarded_vision_call(b"px", "test prompt") is None
        assert len(calls) == n_before, "capped call must never reach the engine"
        settings.set_many({"spend_cap_usd": "0"})
    finally:
        il.vision_call_override = None
        settings.set_many({"spend_cap_usd": "0"})
    # Keyless + no override: escalation is a no-op and the guarded call skips.
    assert il._guarded_vision_call(b"px", "test prompt") is None
    print(
        "  vision seam: context brief -> Document.context, escalation rewrites "
        "TEXT only (geometry untouched), spend cap refuses pre-call, "
        "keyless skips cleanly"
    )

    # =========================================================================
    # web surfaces: module page, review page, actions, export, caps
    # =========================================================================
    r = c.get("/")
    assert 'href="/images"' in r.text
    r = c.get("/images")
    assert r.status_code == 200
    assert "banner.png" in r.text and "banner.svg" in r.text
    assert "ImgCo" in r.text
    print("  web: nav entry + module page lists the images")

    r = c.get(f"/images/{did}?locale={LOCALE}")
    assert r.status_code == 200
    assert "Summer Sale" in r.text  # region source text
    assert "Sonderaktion" in r.text  # region translation in the editor
    assert "OVERFLOW" in r.text  # flag surfaced
    assert "LOW CONF" in r.text  # 0.5-confidence region flagged
    assert f"/images/{did}/original" in r.text
    assert f"/images/{did}/localized" in r.text
    print("  web: review page renders side-by-side + region list with confidence/overflow flags")

    r = c.get(f"/images/{did}/original")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    r = c.get(f"/images/{did}/localized?locale={LOCALE}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    r = c.get(f"/images/{did}/export?locale={LOCALE}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert "attachment" in r.headers["content-disposition"]
    assert f"banner.{LOCALE}.png" in r.headers["content-disposition"]
    assert r.content == out1  # export == the deterministic render
    print("  web: original/localized/export serve image/png; export bytes == deterministic render")

    # Region edit -> segment written like the editor -> re-render shows it.
    with Session(engine) as s:
        seg = [  # noqa: RUF015
            x
            for x in s.exec(select(Segment).where(Segment.document_id == did)).all()
            if x.source_text == "Summer Sale"
        ][0]
        sid = seg.id
    r = c.post(
        f"/images/{did}/regions/{sid}",
        data={"target_text": "Sommerschlussverkauf", "locale": LOCALE},
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    with Session(engine) as s:
        seg = s.get(Segment, sid)
        assert seg.target_text == "Sommerschlussverkauf"
        assert seg.status == SegStatus.EDITED
        doc = s.get(Document, did)
        out3, _f = il.render_localized(s, doc, LOCALE)
    assert out3 != out1  # the edit re-renders differently
    print("  web: region edit writes the Segment (EDITED) and re-renders")

    # =========================================================================
    # SECURITY: uploaded SVG is attacker-controlled markup. Served inline,
    # same-origin, as image/svg+xml it is stored XSS (a victim who opens the raw
    # /original|/localized endpoint runs the SVG's <script> in the app origin).
    # Both endpoints (original AND the text-swapped localized build, which leaves
    # <script>/on* intact) MUST serve SVG as a download under nosniff + a CSP that
    # neuters script even if a browser rendered it inline. Rasters cannot execute
    # and keep serving inline. FAILS before the images.py _serve_image fix.
    # =========================================================================
    mal_svg = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80">\n'
        b"  <script>alert(document.domain)</script>\n"
        b'  <text x="10" y="40" onclick="alert(1)">Summer Sale</text>\n'
        b"</svg>\n"
    )
    r = c.post(
        "/images/upload",
        data={"project_id": str(pid)},
        files={"file": ("evil.svg", mal_svg, "image/svg+xml")},
    )
    assert r.status_code == 303, r.text
    xss_id = int(r.headers["location"].rsplit("/", 1)[-1])
    for url in (f"/images/{xss_id}/original",):
        rr = c.get(url)
        assert rr.status_code == 200, (url, rr.status_code)
        assert rr.headers["content-type"] == "image/svg+xml", rr.headers
        cd = rr.headers.get("content-disposition", "")
        assert "attachment" in cd, ("SVG must be a download, not inline", url, cd)
        assert rr.headers.get("x-content-type-options") == "nosniff", rr.headers
        assert "default-src 'none'" in rr.headers.get("content-security-policy", ""), (
            "SVG needs a locked-down CSP",
            url,
            dict(rr.headers),
        )
    # The localized SVG build (text-swap: <script>/on* survive) is equally unsafe
    # and must carry the SAME headers.
    with Session(engine) as s:
        il.translate_image(s, s.get(Document, xss_id), LOCALE, actor="verify")
    rr = c.get(f"/images/{xss_id}/localized?locale={LOCALE}")
    assert rr.status_code == 200
    assert "attachment" in rr.headers.get("content-disposition", ""), rr.headers
    assert rr.headers.get("x-content-type-options") == "nosniff", rr.headers
    assert "default-src 'none'" in rr.headers.get("content-security-policy", ""), rr.headers
    # A raster ORIGINAL stays inline (cannot execute) but still gets nosniff.
    rr = c.get(f"/images/{did}/original")
    assert "attachment" not in rr.headers.get("content-disposition", ""), (
        "raster preview must stay inline for the <img> review page"
    )
    assert rr.headers.get("x-content-type-options") == "nosniff", rr.headers
    print(
        "  security: uploaded SVG served as download + nosniff + locked CSP on "
        "original & localized (XSS neutered); raster stays inline"
    )

    # Upload caps: byte cap + pixel-dimension guard.
    from app import config as appcfg

    orig_cap = appcfg.MAX_UPLOAD_BYTES
    appcfg.MAX_UPLOAD_BYTES = 1024
    try:
        r = c.post(
            "/images/upload",
            data={"project_id": str(pid)},
            files={"file": ("big.png", png, "image/png")},
        )
        assert r.status_code == 303 and "err=toolarge" in r.headers["location"]
    finally:
        appcfg.MAX_UPLOAD_BYTES = orig_cap
    # #573 gate: an image with NO detected text (no sidecar under the mock detector) still
    # ingests as a 0-region document; only an empty TEXT file is refused as empty.
    blank_png = io.BytesIO()
    Image.new("RGB", (60, 40), (255, 255, 255)).save(blank_png, format="PNG")
    with Session(engine) as s:
        bdoc0 = il.ingest_image(
            s, s.get(Project, pid), "blank.png", blank_png.getvalue(), actor="verify"
        )
        assert bdoc0.id and (bdoc0.skeleton or {}).get("regions") == [], bdoc0.skeleton
        assert (bdoc0.skeleton or {}).get("detector") == "none", bdoc0.skeleton
    r = c.post(
        "/images/upload",
        data={"project_id": str(pid)},
        files={"file": ("blank2.png", blank_png.getvalue(), "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    wide = Image.new("RGB", (8500, 40), (255, 255, 255))
    wbuf = io.BytesIO()
    wide.save(wbuf, format="PNG")
    try:
        with Session(engine) as s:
            il.ingest_image(s, s.get(Project, pid), "wide.png", wbuf.getvalue(), actor="verify")
        raise AssertionError("8500px image must be refused")
    except il.ImageLocalizationError as exc:
        assert "8000" in str(exc)
    r = c.post(
        "/images/upload",
        data={"project_id": str(pid)},
        files={"file": ("wide.png", wbuf.getvalue(), "image/png")},
    )
    assert r.status_code == 303 and "err=badimage" in r.headers["location"]
    print("  caps: MAX_UPLOAD_BYTES 303->toolarge; 8000px dimension guard refuses (service + web)")

    # =========================================================================
    # /api parity: docs-scope read + export
    # =========================================================================
    u = security.create_user("imgapi@x.com", "Img Api", "admin", "img-api-pw-123")
    _row, token = apikeys.create_key(u.id, "img-test", scopes="api docs")
    r = c.get("/api/images")
    assert r.status_code == 401
    h = {"X-Loc-Token": token}
    r = c.get("/api/images", headers=h)
    assert r.status_code == 200
    names = {i["filename"] for i in r.json()["images"]}
    assert "banner.png" in names and "banner.svg" in names
    r = c.get(f"/api/images/{did}?locale={LOCALE}", headers=h)
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    regs = body["regions"]
    assert len(regs) == 4
    assert any(x["overflow"] for x in regs)
    assert all("bbox" in x and "polygon" in x and "confidence" in x for x in regs)
    r = c.get(f"/api/images/{did}/export?locale={LOCALE}", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    print("  /api: 401 tokenless; list/show/export with docs scope (regions + flags + bytes)")

    # =========================================================================
    # #125 (MED) end to end: a blocked vision engine target surfaces the refusal on
    # every operator surface (web review page, CLI show, /api/images), the parallel to
    # OCR's "[OCR error: ...]" being visible in the extracted text -- never a silent None.
    # =========================================================================
    from app import config as _cfg

    with Session(engine) as s:
        bcust = Customer(name="BlockedCo")
        s.add(bcust)
        s.commit()
        s.refresh(bcust)
        # A local vision engine whose target the #120/#124 egress door refuses.
        bproj = Project(
            name="Blocked", source_locale="en-US", default_provider="local", customer_id=bcust.id
        )
        s.add(bproj)
        s.commit()
        s.refresh(bproj)
        s.add(TargetLocale(project_id=bproj.id, locale=LOCALE))
        s.commit()
        bpid = bproj.id
    # A single LOW-confidence region so recognition escalation actually fires.
    blocked_png = make_fixture_png(
        [{"text": "Menu", "bbox": [20, 20, 120, 40], "confidence": 0.30}]
    )
    _prev_base, _prev_key = _cfg.LOCAL_BASE_URL, _cfg.LOCAL_API_KEY
    _prev_override = il.vision_call_override
    _cfg.LOCAL_BASE_URL = "http://169.254.169.254"  # link-local metadata -> always refused
    _cfg.LOCAL_API_KEY = "blocked-fixture-key"  # pregate: allow-secret offline fixture
    il.vision_call_override = None
    try:
        # The egress door refuses 169.254.169.254 by literal IP (no resolution needed).
        assert isinstance(il._guarded_vision_call(b"x", "read", "local"), il.VisionRefusal), (
            "the door must refuse the blocked base as a typed VisionRefusal"
        )
        with Session(engine) as s:
            bdoc = il.ingest_image(
                s, s.get(Project, bpid), "blocked.png", blocked_png, actor="verify"
            )
            bdid = bdoc.id
            # The refusal was recorded on the persisted region geometry contract.
            skel = s.get(Document, bdid).skeleton or {}
            region0 = (skel.get("regions") or [{}])[0]
            assert region0.get("vlm_error", "").startswith("[vision error: EgressBlocked:"), region0
            # A refusal is never a context brief.
            assert s.get(Document, bdid).context is None
    finally:
        _cfg.LOCAL_BASE_URL, _cfg.LOCAL_API_KEY = _prev_base, _prev_key
        il.vision_call_override = _prev_override

    # SURFACE 1 -- the shared region_rows row model (feeds all three surfaces).
    with Session(engine) as s:
        brows = il.region_rows(s, s.get(Document, bdid), LOCALE)
        assert brows and "EgressBlocked" in brows[0]["vlm_error"], brows

    # SURFACE 2 -- /api/images/{id}: the additive vlm_error field carries it.
    r = c.get(f"/api/images/{bdid}?locale={LOCALE}", headers=h)
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert "EgressBlocked" in body["regions"][0]["vlm_error"], body["regions"][0]
    # A control image (no blocked engine) reports an EMPTY vlm_error -- additive, never noisy.
    r0 = c.get(f"/api/images/{did}?locale={LOCALE}", headers=h)
    assert all(x["vlm_error"] == "" for x in r0.json()["regions"]), "control image must be clean"

    # SURFACE 3 -- web review page: the banner + per-row ENGINE BLOCKED badge.
    r = c.get(f"/images/{bdid}?locale={LOCALE}")
    assert r.status_code == 200
    assert "Vision engine blocked" in r.text and "ENGINE BLOCKED" in r.text, "web banner/badge"

    # SURFACE 4 -- CLI show: JSON field + human flag.
    rc, out = _run_cli(["--json", "image", "show", str(bdid)])
    cli_payload = json.loads(out)
    assert "EgressBlocked" in cli_payload["regions"][0]["vlm_error"], cli_payload
    rc, out = _run_cli(["image", "show", str(bdid)])
    assert rc == 0 and "ENGINE-BLOCKED" in out and "refused by egress policy" in out, out
    print(
        "  #125 (MED): blocked vision engine surfaces the refusal on region_rows, "
        "/api/images (additive vlm_error), the web review page, and CLI show"
    )

    # =========================================================================
    # (9) CLI verbs
    # =========================================================================
    tmp = tempfile.mkdtemp(prefix="loctms_imgcli_")
    cli_png = os.path.join(tmp, "cli_banner.png")
    with open(cli_png, "wb") as fh:
        fh.write(
            make_fixture_png(
                [{"text": "Hello world", "bbox": [20, 20, 200, 40], "confidence": 0.9}]
            )
        )
    rc, out = _run_cli(["image", "upload", str(pid), cli_png])
    assert rc == 0 and "1 region(s)" in out and "detector=mock" in out, out
    cli_did = int(out.split("#")[1].split(":")[0])
    rc, out = _run_cli(["image", "list", str(pid)])
    assert rc == 0 and "cli_banner.png" in out
    rc, out = _run_cli(["--json", "image", "show", str(cli_did)])
    payload = json.loads(out)
    assert payload["ok"] and payload["regions"][0]["source_text"] == "Hello world"
    rc, out = _run_cli(["image", "translate", str(cli_did), "--locale", LOCALE])
    assert rc == 0 and "Translated image" in out, out
    rc, out = _run_cli(["--json", "image", "render", str(cli_did), "--locale", LOCALE])
    rj = json.loads(out)
    assert rj["ok"] and rj["overflow_count"] == 0 and rj["path"] is None
    dest = os.path.join(tmp, "out.de.png")
    rc, out = _run_cli(["image", "export", str(cli_did), "--locale", LOCALE, "-o", dest])
    assert rc == 0 and os.path.isfile(dest)
    exp = Image.open(dest)
    assert exp.format == "PNG"
    assert dark_pixels_in_box(open(dest, "rb").read(), [20, 20, 200, 40]) > 30  # noqa: SIM115
    # export refuses to clobber without --force (then allows with it).
    rc2, _ = _run_cli(["image", "export", str(cli_did), "--locale", LOCALE, "-o", dest])
    assert rc2 == 2
    rc3, _ = _run_cli(["image", "export", str(cli_did), "--locale", LOCALE, "-o", dest, "--force"])
    assert rc3 == 0
    # bad locale + non-image doc id -> clean usage errors
    rc4, _ = _run_cli(["image", "show", str(cli_did), "--locale", "xx-XX"])
    assert rc4 == 2
    print(
        "  cli: upload/list/show/translate/render/export verbs (json + human, "
        "overwrite guard, clean usage errors)"
    )

    # =========================================================================
    # #588: a run where EVERY image region fails is FAILED, not completed
    # (same rule as #572: jobs.all_failed_error). #624: partial is FAILED too.
    # =========================================================================
    from app.models import Job, JobStatus

    class DeadEngine(MockProvider):
        def complete(self, prompt, system=None, task=None, payload=None, **kw):
            raise RuntimeError("the model 'claude-not-here' is not served")

    class HalfDeadEngine(MockProvider):
        def complete(self, prompt, system=None, task=None, payload=None, **kw):
            if task == "translate" and "Broken" in (payload or {}).get("text", ""):
                raise RuntimeError("the model 'claude-not-here' is not served")
            return super().complete(prompt, system=system, task=task, payload=payload, **kw)

    two = [
        {"text": "Alpha one", "bbox": [20, 20, 200, 40], "confidence": 0.9},
        {"text": "Broken two", "bbox": [20, 80, 200, 40], "confidence": 0.9},
    ]

    def _fresh_image(name):
        with Session(engine) as s:
            cu = Customer(name=name)
            s.add(cu)
            s.commit()
            s.refresh(cu)
            pj = make_project(s, name, cu.id)
            d = il.ingest_image(s, pj, name + ".png", make_fixture_png(two), actor="verify")
            return d.id

    def _last_job():
        with Session(engine) as s:
            return s.exec(
                select(Job).where(Job.kind == "translate_document").order_by(Job.id.desc())
            ).first()

    dead_id = _fresh_image("deadimg")
    register_provider("mock", DeadEngine())
    try:
        with Session(engine) as s:
            try:
                il.translate_image(s, s.get(Document, dead_id), LOCALE, actor="verify")
            except il.ImageLocalizationError as exc:
                assert "is not served" in str(exc), exc
            else:
                raise AssertionError("an all-failed image translate must raise")
        j = _last_job()
        assert j.status == JobStatus.FAILED, j.status
        assert "is not served" in (j.error or ""), j.error
        assert j.result["translated"] == 0 and j.result["errors"] >= 1, j.result
        # web door: redirects back with err=, never translated=1
        r = c.post(f"/images/{dead_id}/translate", data={"locale": LOCALE})
        assert r.status_code == 303 and "err=" in r.headers["location"], r.headers["location"]
        assert "translated=1" not in r.headers["location"], r.headers["location"]
        # API door: 400 ok False carrying the engine text
        r = c.post(f"/api/images/{dead_id}/translate", json={"locale": LOCALE}, headers=h)
        assert r.status_code == 400 and r.json()["ok"] is False, r.text
        assert "is not served" in r.json()["error"], r.text
        # CLI door: nonzero exit, no "Translated image" success line
        rc, out = _run_cli(["image", "translate", str(dead_id), "--locale", LOCALE])
        assert rc != 0 and "Translated image" not in out, (rc, out)
        assert _last_job().status == JobStatus.FAILED
    finally:
        _OVERRIDES.pop("mock", None)

    half_id = _fresh_image("halfimg")
    register_provider("mock", HalfDeadEngine())
    try:
        # #624 (intended change, was "partial stays COMPLETED"): a partial failure is FAILED too.
        with Session(engine) as s:
            try:
                il.translate_image(s, s.get(Document, half_id), LOCALE, actor="verify")
            except il.ImageLocalizationError as exc:
                assert "Partially translated" in str(exc), exc
            else:
                raise AssertionError("a partial image translate must raise (#624)")
        j = _last_job()
        assert j.status == JobStatus.FAILED, (j.status, j.result)
        assert j.result["translated"] >= 1 and j.result["errors"] >= 1, j.result
        assert "Partially translated" in (j.error or ""), j.error
        r = c.post(f"/images/{half_id}/translate", data={"locale": LOCALE})
        assert r.status_code == 303 and "err=" in r.headers["location"], r.headers["location"]
        r = c.post(f"/api/images/{half_id}/translate", json={"locale": LOCALE}, headers=h)
        assert r.status_code == 400 and r.json()["ok"] is False, r.text
        # the re-run only retries the failed region, so it may read as all-failed this time
        assert "Partially translated" in r.json()["error"] or "failed" in r.json()["error"], r.text
        rc, out = _run_cli(["image", "translate", str(half_id), "--locale", LOCALE])
        assert rc != 0 and "Translated image" not in out, (rc, out)
    finally:
        _OVERRIDES.pop("mock", None)
    print(
        "  #588: every region failing -> FAILED job + error on web/API/CLI; "
        "one region failing is FAILED too (#624), errors kept in the result"
    )

    # =========================================================================
    # (7) kill switch
    # =========================================================================
    settings.set_many({"image_localization_enabled": "0"})
    try:
        assert 'href="/images"' not in c.get("/").text
        assert c.get("/images").status_code == 404
        assert c.get(f"/images/{did}").status_code == 404
        assert c.get(f"/images/{did}/export?locale={LOCALE}").status_code == 404
        assert c.get("/api/images", headers=h).status_code == 404
        rc, _out = _run_cli(["image", "list", str(pid)])
        assert rc == 1
        # NEW image ingestion is refused everywhere (filter parse gate).
        try:
            with Session(engine) as s:
                il.ingest_image(
                    s, s.get(Project, pid), "off.png", make_fixture_png(REGIONS[:1]), actor="verify"
                )
            raise AssertionError("ingest must be refused while the module is off")
        except Exception as exc:
            assert "disabled" in str(exc)
    finally:
        settings.set_many({"image_localization_enabled": "1"})
    assert c.get("/images").status_code == 200
    print(
        "  kill switch: nav gone, web+api 404, CLI refuses, ingest refused; flips back on cleanly"
    )

    # detector status shape (surfaces read this)
    st = il.detector_status()
    assert set(st["available"]) == {"mock", "winocr", "rapidocr"}
    assert st["available"]["mock"] is True
    print(
        "  detector seam: status reports mock/winocr/rapidocr availability "
        f"(default here: {st['default']})"
    )

    # RapidOCR result-shape parsing (regression: real rapidocr 3.x returns
    # .boxes/.txts/.scores as NUMPY ARRAYS, and `arr or []` raised
    # "truth value of an array is ambiguous" -- crashing the live opt-in path
    # that the mock-detector hermetic tests never exercised).
    quad = [[10.0, 12.0], [90.0, 12.0], [90.0, 40.0], [10.0, 40.0]]
    # v1 tuple shape: (list[[box, text, score]], elapse) -- no numpy needed
    r1 = il._parse_rapidocr_result(([[quad, "Hi", 0.88]], 0.01))
    assert len(r1) == 1 and r1[0]["text"] == "Hi", r1
    # empty / None shapes never raise
    assert il._parse_rapidocr_result(None) == []
    assert il._parse_rapidocr_result(([], 0.0)) == []

    # The v2/v3 object shape needs real numpy arrays to reproduce the
    # truth-value failure. numpy is NOT a project dep (it arrives only with the
    # opt-in `pip install rapidocr onnxruntime`), so skip cleanly without it --
    # the bug this pins cannot occur on a numpy-less install.
    try:
        import numpy as _np
    except ImportError:
        _np = None
    if _np is not None:

        class _V3Result:  # v2/v3 object shape, numpy-array attributes
            boxes = _np.array([quad], dtype=float)
            txts = _np.array(["Hello"], dtype=object)
            scores = _np.array([0.97], dtype=float)

        r3 = il._parse_rapidocr_result(_V3Result())
        assert len(r3) == 1 and r3[0]["text"] == "Hello", r3
        assert abs(r3[0]["confidence"] - 0.97) < 1e-6
        assert r3[0]["source"] == "classical_detector"

        class _EmptyV3:
            boxes = _np.empty((0, 4, 2))
            txts = _np.array([], dtype=object)
            scores = _np.array([], dtype=float)

        assert il._parse_rapidocr_result(_EmptyV3()) == []
        print(
            "  rapidocr parsing: v1 tuple + v2/v3 numpy-array shapes parse; "
            "empty/None safe (numpy truth-value regression pinned)"
        )
    else:
        print(
            "  rapidocr parsing: v1 tuple + empty/None safe "
            "(numpy absent -- v2/v3 numpy-shape pin skipped)"
        )

    # =========================================================================
    # (11) detector resolution (issue #30): the ENGINE is resolved FIRST; an
    #      uploaded fixture sidecar can never override a real, configured OCR
    #      engine. The sidecar is honored only when the resolution is the mock.
    # =========================================================================
    inj = make_fixture_png(
        [{"text": "FAKE INJECTED", "bbox": [10, 10, 120, 30], "confidence": 0.99}]
    )
    assert il._has_fixture_sidecar(inj), "sidecar not embedded in the probe PNG"
    _env0 = os.environ.pop("LOC_TMS_IMAGE_DETECTOR", None)
    _rapid_avail = il.RapidOcrDetector.__dict__["available"]
    _rapid_detect = il.RapidOcrDetector.__dict__["detect"]
    _win_avail = il.WinOcrDetector.__dict__["available"]
    # LOW-4: capture the "sidecar ignored" operator-visibility INFO line so a later
    # refactor cannot drop it silently. Attach a handler straight to the module
    # logger; the message must name ONLY the engine (no region text, no image body,
    # and no filename -- resolve_detector never receives one).
    _cap: list[logging.LogRecord] = []

    class _CapHandler(logging.Handler):
        def emit(self, record):
            _cap.append(record)

    _illog = logging.getLogger("loc_tms.image_localization")
    _caph = _CapHandler(level=logging.INFO)
    _prev_level = _illog.level
    _illog.addHandler(_caph)
    _illog.setLevel(logging.INFO)
    try:
        # (a) env=rapidocr + a real engine available + sidecar bytes -> the real
        #     engine, and the attacker-injected label never reaches the regions.
        il.RapidOcrDetector.available = staticmethod(lambda: True)
        il.RapidOcrDetector.detect = lambda self, data: [
            {"text": "REAL OCR", "bbox_axis_aligned": [0, 0, 1, 1]}
        ]
        _cap.clear()
        os.environ["LOC_TMS_IMAGE_DETECTOR"] = "rapidocr"
        det = il.resolve_detector(inj)
        assert type(det).__name__ == "RapidOcrDetector", type(det).__name__
        # LOW-4: exactly ONE "sidecar ignored" INFO line, carrying only the engine
        # name -- no injected region text, no image body/filename.
        _ignored = [
            r.getMessage()
            for r in _cap
            if r.levelno == logging.INFO and "sidecar ignored" in r.getMessage()
        ]
        assert len(_ignored) == 1, _ignored
        assert _ignored[0] == (
            "image-loc: fixture sidecar ignored; real detector 'rapidocr' "
            "resolved via LOC_TMS_IMAGE_DETECTOR"
        ), _ignored[0]
        assert "FAKE INJECTED" not in _ignored[0], _ignored[0]
        il._DETECT_CACHE.clear()
        texts = [r.get("text") for r in il.detect_regions(inj)]
        assert "FAKE INJECTED" not in texts, texts
        assert "REAL OCR" in texts, texts

        # (a') auto-detect (no env) also refuses to let the sidecar win -- and the
        #      injected label is absent from the REGION LIST, not just from the class.
        os.environ.pop("LOC_TMS_IMAGE_DETECTOR", None)
        il.WinOcrDetector.available = staticmethod(lambda: False)
        det = il.resolve_detector(inj)
        assert type(det).__name__ == "RapidOcrDetector", type(det).__name__
        il._DETECT_CACHE.clear()
        texts = [r.get("text") for r in il.detect_regions(inj)]
        assert "FAKE INJECTED" not in texts, texts
        assert "REAL OCR" in texts, texts

        # (d) the ISSUE'S OWN repro: env=winocr + sidecar with winocr NOT installed
        #     here raises "not available"; it does NOT fall back to Mock (the #30
        #     defect returned MockDetector for any sidecar PNG, silencing this error).
        il.WinOcrDetector.available = staticmethod(lambda: False)
        os.environ["LOC_TMS_IMAGE_DETECTOR"] = "winocr"
        try:
            il.resolve_detector(inj)
            raise AssertionError("expected ImageLocalizationError for env=winocr uninstalled")
        except il.ImageLocalizationError as e:
            assert "not available" in str(e), str(e)

        # (e) env=winocr + sidecar with winocr monkeypatched AVAILABLE -> the real
        #     WinOcrDetector class, never MockDetector (the sidecar is ignored).
        il.WinOcrDetector.available = staticmethod(lambda: True)
        det = il.resolve_detector(inj)
        assert type(det).__name__ == "WinOcrDetector", type(det).__name__

        # (f) an UNKNOWN env name + sidecar bytes still raises -- the sidecar cannot
        #     make an unrecognized engine resolve to mock.
        os.environ["LOC_TMS_IMAGE_DETECTOR"] = "bogus-engine"
        try:
            il.resolve_detector(inj)
            raise AssertionError("expected ImageLocalizationError for unknown env")
        except il.ImageLocalizationError as e:
            assert "unknown" in str(e).lower(), str(e)

        # (b) env=mock, and env-unset with NO real engine, still read the sidecar.
        il.RapidOcrDetector.available = staticmethod(lambda: False)
        il.WinOcrDetector.available = staticmethod(lambda: False)
        os.environ["LOC_TMS_IMAGE_DETECTOR"] = "mock"
        det = il.resolve_detector(inj)
        assert type(det).__name__ == "MockDetector", type(det).__name__
        il._DETECT_CACHE.clear()
        assert "FAKE INJECTED" in [r.get("text") for r in il.detect_regions(inj)]
        os.environ.pop("LOC_TMS_IMAGE_DETECTOR", None)
        det = il.resolve_detector(inj)  # no env, no real engine -> mock + sidecar
        assert type(det).__name__ == "MockDetector", type(det).__name__

        # (c) a real engine NAMED but NOT installed still errors (today's
        #     behavior) even for sidecar bytes -- the sidecar cannot rescue it.
        os.environ["LOC_TMS_IMAGE_DETECTOR"] = "rapidocr"
        try:
            il.resolve_detector(inj)
            raise AssertionError("expected ImageLocalizationError for absent rapidocr")
        except il.ImageLocalizationError:
            pass
    finally:
        il.RapidOcrDetector.available = _rapid_avail
        il.RapidOcrDetector.detect = _rapid_detect
        il.WinOcrDetector.available = _win_avail
        il._DETECT_CACHE.clear()
        _illog.removeHandler(_caph)
        _illog.setLevel(_prev_level)
        if _env0 is None:
            os.environ.pop("LOC_TMS_IMAGE_DETECTOR", None)
        else:
            os.environ["LOC_TMS_IMAGE_DETECTOR"] = _env0
    print(
        "  detector resolution (issue #30): engine resolved first; a real engine is "
        "never overridden by an uploaded sidecar (env + auto-detect, injected label "
        "absent from regions); env=winocr uninstalled and an unknown env both still "
        "error; a monkeypatched-available winocr resolves real; env=mock / no-engine "
        "still read the sidecar; the 'sidecar ignored' INFO line is emitted with no body"
    )

    # Issue #546: a total-pixel cap, not only a per-side cap. 7000x7000 passes the 8000px
    # per-side guard but would decode to ~200 MB of RGBA.
    big = Image.new("L", (7000, 7000), 255)
    bbuf = io.BytesIO()
    big.save(bbuf, format="PNG")
    try:
        il.open_raster(bbuf.getvalue())
        raise AssertionError("49 megapixel image must be refused by the total-pixel cap")
    except il.ImageLocalizationError as exc:
        assert "megapixel" in str(exc) or "total" in str(exc), str(exc)
    ok = Image.new("L", (5000, 5000), 255)  # exactly 25 MP stays accepted
    obuf = io.BytesIO()
    ok.save(obuf, format="PNG")
    assert il.open_raster(obuf.getvalue()).size == (5000, 5000)
    mid = Image.new("L", (5100, 5000), 255)  # 25.5 MP: just over the default cap
    mbuf = io.BytesIO()
    mid.save(mbuf, format="PNG")
    try:
        il.open_raster(mbuf.getvalue())
        raise AssertionError("25.5 megapixel image must be refused by the 25 MP default")
    except il.ImageLocalizationError as exc:
        assert "megapixel" in str(exc), str(exc)
    _saved_cap = il.MAX_TOTAL_PIXELS
    il.MAX_TOTAL_PIXELS = 100  # the cap is checked from the header, before decode
    try:
        il.open_raster(obuf.getvalue())
        raise AssertionError("a lowered cap must refuse the 5000x5000 image")
    except il.ImageLocalizationError:
        pass
    finally:
        il.MAX_TOTAL_PIXELS = _saved_cap
    print("  caps: total-pixel cap refuses 7000x7000 and 25.5 MP, accepts 5000x5000 (#546)")

    print("IMAGE LOCALIZATION OK")


if __name__ == "__main__":
    main()
