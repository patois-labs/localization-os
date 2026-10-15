"""Image Localization (Beta) — detection, typesetting, and pipeline glue.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports enabled, sniff_kind, normalize_region,
_reading_order, _rect_quad, _parse_rapidocr_result, _vision_live, _is_cjk, _line_width, _wrap,
_luminance, _segments_per_unit, stamp_region_meta from app.services.image_localization_part1.

SPEC-image-localization Phase 1. This module owns everything image-specific so
the surfaces (web /images, CLI `image`, /api/images) and the ImageFilter stay
thin callers:

  * the pluggable REGION DETECTOR seam (mock / winocr / rapidocr) — geometry is
    ALWAYS pixel coordinates from a local detector, NEVER from a VLM (research 02);
  * the geometry contract normalizer (one dict shape for every detector);
  * VLM escalation for low-confidence region RECOGNITION only (text, never
    geometry) + the one-per-image CONTEXT BRIEF call — both spend-guarded;
  * OVERLAY-mode raster composition (D4): border-ring flat-fill erase + fit-to-box
    typesetting with the bundled Noto fonts (D5), OVERFLOW flagged, never truncated
    (research 03);
  * ingest/translate/render orchestration on top of the UNCHANGED document
    pipeline (ingest_document / pretranslate_stream / export seams), so
    TM / glossary / refusal / LQA / workflows apply to image regions for free.

Kill switch: `image_localization_enabled` setting (default "1"); the SURFACES
gate on it (enabled() below) — this service stays importable either way.

Detector resolution order (resolve_detector) — the ENGINE is resolved first, and
the fixture sidecar is honored ONLY when that resolution is the MockDetector, so
uploaded bytes can never silence a real OCR engine (issue #30):
  1. env LOC_TMS_IMAGE_DETECTOR = mock|winocr|rapidocr — explicit operator pick
     (unknown name -> error; a named engine that is not installed -> error);
  2. no env: winocr (Windows.Media.Ocr) when importable — the D1 default, zero
     ML deps;
  3. no env: rapidocr (+onnxruntime) when importable — the blessed opt-in
     (`pip install rapidocr onnxruntime`, NOT in requirements.txt);
  4. MockDetector otherwise (env=mock, or no env and no real engine installed).
A fixture sidecar embedded in the image (PNG tEXt "loc_tms_regions") is the
hermetic-test + keyless-demo path: it feeds the MockDetector deterministic
regions, but ONLY when the resolution above already landed on mock. If a real
engine is resolved (env override or auto-detected) the sidecar is IGNORED and
one info line is logged for operator visibility; a MockDetector with no sidecar
yields zero regions (the honest offline answer; the UI/CLI say how to enable an
engine).

Documented in: docs/architecture/image-localization.md
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import os
import statistics
import sys
from collections import OrderedDict

from app import config
from app.filters.base import InputLimitError
from app.services import settings
from app.services.image_localization_part1 import (
    _is_cjk,
    _line_width,
    _luminance,
    _parse_rapidocr_result,
    _reading_order,
    _rect_quad,
    _segments_per_unit,
    _vision_live,
    _wrap,
    enabled,
    normalize_region,
    sniff_kind,
    stamp_region_meta,
)

__all__ = [
    "_is_cjk",
    "_line_width",
    "_luminance",
    "_parse_rapidocr_result",
    "_reading_order",
    "_rect_quad",
    "_segments_per_unit",
    "_vision_live",
    "_wrap",
    "enabled",
    "math",
    "normalize_region",
    "settings",
    "sniff_kind",
    "stamp_region_meta",
]

log = logging.getLogger("loc_tms.image_localization")

# ---------------------------------------------------------------------------
# Constants (the fit/erase knobs — research 03 values)
# ---------------------------------------------------------------------------
IMAGE_EXTS = ("svg", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp")
# Raster kinds sniff_kind can return (file-format Batch 16 added tiff/bmp/gif/webp).
RASTER_KINDS = ("png", "jpeg", "tiff", "bmp", "gif", "webp")
# sniff_kind -> the ONE Pillow plugin allowed to open those bytes (Image.open formats=).
# Pinning the plugin means a crafted file can never reach an unexpected decoder.
_PIL_FORMAT = {
    "png": "PNG",
    "jpeg": "JPEG",
    "tiff": "TIFF",
    "bmp": "BMP",
    "gif": "GIF",
    "webp": "WEBP",
}
# Decompression-bomb guard (spec 3.3): per-dimension pixel cap, independent of
# the MAX_UPLOAD_BYTES byte cap.
MAX_PIXEL_DIM = 8000
# Total-pixel cap (#546): a 8000x8000 image passes the per-side guard but decodes to
# ~256 MB of RGBA, so two concurrent uploads could OOM a small machine. 25 MP covers
# any real screenshot or photo we localize (5000x5000). Override with the
# IMAGE_MAX_TOTAL_PIXELS environment variable.
MAX_TOTAL_PIXELS = config.IMAGE_MAX_TOTAL_PIXELS
# Per-image region cap (spec R2): a pathological detection result is truncated
# and flagged rather than fanned out into hundreds of segments/provider calls.
MAX_REGIONS = 100
# Fit-to-box (research 03): inner box ratio, size search bounds, spacings.
INNER_BOX_RATIO = 0.75
MIN_FONT_SIZE = 10
MAX_FONT_SIZE = 72
LINE_SPACING = 1.15
TIGHT_LINE_SPACING = 1.05
# Erase (research 03 tier 1): mask dilation + border-ring width, pixels.
ERASE_PAD = 4
RING_WIDTH = 3
# VLM recognition escalation fires only below this detector confidence.
VLM_CONFIDENCE_THRESHOLD = 0.65
# Whole-image context call: downscale the long edge to this before sending.
CONTEXT_MAX_EDGE = 1024

_FIXTURE_KEY = "loc_tms_regions"  # PNG tEXt sidecar key the MockDetector reads

FONT_DIR = config.BASE_DIR / "assets" / "fonts"
_FONT_FILES = {
    "latin": "NotoSans-Regular.ttf",
    "cjk": "NotoSansCJK-Regular.otf",
}

# Test seam: when set, _guarded_vision_call uses this callable
# (fn(png_bytes, prompt) -> str) instead of a real SDK dispatch, while still
# running the SAME spend pre-check + usage recording around it. Lets the
# hermetic suite prove the guard-rail joins without any provider SDK/key.
vision_call_override = None


class ImageLocalizationError(Exception):
    """Clean, expected failure (bad image, missing engine, over caps)."""


class ImageRefusedError(ImageLocalizationError, InputLimitError):
    """A raster the overlay build cannot reproduce faithfully (an animated GIF or WebP,
    a multi-page TIFF) or cannot decode on this install (no WebP codec). Also an
    InputLimitError so every generic upload door (web, /api, CLI, MCP) shows the plain
    message instead of 'corrupted' (file-format Batch 16)."""


def _check_single_frame(im, kind: str | None) -> None:
    """Refuse a multi-frame raster: the overlay build draws on one frame and would
    silently flatten an animation or drop TIFF pages. `is_animated` reads only as far
    as the second frame, never the whole chain."""
    try:
        multi = bool(getattr(im, "is_animated", False))
    except Exception as exc:  # a broken frame chain is not a decodable image
        raise ImageLocalizationError(f"not a decodable image: {exc}") from exc
    if not multi:
        return
    what = {"tiff": "TIFF has more than one page", "gif": "GIF is animated"}.get(
        kind or "", f"{(kind or 'image').upper()} is animated"
    )
    raise ImageRefusedError(
        f"This {what}. Image translation draws on a single frame and would flatten it, "
        "so it was not processed. Export the frame or page you need as a single-frame "
        "PNG (or a still GIF, WebP or TIFF) and upload that."
    )


def open_raster(data: bytes):
    """Open raster bytes with Pillow behind the pixel-dimension guard."""
    from PIL import Image

    kind = sniff_kind(data)
    pil_format = _PIL_FORMAT.get(kind or "")
    if pil_format is None:
        # Gate (train-12): bytes whose magic is not one of our raster kinds never reach
        # Pillow's open-ended plugin probe. The generic document upload now calls this for
        # any .png/.gif/... name, and an unpinned probe would let e.g. EPS (Ghostscript on
        # load) or another exotic decoder run on attacker bytes.
        raise ImageLocalizationError(
            "not a decodable image: the content is not PNG, JPEG, TIFF, BMP, GIF or WebP"
        )
    if kind == "webp":
        from PIL import features

        if not features.check("webp"):
            raise ImageRefusedError(
                "This install's image library has no WebP codec, so WebP images cannot "
                "be translated here. Convert the image to PNG and upload that."
            )
    try:
        # Only the plugin the magic bytes name may decode the file (None for bytes we do
        # not recognise: Pillow then refuses or reads them as before).
        im = Image.open(io.BytesIO(data), formats=[pil_format] if pil_format else None)
        # Check dimensions from the header BEFORE decoding pixels: im.load() on a
        # 13000x13000 image allocates ~600MB even though the guard would reject it,
        # so an oversized (but under Pillow's own bomb threshold) upload is a
        # transient-memory DoS if we decode first. Image.open is lazy; .size is
        # populated from the header without loading the pixel data.
        w, h = im.size
        if w > MAX_PIXEL_DIM or h > MAX_PIXEL_DIM:
            raise ImageLocalizationError(
                f"image dimensions {w}x{h} exceed the {MAX_PIXEL_DIM}px guard"
            )
        if w * h > MAX_TOTAL_PIXELS:
            raise ImageLocalizationError(
                f"image is {w * h / 1_000_000:.0f} megapixels ({w}x{h}); the total-pixel "
                f"guard allows {MAX_TOTAL_PIXELS // 1_000_000} megapixels"
            )
        _check_single_frame(im, kind)
        im.load()
    except ImageLocalizationError:
        raise
    except Exception as exc:
        raise ImageLocalizationError(f"not a decodable image: {exc}") from exc
    return im


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------
class MockDetector:
    """Deterministic offline detector.

    Reads a fixture sidecar embedded in the PNG's tEXt metadata under the
    `loc_tms_regions` key: a JSON list of {"text": ..., "bbox": [x,y,w,h],
    optional "confidence"/"polygon"/"angle_deg"}. The hermetic suite and the
    demo-image generator (scripts/make_demo_image.py) write that chunk when
    drawing known text at known coords — so detect->translate->render is fully
    reproducible with zero engines and zero keys. An image WITHOUT the sidecar
    yields no regions (the honest offline answer; the surfaces explain how to
    enable winocr/rapidocr)."""

    name = "mock"

    @staticmethod
    def available() -> bool:
        return True

    def detect(self, data: bytes) -> list[dict]:
        kind = sniff_kind(data)
        if kind != "png":
            return []
        im = open_raster(data)
        raw = (getattr(im, "text", None) or {}).get(_FIXTURE_KEY, "")
        if not raw:
            return []
        try:
            rows = json.loads(raw)
        except (ValueError, TypeError):
            return []
        out = []
        for i, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, dict) or not row.get("text") or not row.get("bbox"):
                continue
            try:
                bbox = list(row["bbox"])
                if len(bbox) != 4:
                    continue
                # The sidecar is attacker-controllable (any uploaded PNG's tEXt
                # chunk): coerce numerics defensively so a non-numeric / malformed
                # bbox skips the region rather than raising a 500 mid-ingest.
                out.append(
                    normalize_region(
                        i,
                        text=str(row["text"]),
                        bbox=[float(v) for v in bbox],
                        polygon=row.get("polygon"),
                        angle_deg=float(row.get("angle_deg", 0.0)),
                        confidence=float(row.get("confidence", 0.99)),
                        source="mock",
                    )
                )
            except (TypeError, ValueError):
                continue
        return _reading_order(out)


class WinOcrDetector:
    """Windows.Media.Ocr via the `winocr` package (D1 default on Windows).

    Native OS feature, zero ML deps; real pixel word boxes + line grouping.
    Lazy import + graceful absence. Windows OCR reports no per-word confidence,
    so lines carry a fixed 0.9 (above the VLM escalation threshold — escalation
    is driven by engines that DO report confidence, or by an operator lowering
    the threshold)."""

    name = "winocr"

    @staticmethod
    def available() -> bool:
        if sys.platform != "win32":
            return False
        try:
            import winocr  # noqa: F401

            return True
        except Exception:
            return False

    def detect(self, data: bytes) -> list[dict]:
        import winocr

        im = open_raster(data).convert("RGB")
        result = winocr.recognize_pil_sync(im, lang="en")
        # winocr returns a dict-like {'text':..., 'lines':[{'text':...,
        # 'words':[{'bounding_rect':{'x','y','width','height'},'text':...}]}]}
        lines = (result or {}).get("lines") or []
        out = []
        for i, line in enumerate(lines):
            words = line.get("words") or []
            rects = [w.get("bounding_rect") or {} for w in words]
            rects = [r for r in rects if r]
            text = (line.get("text") or "").strip()
            if not text or not rects:
                continue
            x0 = min(r.get("x", 0) for r in rects)
            y0 = min(r.get("y", 0) for r in rects)
            x1 = max(r.get("x", 0) + r.get("width", 0) for r in rects)
            y1 = max(r.get("y", 0) + r.get("height", 0) for r in rects)
            word_items = [
                {"text": (w.get("text") or ""), "polygon": _rect_quad(w.get("bounding_rect") or {})}
                for w in words
            ]
            out.append(
                normalize_region(
                    i,
                    text=text,
                    bbox=[x0, y0, x1 - x0, y1 - y0],
                    confidence=0.9,
                    lines=[{"text": text, "polygon": None, "words": word_items}],
                    source="winocr",
                )
            )
        # lines[].polygon defaulted to None above; normalize_region only fills
        # the region-level polygon, so patch line polygons to the region quad.
        for r in out:
            for ln in r["lines"]:
                if ln.get("polygon") is None:
                    ln["polygon"] = r["polygon"]
        return _reading_order(out)


class RapidOcrDetector:
    """RapidOCR (ONNX) — the blessed OPT-IN engine (D2). NOT in requirements.txt;
    enable with `pip install rapidocr onnxruntime`. Lazy import; clean message
    when absent. Returns DB-detector quads + per-item confidence, so the VLM
    recognition escalation has real scores to trigger on."""

    name = "rapidocr"

    @staticmethod
    def available() -> bool:
        try:
            try:
                from rapidocr import RapidOCR
            except ImportError:
                from rapidocr_onnxruntime import RapidOCR  # noqa: F401
            return True
        except Exception:
            return False

    def detect(self, data: bytes) -> list[dict]:
        try:
            from rapidocr import RapidOCR
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR()
        im = open_raster(data).convert("RGB")
        # RapidOCR accepts numpy arrays / paths / bytes depending on version;
        # PNG bytes are the most portable input across versions.
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        result = engine(buf.getvalue())
        return _parse_rapidocr_result(result)


_DETECTORS = {d.name: d for d in (MockDetector, WinOcrDetector, RapidOcrDetector)}


def _has_fixture_sidecar(data: bytes) -> bool:
    if sniff_kind(data) != "png":
        return False
    try:
        im = open_raster(data)
    except ImageLocalizationError:
        raise
    except Exception:
        return False
    return bool((getattr(im, "text", None) or {}).get(_FIXTURE_KEY))


def resolve_detector(data: bytes):
    """Pick the detector for these bytes (see module docstring for the order).

    The ENGINE is resolved first; the fixture sidecar is honored only when that
    resolution is the MockDetector, so an uploaded PNG carrying a `loc_tms_regions`
    tEXt chunk can never override a real, configured OCR engine (issue #30). When
    a real engine is resolved and the bytes carry a sidecar, the sidecar is
    ignored and one info line is logged (no body content) for operator
    visibility."""
    has_sidecar = _has_fixture_sidecar(data)
    env = (os.environ.get("LOC_TMS_IMAGE_DETECTOR") or "").strip().lower()
    if env:
        cls = _DETECTORS.get(env)
        if cls is None:
            raise ImageLocalizationError(
                f"unknown LOC_TMS_IMAGE_DETECTOR {env!r} (mock|winocr|rapidocr)"
            )
        if not cls.available():
            raise ImageLocalizationError(
                f"detector {env!r} is not available on this install"
                + (" - pip install rapidocr onnxruntime" if env == "rapidocr" else "")
            )
        if has_sidecar and cls is not MockDetector:
            log.info(
                "image-loc: fixture sidecar ignored; real detector %r resolved via "
                "LOC_TMS_IMAGE_DETECTOR",
                env,
            )
        return cls()
    if WinOcrDetector.available():
        if has_sidecar:
            log.info("image-loc: fixture sidecar ignored; real detector 'winocr' auto-detected")
        return WinOcrDetector()
    if RapidOcrDetector.available():
        if has_sidecar:
            log.info("image-loc: fixture sidecar ignored; real detector 'rapidocr' auto-detected")
        return RapidOcrDetector()
    return MockDetector()


def detector_status() -> dict:
    """What the surfaces show/print: which engines are available + the default."""
    avail = {name: cls.available() for name, cls in _DETECTORS.items()}
    if WinOcrDetector.available():
        default = "winocr"
    elif RapidOcrDetector.available():
        default = "rapidocr"
    else:
        default = "mock"
    env = (os.environ.get("LOC_TMS_IMAGE_DETECTOR") or "").strip().lower()
    return {
        "available": avail,
        "default": env or default,
        "env_override": env or None,
        "hint": ("rapidocr: pip install rapidocr onnxruntime" if not avail.get("rapidocr") else ""),
    }


# --- detection cache (parse and build must see identical regions) ------------
_DETECT_CACHE: OrderedDict[str, list] = OrderedDict()
_DETECT_CACHE_MAX = 32


def detect_regions(data: bytes) -> list[dict]:
    """Detect text regions in raster bytes, content-hash cached.

    The cache is what makes ImageFilter.parse and ImageFilter.build agree on
    geometry within a process even for a live engine: build() re-detects on the
    SAME original bytes (the html/docx filter re-derive pattern) and gets the
    cached, identical region list. The MockDetector is deterministic anyway; the
    cache also spares a real engine a second OCR pass per export."""
    key = hashlib.sha256(data).hexdigest()
    if key in _DETECT_CACHE:
        _DETECT_CACHE.move_to_end(key)
        return json.loads(json.dumps(_DETECT_CACHE[key]))  # defensive copy
    regions = resolve_detector(data).detect(data)
    if len(regions) > MAX_REGIONS:
        regions = regions[:MAX_REGIONS]
        for r in regions:
            r["truncated_region_set"] = True
    _DETECT_CACHE[key] = json.loads(json.dumps(regions))
    while len(_DETECT_CACHE) > _DETECT_CACHE_MAX:
        _DETECT_CACHE.popitem(last=False)
    return regions


#: #125 — the display marker a refusal's operator-facing text carries, mirroring the
#: "[OCR error: ...]" placeholder ocr.ocr_image returns. It is only a DISPLAY prefix on
#: the message inside VisionRefusal.text; classification never keys on it (see the LOW-3
#: note on VisionRefusal), so a real recognized line that happens to start with these
#: characters can never be mistaken for a refusal.
VISION_ERROR_PREFIX = "[vision error: "


class VisionRefusal:
    """A TYPED result meaning "the egress door refused this vision call" (#125, LOW-3).

    ``_guarded_vision_call`` returns one of THREE distinct things, and a caller must be
    able to tell them apart without ever inspecting recognized CONTENT:
      * ``str``           -> recognized text (which may legitimately begin with any
                             characters, including ``[vision error: ``);
      * ``VisionRefusal`` -> a policy refusal to surface to the operator;
      * ``None``          -> a clean skip (keyless/offline) or a real provider failure.
    Distinguishing a refusal by an ``isinstance`` check on this type (never a string
    prefix on the returned text) is the whole point: prefix-sniffing the content would
    misclassify a real OCR line that starts with the marker, discard the corrected text,
    and skip later regions.
    """

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text

    def __repr__(self):
        return f"VisionRefusal({self.text!r})"


def _vision_error_text(exc: BaseException) -> str:
    """The OCR-shaped operator message for `exc` (same class name + message ocr.py surfaces)."""
    return f"{VISION_ERROR_PREFIX}{type(exc).__name__}: {exc}]"


def is_vision_error(value) -> bool:
    """True when a `_guarded_vision_call` return is a refusal, not recognized text.

    Typed check (LOW-3): a refusal is a ``VisionRefusal`` instance, never a string whose
    content merely looks like an error. Recognized text and ``None`` are both False."""
    return isinstance(value, VisionRefusal)


def _echo_checked(text):
    """#359: recognized text carrying a global credential raises KeyEchoError, which the
    caller's generic handler turns into a clean failure (None)."""
    from app.services.secrets_scrub import guard_answer

    return guard_answer(text)


def _guarded_vision_call(
    png_bytes: bytes, prompt: str, provider: str | None = None
) -> str | VisionRefusal | None:
    """One spend-guarded vision completion. Returns recognized ``str`` text, a typed
    ``VisionRefusal`` when the egress door refused the engine target (#125), or ``None``
    when skipped (keyless/offline) or a real provider call failed.

    Guard order (mirrors run_completion / spend.blocked_for):
      1. keyless / not vision-capable -> None (clean offline skip, no spend);
      2. monthly cap reached -> refuse BEFORE the call + emit the trip audit;
      3. fire the per-SDK call (ocr.py shapes);
      4. record real token usage into the spend ledger.
    """
    from app.services import ai_optout, spend
    from app.services.egress import EgressBlocked

    # AI opt-out choke point (DPAI-08): an opted-out project's image is never sent to a
    # vision model. A clean skip (None), exactly like the keyless/offline path.
    try:
        ai_optout.guard("image text recognition")
    except ai_optout.AIOptOutRefused:
        return None
    from app.services.pii_boundary import RequestRedaction

    redaction = RequestRedaction(prompt)
    prompt = redaction.redact(prompt)
    provider = (provider or config.DEFAULT_PROVIDER or "").lower()
    live = _vision_live(provider) or (vision_call_override is not None)
    if not live:
        return None
    blocked, _spent, _cap = spend.would_block()
    if blocked:
        spend.note_trip()
        return None
    model = config.DEFAULT_MODELS.get(provider, "")
    if vision_call_override is not None:
        text = vision_call_override(png_bytes, prompt)
        usage = {
            "input_tokens": max(1, len(png_bytes) // 1000),
            "output_tokens": max(1, len(text or "") // 4),
        }
        spend.record_ref_usage(provider, model, usage)
        return redaction.restore(text)
    b64 = base64.b64encode(png_bytes).decode()
    try:
        if provider == "claude" and config.ANTHROPIC_API_KEY:
            import anthropic

            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model=model,
                max_tokens=1500,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
            u = getattr(msg, "usage", None)
            spend.record_ref_usage(
                provider,
                model,
                {
                    "input_tokens": getattr(u, "input_tokens", 0) or 0,
                    "output_tokens": getattr(u, "output_tokens", 0) or 0,
                },
            )
            return _echo_checked(
                redaction.restore("".join(b.text for b in msg.content if b.type == "text"))
            )
        if provider in ("openai", "local"):
            # #342: the pinned local sampling; the hosted OpenAI request stays as it was.
            # #568: resolved BEFORE the client is built, so a raise here cannot strand a
            # client that the try/finally below has not yet taken ownership of.
            sampling = {}
            if provider == "local":
                from app.services.providers_hub import local_sampling_kwargs

                sampling = local_sampling_kwargs()
            if provider == "local":
                from openai import DEFAULT_TIMEOUT

                from app.services.credential_origin import slot_key_for
                from app.services.providers import _local_client

                client = _local_client(
                    base_url=config.LOCAL_BASE_URL,
                    api_key=slot_key_for(config.LOCAL_BASE_URL),
                    timeout=DEFAULT_TIMEOUT,
                )
                model = config.DEFAULT_MODELS.get("local") or "local-model"
            else:
                from openai import OpenAI

                client = OpenAI(api_key=config.OPENAI_API_KEY)
            try:
                resp = client.chat.completions.create(
                    model=model,
                    max_tokens=1500,
                    **sampling,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                                },
                            ],
                        }
                    ],
                )
            finally:
                if provider == "local":
                    # #437: _local_client's caller-supplied http_client is never closed
                    # by the SDK; close the per-image local client on every path. The
                    # hosted OpenAI client (self-built) stays as it was.
                    client.close()
            u = getattr(resp, "usage", None)
            spend.record_ref_usage(
                provider,
                model,
                {
                    "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
                    "output_tokens": getattr(u, "completion_tokens", 0) or 0,
                },
            )
            return _echo_checked(redaction.restore(resp.choices[0].message.content or ""))
        if provider == "gemini" and config.GEMINI_API_KEY:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=config.GEMINI_API_KEY)
            resp = client.models.generate_content(
                model=model,
                contents=[types.Part.from_bytes(data=png_bytes, mime_type="image/png"), prompt],
            )
            um = getattr(resp, "usage_metadata", None)
            spend.record_ref_usage(
                provider,
                model,
                {
                    "input_tokens": getattr(um, "prompt_token_count", 0) or 0,
                    "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
                },
            )
            return _echo_checked(redaction.restore(resp.text or ""))
    except EgressBlocked as exc:
        # #125: the #120/#124 egress door's refusal is a POLICY ANSWER, not a provider
        # failure. The blanket handler below used to swallow it, so image localization
        # went silently empty and the operator never learned the configured engine target
        # was blocked. Surface it as a TYPED refusal carrying the SAME operator message
        # shape ocr.ocr_image returns after #124; the callers below check the type (never
        # the text) and keep it out of translatable content.
        log.warning("vision call refused by the egress door: %s", exc)
        return VisionRefusal(_vision_error_text(exc))
    except Exception:
        # A real provider failure (timeout, bad key, malformed response). Unchanged: the
        # callers degrade to the classical detector's own text / no context brief.
        return None
    return None


_RECOGNIZE_PROMPT = (
    "You are an OCR engine. Transcribe the text in this cropped image region "
    "exactly as it appears. Output ONLY the transcribed text - no commentary, "
    "no translation, no quotes."
)

_CONTEXT_PROMPT = (
    "You are a localization context analyst. In 2-4 short sentences, describe "
    "this image for a translator: what kind of asset it is (banner, screenshot, "
    "diagram, ad...), its audience and tone, and what role the visible text "
    "plays. Plain prose only - no headings, no lists, no instructions."
)


def escalate_recognition(
    data: bytes, regions: list[dict], provider: str | None = None
) -> list[dict]:
    """VLM RECOGNITION escalation (research 02 hybrid rule): re-read the text of
    low-confidence regions from their pixel crops. Text only — the polygon/bbox
    stay exactly as the classical detector produced them (iron rule: geometry is
    never VLM-sourced). No-op when keyless (clean offline skip)."""
    if not regions:
        return regions
    live = _vision_live(provider) or (vision_call_override is not None)
    if not live:
        return regions
    im = None
    for r in regions:
        if r.get("confidence", 1.0) >= VLM_CONFIDENCE_THRESHOLD:
            continue
        if im is None:
            im = open_raster(data).convert("RGB")
        x, y, w, h = r["bbox_axis_aligned"]
        pad = ERASE_PAD
        crop = im.crop(
            (
                max(0, x - pad),
                max(0, y - pad),
                min(im.width, x + w + pad),
                min(im.height, y + h + pad),
            )
        )
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        text = _guarded_vision_call(buf.getvalue(), _RECOGNIZE_PROMPT, provider)
        if is_vision_error(text):
            # #125: record the refusal on the region -- the region list IS the persisted
            # geometry contract the review page, Segment.meta and render all read, so this
            # is where the run's result can show it -- and STOP escalating: the same engine
            # target refuses every remaining region, so continuing would only repeat the
            # door's audit row. The region keeps its classical text and confidence.
            r["vlm_error"] = text.text
            log.warning("VLM recognition escalation stopped: %s", text.text)
            break
        if text and text.strip():
            r["text"] = " ".join(text.split())
            r["source"] = "vlm_recognized"
            r["confidence"] = max(r.get("confidence", 0.0), VLM_CONFIDENCE_THRESHOLD)
    return regions


def context_brief_for_image(data: bytes, provider: str | None = None) -> str | None:
    """One spend-guarded whole-image vision call -> a short context brief, or
    None offline (skip cleanly). The caller attaches it as the document context
    package's `notes` (the SAME Document.context seam the doc-context feature
    uses), so it reaches every region's translation prompt sanitized by
    context_pkg.validate_and_sanitize."""
    kind = sniff_kind(data)
    if kind not in RASTER_KINDS:
        return None
    live = _vision_live(provider) or (vision_call_override is not None)
    if not live:
        return None
    im = open_raster(data).convert("RGB")
    if max(im.size) > CONTEXT_MAX_EDGE:
        scale = CONTEXT_MAX_EDGE / max(im.size)
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    text = _guarded_vision_call(buf.getvalue(), _CONTEXT_PROMPT, provider)
    if is_vision_error(text):
        # #125: a refusal is not a brief. It must never reach Document.context, from where
        # context_pkg would carry it into EVERY region's translation prompt. It is already
        # logged (and audited) at the door; ingest continues briefless, exactly as offline.
        return None
    if not text:
        return None
    return " ".join(text.split())[:800] or None


def _font_path(text: str) -> str | None:
    """Bundled font for this text (CJK-aware), or None -> Pillow default."""
    name = _FONT_FILES["cjk"] if _is_cjk(text) else _FONT_FILES["latin"]
    p = FONT_DIR / name
    if p.is_file():
        return str(p)
    p = FONT_DIR / _FONT_FILES["latin"]
    return str(p) if p.is_file() else None


def _load_font(text: str, size: int):
    from PIL import ImageFont

    path = _font_path(text)
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()  # degraded, flagged by caller


def fit_text_to_box(text: str, box_w: int, box_h: int) -> dict:
    """The research-03 fit algorithm. Returns {size, lines, spacing, overflow,
    font_degraded}. Search: inner box ~75%; start size from the box; iterate
    size DOWN; at each size try normal 1.15 spacing then the tighter 1.05
    (spacing reduction BEFORE size reduction, the comic-translate order); floor
    at MIN_FONT_SIZE and FLAG overflow — never truncate the text."""
    inner_w = max(1, int(box_w * INNER_BOX_RATIO))
    inner_h = max(1, int(box_h * INNER_BOX_RATIO))
    start = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, int(box_h * 0.45)))
    degraded = _font_path(text) is None
    size = start
    while size >= MIN_FONT_SIZE:
        font = _load_font(text, size)
        lines = _wrap(text, font, inner_w)
        if lines is not None:
            for spacing in (LINE_SPACING, TIGHT_LINE_SPACING):
                total_h = int(size * spacing * len(lines))
                if total_h <= inner_h:
                    return {
                        "size": size,
                        "lines": lines,
                        "spacing": spacing,
                        "overflow": False,
                        "font_degraded": degraded,
                    }
        size -= 1
    # Floor: render everything at the minimum readable size, tight spacing,
    # flagged OVERFLOW (research 03: flag, never silently truncate).
    font = _load_font(text, MIN_FONT_SIZE)
    lines = _wrap(text, font, inner_w)
    if lines is None:
        lines = [text]  # even one char over: draw it anyway, flagged
    return {
        "size": MIN_FONT_SIZE,
        "lines": lines,
        "spacing": TIGHT_LINE_SPACING,
        "overflow": True,
        "font_degraded": degraded,
    }


# ---------------------------------------------------------------------------
# Erase (tier 1: border-ring flat fill) + compose (OVERLAY mode, D4)
# ---------------------------------------------------------------------------
def _ring_median_color(im, box: tuple[int, int, int, int]) -> tuple:
    """Median color of the RING_WIDTH-px band around `box` (the dilated mask).
    Median per channel — robust to a few glyph pixels leaking into the ring."""
    x0, y0, x1, y1 = box
    rx0, ry0 = max(0, x0 - RING_WIDTH), max(0, y0 - RING_WIDTH)
    rx1, ry1 = min(im.width, x1 + RING_WIDTH), min(im.height, y1 + RING_WIDTH)
    px = im.load()
    samples = []
    for yy in range(ry0, ry1):
        for xx in range(rx0, rx1):
            if x0 <= xx < x1 and y0 <= yy < y1:
                continue  # inside the erase rect: not part of the ring
            samples.append(px[xx, yy])
    if not samples:
        return (255,) * len(im.getbands())
    nch = len(samples[0]) if isinstance(samples[0], tuple) else 1
    if nch == 1:
        return (int(statistics.median(samples)),)
    return tuple(int(statistics.median([s[c] for s in samples])) for c in range(nch))


def compose_overlay(
    data: bytes, regions: list[dict], texts: dict[str, str]
) -> tuple[bytes, list[dict]]:
    """OVERLAY-mode raster build: for each region with a replacement text,
    erase via border-ring flat fill, then draw the text fit-to-box. Returns
    (image bytes in the ORIGINAL raster format, per-region render flags).

    Deterministic: same bytes + same texts + same bundled font -> identical
    output bytes (the hermetic suite pins this)."""
    from PIL import ImageDraw, ImageFont

    kind = sniff_kind(data)
    im = open_raster(data)
    src_info = dict(getattr(im, "info", {}) or {})
    out_format = _PIL_FORMAT.get(kind or "", "JPEG")
    if out_format == "PNG":
        im = im.convert("RGBA")
    elif out_format == "JPEG":
        im = im.convert("RGB")
    else:  # Batch 16 aliases: keep transparency only where the source had it
        has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in src_info
        im = im.convert("RGBA" if has_alpha else "RGB")
    draw = ImageDraw.Draw(im)
    flags: list[dict] = []
    for r in regions:
        idx = r["reading_order_index"]
        new_text = texts.get(str(idx))
        if new_text is None or not str(new_text).strip():
            continue
        new_text = str(new_text)
        x, y, w, h = r["bbox_axis_aligned"]
        # --- erase: dilate the box by ERASE_PAD, flat-fill with the ring median
        ex0, ey0 = max(0, x - ERASE_PAD), max(0, y - ERASE_PAD)
        ex1, ey1 = min(im.width, x + w + ERASE_PAD), min(im.height, y + h + ERASE_PAD)
        if ex1 <= ex0 or ey1 <= ey0:
            continue  # degenerate/out-of-frame region: nothing to do
        bg = _ring_median_color(im, (ex0, ey0, ex1, ey1))
        draw.rectangle([ex0, ey0, ex1 - 1, ey1 - 1], fill=bg)
        # --- typeset: fit-to-box inside the ORIGINAL region box
        fit = fit_text_to_box(new_text, w, h)
        font = _load_font(new_text, fit["size"])
        if isinstance(font, ImageFont.ImageFont):  # bitmap default = degraded
            fit["font_degraded"] = True
        fg = (20, 20, 20) if _luminance(bg) >= 128 else (240, 240, 240)
        if im.mode == "RGBA":
            fg = fg + (255,)  # noqa: RUF005
        line_h = int(fit["size"] * fit["spacing"])
        block_h = line_h * len(fit["lines"])
        ty = y + max(0, (h - block_h) // 2)
        for line in fit["lines"]:
            lw = _line_width(font, line)
            tx = x + max(0, (w - lw) // 2)
            draw.text((tx, ty), line, font=font, fill=fg)
            ty += line_h
        flags.append(
            {
                "region_id": r["id"],
                "reading_order_index": idx,
                "overflow": fit["overflow"],
                "fitted_size": fit["size"],
                "font_degraded": fit.get("font_degraded", False),
            }
        )
    buf = io.BytesIO()
    if out_format == "JPEG":
        im.save(buf, format="JPEG", quality=92)
    elif out_format == "PNG":
        im.save(buf, format="PNG")
    else:
        im.save(buf, format=out_format, **_alias_save_options(out_format, src_info))
    return buf.getvalue(), flags


# TIFF compressions Pillow writes losslessly without extra codecs; anything else
# (JPEG-in-TIFF, CCITT fax on a converted RGB image) is written as LZW.
_TIFF_KEEP_COMPRESSION = ("raw", "tiff_lzw", "tiff_adobe_deflate", "packbits")


def _alias_save_options(out_format: str, src_info: dict) -> dict:
    """Save options for the Batch 16 raster aliases: the source's own format, its ICC
    profile and resolution where the format carries them, deterministic settings."""
    opts: dict = {}
    icc = src_info.get("icc_profile")
    if icc and out_format in ("TIFF", "WEBP"):
        opts["icc_profile"] = icc
    if out_format == "TIFF":
        comp = src_info.get("compression")
        opts["compression"] = comp if comp in _TIFF_KEEP_COMPRESSION else "tiff_lzw"
        if src_info.get("dpi"):
            opts["dpi"] = src_info["dpi"]
    elif out_format == "WEBP":
        opts["quality"] = 92
        opts["method"] = 4
    elif out_format == "BMP" and src_info.get("dpi"):
        opts["dpi"] = src_info["dpi"]
    return opts


# ---------------------------------------------------------------------------
# Pipeline orchestration (ingest / meta / rows / translate / render)
# ---------------------------------------------------------------------------
def is_image_fmt(fmt: str) -> bool:
    return (fmt or "").lower() in IMAGE_EXTS


def ingest_image(session, project, filename: str, data: bytes, actor: str = "") -> object:
    """Upload seam shared by web + CLI: validate, run the one-per-image context
    call (live only; offline skips cleanly), ingest through the STANDARD
    ingest_document seam (SEGMENTED — one Segment per region sentence), then
    stamp each Segment.meta with its region's geometry."""
    from app.models import TransMode
    from app.services.ingest import ingest_document

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in IMAGE_EXTS:
        raise ImageLocalizationError(
            f"not an image format: .{ext or '?'} (accepted: " + ", ".join(IMAGE_EXTS) + ")"
        )
    kind = sniff_kind(data)
    if kind is None:
        raise ImageLocalizationError("file content is not SVG, PNG, JPEG, TIFF, BMP, GIF or WebP")
    if kind in RASTER_KINDS:
        open_raster(data)  # decode + pixel-dimension guard before anything else

    ctx = None
    if kind in RASTER_KINDS:
        # AI opt-out (DPAI-08): the vision brief runs inside the project's AI scope.
        from app.services import ai_optout

        with ai_optout.scope(project_id=getattr(project, "id", None)):
            brief = context_brief_for_image(data, project.default_provider)
        if brief:
            from app.services import context_pkg

            try:
                ctx = context_pkg.validate_and_sanitize({"notes": brief}) or None
            except context_pkg.ContextError:
                ctx = None  # a malformed brief must never block ingest

    # allow_empty (#573 gate): an image with no detected text still ingests (0 regions),
    # unlike an empty text file, which ingest_document refuses.
    doc = ingest_document(
        session,
        project,
        filename,
        data,
        TransMode.SEGMENTED,
        created_by=actor,
        context=ctx,
        allow_empty=True,
    )
    stamp_region_meta(session, doc)
    return doc


def region_rows(session, doc, locale: str) -> list[dict]:
    """The review-page/CLI/api row model: one row per region for `locale`,
    joining geometry (skeleton) with the region's segments + render flags."""
    from sqlmodel import select

    from app.models import Segment

    skel = doc.skeleton or {}
    regions = skel.get("regions") or []
    counts = _segments_per_unit(doc)
    segs = sorted(
        session.exec(
            select(Segment).where(Segment.document_id == doc.id, Segment.target_locale == locale)
        ).all(),
        key=lambda s: s.order_index,
    )
    rows: list[dict] = []
    i = 0
    for u_idx, n in enumerate(counts):
        chunk = segs[i : i + n]
        i += n
        region = regions[u_idx] if u_idx < len(regions) else None
        if region is None:
            continue
        translated = [(s.target_text or "") for s in chunk]
        # #668: CJK-aware seams (no space after 。 or between two CJK sentences).
        from app.services.segment import join_unit

        joined = join_unit([t for t in translated if t], None).strip()
        rows.append(
            {
                "region": region,
                "segments": chunk,
                "source_text": region.get("text", ""),
                "target_text": joined,
                "confidence": region.get("confidence", 1.0),
                "low_confidence": region.get("confidence", 1.0) < VLM_CONFIDENCE_THRESHOLD,
                "translated": bool(joined),
                # #125 (MED): the egress refusal recorded on the region at escalate time,
                # surfaced to every operator surface that reads this row model (web review,
                # CLI show, /api/images) exactly as OCR's "[OCR error: ...]" rides in the
                # extracted text. Empty string = no refusal (the overwhelming-majority case).
                "vlm_error": region.get("vlm_error", ""),
            }
        )
    # Fit flags for translated rows (cheap, deterministic — no image decode).
    for row in rows:
        if row["translated"]:
            _x, _y, w, h = row["region"]["bbox_axis_aligned"]
            fit = fit_text_to_box(row["target_text"], w, h)
            row["overflow"] = fit["overflow"]
            row["fitted_size"] = fit["size"]
        else:
            row["overflow"] = False
            row["fitted_size"] = None
    return rows


def translate_image(session, doc, locale: str, actor: str = "cli") -> dict:
    """Synchronous translate of an image document into `locale` through the
    UNCHANGED per-segment pipeline (TM lookup, glossary, refusal->NEEDS_REVIEW,
    spend, budgets all apply). Shared by the web route and the CLI verb
    (parity by construction). Returns the stream's summary stats."""
    from sqlmodel import select

    from app.models import Segment

    # #617 item 4 (gate): a run parked on a human_review step holds this locale; refuse before
    # the budget check. Surfaces answer 409 (web redirect error, /api 409, CLI error).
    from app.services import budgets, jobs, lifecycle, segment_review, translate

    try:
        segment_review.refuse_if_awaiting_review(session, doc.id, locale)
    except segment_review.AwaitingReviewError as exc:
        raise ImageLocalizationError(str(exc)) from exc
    dept = budgets.department_for_actor(session, actor)
    # #503: the budget row and its spend are the DOCUMENT's org's (None, unscoped, with the
    # gate off), so another org's department of the same name never gates or counts here.
    from app.models import Project as _Project
    from app.services import access as _access

    _proj = session.get(_Project, doc.project_id)
    allowed, _remaining, msg = budgets.check(
        dept, org_id=getattr(_proj, "org_id", None) if _access.multi_org_active() else None
    )
    if not allowed:
        raise ImageLocalizationError(msg)
    present = {
        s.target_locale
        for s in session.exec(select(Segment).where(Segment.document_id == doc.id)).all()
    }
    if locale not in present:
        raise ImageLocalizationError(
            f"locale {locale!r} not on document #{doc.id} "
            f"(present: {', '.join(sorted(present)) or 'none'})"
        )
    stats: dict = {}
    running: dict = {}  # #364: the stream's booked USD ("cost_usd"), for the Job row only
    n = 0
    for ev in translate.pretranslate_stream(session, doc.id, locale, stats_sink=running):
        if ev.get("type") == "segment":
            n += 1
        elif ev.get("type") == "summary":
            stats = {k: v for k, v in ev.items() if k != "type"}
    lifecycle.mark_completed_if_done(session, doc.project_id, actor=actor)
    # #588 / #624 (same rule as #572 and #612): any region failing is a FAILED run. All
    # failed carries the first engine error; a partial failure names the failed count and
    # keeps the per-region stats (errors, first_error) on the result. Regions that landed
    # stay saved. Only a run with no error ends COMPLETED.
    dead = jobs.run_error(stats, n or None)
    try:  # noqa: SIM105
        jobs.record_sync_job(
            "translate_document",
            {"document_id": doc.id, "locale": locale},
            actor=actor,
            result={**stats, "cost_usd": running.get("cost_usd")} if stats else {},
            total=n,
            status=jobs.JobStatus.FAILED if dead else jobs.JobStatus.COMPLETED,
            error=dead,
        )
    except Exception:  # noqa: S110 -- job-history row only; the translation already committed
        pass
    if dead:
        # The web route, /api route and CLI verb all turn this into their error answer.
        raise ImageLocalizationError(
            dead if dead.startswith("Partially translated") else f"Image translation failed: {dead}"
        )
    return stats


def render_localized(session, doc, locale: str) -> tuple[bytes, list[dict]]:
    """Localized image bytes + per-region render flags for `locale`.

    SVG: the filter's text-swap build (flags: none). Raster: overlay compose
    with the same paragraph mapping export uses, so what the review page shows
    IS what export downloads."""
    from app.ports import blobstore
    from app.services import export

    original = blobstore.get().get(blobstore.document_key(doc))
    # Batch 15 T2 (A-M4/B-MED-5): the image surface is OUT OF the empty-gate scope in v1, so
    # pass deliver_empties=False EXPLICITLY -- otherwise paragraph_texts_for would self-resolve
    # the covering-approval state and, post-approval, silently render approved-empty regions
    # blank with no audit on this ungated path. This keeps image localization byte-identical
    # to pre-T2 in BOTH gate states. (Gating the image delivery surface is a later train.)
    mapping = export.paragraph_texts_for(session, doc, locale, deliver_empties=False)
    if sniff_kind(original) == "svg":
        from app.filters import get_filter
        from app.filters.base import document_code_page

        with document_code_page(getattr(doc, "skeleton", None)):  # decision 22
            return get_filter(doc.fmt).build(original, mapping), []
    regions = (doc.skeleton or {}).get("regions") or detect_regions(original)
    return compose_overlay(original, regions, mapping)
