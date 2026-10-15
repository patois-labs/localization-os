"""Raster aliases .tif/.tiff/.bmp/.gif/.webp on the image filter (file-format Batch 16).

Specs checked: TIFF 6.0 header (II*\\0 / MM\\0*), BMP "BM" file header, GIF87a/GIF89a,
WebP RIFF container (RFC 9649), Pillow's image file format docs (formats= pinning,
is_animated, MAX_IMAGE_PIXELS). Every image is generated at test runtime.

  registry     every alias resolves to the image filter; IMAGE_EXTS and the MIME map know them
  sniff        magic bytes decide the kind, never the extension
  overlay      compose_overlay draws on each format and writes the SAME format back,
               byte-deterministically; alpha survives for RGBA TIFF/WebP and GIF transparency
  frames       an animated GIF, an animated WebP and a multi-page TIFF are refused with a
               plain message (ImageRefusedError, an InputLimitError), at the filter, at
               ingest_image and at the generic document upload seam, and nothing is stored
  hostile      a header claiming 30000x30000 pixels is refused before decode; bytes whose
               magic names one plugin cannot be decoded by another; truncated data refused
  pipeline     a still TIFF/BMP/GIF/WebP ingests as an image document (mock detector, zero
               regions) and render_localized returns the source format

Hermetic + offline. Final line: "IMAGE RASTER ALIASES OK".
"""

import io
import os
import struct
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_imgalias_")
os.environ["LOC_TMS_IMAGE_DETECTOR"] = "mock"

from PIL import Image
from sqlmodel import Session, select

from app.db import engine, init_db
from app.filters import get_filter, supported_exts
from app.filters.base import InputLimitError
from app.models import Customer, Document, Project, TargetLocale, TransMode
from app.services import image_localization as il
from app.services.ingest import ingest_document
from app.services.mime import mime_for_fmt

ALIASES = {"tif": "tiff", "tiff": "tiff", "bmp": "bmp", "gif": "gif", "webp": "webp"}
PIL_NAME = {"tiff": "TIFF", "bmp": "BMP", "gif": "GIF", "webp": "WEBP"}


def still(fmt: str, mode: str = "RGB", size=(120, 60)) -> bytes:
    color = (255, 255, 255, 255) if mode == "RGBA" else (255, 255, 255)
    im = Image.new(mode, size, color)
    if mode == "RGBA":
        for x in range(10):
            im.putpixel((x, 0), (0, 0, 0, 0))  # a transparent corner to keep
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    return buf.getvalue()


def multi(fmt: str) -> bytes:
    frames = [Image.new("RGB", (40, 30), c) for c in ((255, 0, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, format=fmt, save_all=True, append_images=frames[1:])
    return buf.getvalue()


def region():
    return il.normalize_region(0, text="Sale", bbox=[10, 10, 90, 30], source="mock")


def refused(fn, exc=InputLimitError, needle=""):
    try:
        fn()
    except exc as e:
        assert needle in str(e), (needle, str(e))
        return str(e)
    raise AssertionError(f"expected {exc.__name__}")


# --- registry ---------------------------------------------------------------------------
img = get_filter("png")
for ext in ALIASES:
    assert get_filter(ext) is img, ext
    assert ext in supported_exts(), ext
    assert ext in il.IMAGE_EXTS and il.is_image_fmt(ext), ext
    assert mime_for_fmt(ext).startswith("image/"), ext
assert mime_for_fmt("tif") == "image/tiff" and mime_for_fmt("webp") == "image/webp"
print("registry ok")

# --- sniff ------------------------------------------------------------------------------
for kind, fmt in PIL_NAME.items():
    assert il.sniff_kind(still(fmt)) == kind, kind
be_tiff = io.BytesIO()
Image.new("RGB", (8, 8)).save(be_tiff, format="TIFF")
assert il.sniff_kind(b"MM\x00*" + be_tiff.getvalue()[4:]) == "tiff"
assert il.sniff_kind(b"BM") is None  # too short to be a BMP header
assert il.sniff_kind(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None  # RIFF but not WebP
assert il.sniff_kind(still("PNG")) == "png" and il.sniff_kind(still("JPEG")) == "jpeg"
print("sniff ok")

# --- overlay per format: same format back, deterministic -----------------------------------
for kind, fmt in PIL_NAME.items():
    src = still(fmt)
    out1, flags = il.compose_overlay(src, [region()], {"0": "Oferta"})
    out2, _ = il.compose_overlay(src, [region()], {"0": "Oferta"})
    assert out1 == out2, f"{kind} overlay is not deterministic"
    assert il.sniff_kind(out1) == kind, (kind, il.sniff_kind(out1))
    assert len(flags) == 1 and flags[0]["reading_order_index"] == 0
    back = Image.open(io.BytesIO(out1))
    assert back.size == (120, 60)
    # the region was erased and drawn on: some dark pixels inside it
    rgb = back.convert("RGB")
    dark = sum(1 for x in range(10, 100) for y in range(10, 40) if sum(rgb.getpixel((x, y))) < 300)
    assert dark > 0, f"{kind}: no text drawn"
    # untouched text -> no flags, still the source format
    out3, flags3 = il.compose_overlay(src, [region()], {})
    assert flags3 == [] and il.sniff_kind(out3) == kind

for fmt in ("TIFF", "WEBP"):
    out, _ = il.compose_overlay(still(fmt, "RGBA"), [region()], {"0": "Oferta"})
    back = Image.open(io.BytesIO(out))
    assert back.mode == "RGBA", (fmt, back.mode)
    assert back.getpixel((0, 0))[3] == 0, f"{fmt}: transparency lost"
gif_t = io.BytesIO()
Image.new("RGBA", (120, 60), (0, 0, 0, 0)).save(gif_t, format="GIF")
out, _ = il.compose_overlay(gif_t.getvalue(), [region()], {"0": "Oferta"})
assert "transparency" in Image.open(io.BytesIO(out)).info, "GIF transparency lost"
# TIFF keeps a lossless source compression
tiff_def = io.BytesIO()
Image.new("RGB", (120, 60), "white").save(tiff_def, format="TIFF", compression="tiff_adobe_deflate")
out, _ = il.compose_overlay(tiff_def.getvalue(), [region()], {"0": "Oferta"})
assert Image.open(io.BytesIO(out)).info.get("compression") == "tiff_adobe_deflate"
print("overlay ok")

# --- multi-frame refusal --------------------------------------------------------------------
msgs = {
    "GIF": refused(lambda: il.open_raster(multi("GIF")), il.ImageRefusedError, "GIF is animated"),
    "WEBP": refused(
        lambda: il.open_raster(multi("WEBP")), il.ImageRefusedError, "WEBP is animated"
    ),
    "TIFF": refused(
        lambda: il.open_raster(multi("TIFF")), il.ImageRefusedError, "more than one page"
    ),
}
for m in msgs.values():
    assert "single-frame" in m and m.isascii() and "—" not in m
refused(lambda: img.parse(multi("GIF")), InputLimitError, "animated")
refused(
    lambda: il.compose_overlay(multi("GIF"), [region()], {"0": "x"}), InputLimitError, "animated"
)
# a one-frame GIF saved with save_all is still accepted
one = io.BytesIO()
Image.new("RGB", (30, 30), "white").save(one, format="GIF", save_all=True)
il.open_raster(one.getvalue())
print("frames ok")

# --- hostile --------------------------------------------------------------------------------
bmp = bytearray(still("BMP"))
struct.pack_into("<ii", bmp, 18, 30000, 30000)  # BITMAPINFOHEADER width/height
refused(lambda: il.open_raster(bytes(bmp)), il.ImageLocalizationError, "exceed")
bmp_wide = bytearray(still("BMP"))
struct.pack_into("<ii", bmp_wide, 18, 6000, 6000)  # under the side guard, over 25 MP
refused(lambda: il.open_raster(bytes(bmp_wide)), il.ImageLocalizationError, "megapixels")
# "BM" magic in front of PNG bytes: the BMP plugin alone may decode it, and cannot
fake = b"BM" + still("PNG")
refused(lambda: il.open_raster(fake), il.ImageLocalizationError, "not a decodable image")
refused(
    lambda: il.open_raster(still("GIF")[:20]), il.ImageLocalizationError, "not a decodable image"
)
refused(
    lambda: il.open_raster(b"RIFF\x10\x00\x00\x00WEBPVP8 junkjunk"),
    il.ImageLocalizationError,
    "not a decodable image",
)
# Gate (train-12): bytes with no recognised raster magic never reach Pillow's open-ended
# plugin probe (EPS would run Ghostscript on load). An ICO and an EPS header named .png on
# the generic upload are refused before Image.open.
ico = io.BytesIO()
Image.new("RGB", (16, 16), "white").save(ico, format="ICO")
refused(lambda: il.open_raster(ico.getvalue()), il.ImageLocalizationError, "not PNG, JPEG")
eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
refused(lambda: il.open_raster(eps), il.ImageLocalizationError, "not PNG, JPEG")
refused(lambda: get_filter("png").parse(ico.getvalue()), il.ImageLocalizationError, "not PNG")
print("hostile ok")

# --- pipeline -------------------------------------------------------------------------------
init_db()
with Session(engine) as s:
    c = Customer(name="Alias Co")
    s.add(c)
    s.commit()
    s.refresh(c)
    p = Project(name="aliases", source_locale="en-US", default_provider="mock", customer_id=c.id)
    s.add(p)
    s.commit()
    s.refresh(p)
    s.add(TargetLocale(project_id=p.id, locale="es-ES"))
    s.commit()
    for ext, kind in ALIASES.items():
        doc = il.ingest_image(s, p, f"banner.{ext}", still(PIL_NAME[kind]), actor="verify")
        assert doc.fmt == ext, doc.fmt
        out, flags = il.render_localized(s, doc, "es-ES")
        assert il.sniff_kind(out) == kind and flags == []
    before = len(s.exec(select(Document)).all())
    for fmt, ext in (("GIF", "gif"), ("WEBP", "webp"), ("TIFF", "tiff")):
        refused(lambda fmt=fmt, ext=ext: il.ingest_image(s, p, f"anim.{ext}", multi(fmt)))
        s.rollback()
        refused(
            lambda fmt=fmt, ext=ext: ingest_document(
                s, p, f"anim.{ext}", multi(fmt), TransMode.SEGMENTED, created_by="verify"
            )
        )
        s.rollback()
    assert len(s.exec(select(Document)).all()) == before, "a refused image was stored"
print("pipeline ok")

print("IMAGE RASTER ALIASES OK")
