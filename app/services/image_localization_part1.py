"""image_localization: part1

Documented in: docs/architecture/image-localization.md
"""

from __future__ import annotations

import math

from app import config
from app.services import settings


def enabled() -> bool:
    """The module kill switch (settings image_localization_enabled, default on).
    An unreadable Setting table (bare import-time contexts) = the DEFAULT (on);
    every runtime surface has the DB initialized before this is consulted."""
    try:
        return str(settings.get("image_localization_enabled", "1")) == "1"
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Byte sniffing + safe open
# ---------------------------------------------------------------------------
def sniff_kind(data: bytes) -> str | None:
    """'svg' | 'png' | 'jpeg' | 'tiff' | 'bmp' | 'gif' | 'webp' by magic bytes, or None.
    Never trusts the extension."""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    # Raster aliases (file-format Batch 16). Magic numbers: TIFF 6.0 section 2 (II*\0 /
    # MM\0*; BigTIFF uses 43), BMP "BM", GIF87a/GIF89a, WebP RIFF....WEBP (RFC 9649).
    if data[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "tiff"
    if data[:2] == b"BM" and len(data) >= 26:
        return "bmp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:4096].lstrip()
    if head[:5] == b"<?xml" or head[:4] == b"<svg" or b"<svg" in head[:2048]:
        return "svg"
    return None


# ---------------------------------------------------------------------------
# The geometry contract (research 02)
# ---------------------------------------------------------------------------
def normalize_region(
    idx: int,
    *,
    text: str,
    bbox: list,
    polygon: list | None = None,
    angle_deg: float = 0.0,
    baseline: dict | None = None,
    confidence: float = 1.0,
    lines: list | None = None,
    source: str = "classical_detector",
) -> dict:
    """Return one region dict in the canonical geometry-contract shape.

    Coordinates are ALWAYS pixels (iron rule). bbox is [x, y, w, h]; polygon
    defaults to the bbox quad. `source` is the contract enum
    {classical_detector, winocr, vlm_recognized, vlm_fallback, mock}.
    """
    x, y, w, h = (int(round(v)) for v in bbox)  # noqa: RUF046
    if polygon is None:
        polygon = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
    polygon = [[int(round(px)), int(round(py))] for px, py in polygon]  # noqa: RUF046
    if baseline is None:
        # A horizontal region's baseline sits near the bottom of the box.
        baseline = {"slope": 0.0, "intercept": float(y + h)}
    return {
        "id": f"r{idx}",
        "reading_order_index": idx,
        "polygon": polygon,
        "bbox_axis_aligned": [x, y, w, h],
        "angle_deg": float(angle_deg),
        "baseline": {
            "slope": float(baseline.get("slope", 0.0)),
            "intercept": float(baseline.get("intercept", y + h)),
        },
        "text": text,
        "confidence": float(confidence),
        "lines": lines or [{"text": text, "polygon": polygon}],
        "source": source,
    }


def _reading_order(regions: list[dict]) -> list[dict]:
    """Stable top-to-bottom, then left-to-right order; re-stamp ids/indices."""
    regions = sorted(regions, key=lambda r: (r["bbox_axis_aligned"][1], r["bbox_axis_aligned"][0]))
    for i, r in enumerate(regions):
        r["id"] = f"r{i}"
        r["reading_order_index"] = i
    return regions


def _rect_quad(rect: dict) -> list:
    x, y = rect.get("x", 0), rect.get("y", 0)
    w, h = rect.get("width", 0), rect.get("height", 0)
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def _parse_rapidocr_result(result) -> list[dict]:
    """Normalize a RapidOCR call result into region dicts. Split out of
    RapidOcrDetector.detect so the version-shape handling is testable WITHOUT
    rapidocr installed (the hermetic suite feeds fake tuple- and object-shaped
    results, incl. numpy-array attributes -- the real 3.x shape that once raised
    'truth value of an array is ambiguous').

    v1 (rapidocr_onnxruntime): (list[[box, text, score]], elapse).
    v2/v3 (rapidocr): an object exposing .boxes/.txts/.scores as numpy arrays.
    """
    if result is None:
        return []
    if isinstance(result, tuple):
        rows = result[0]
        rows = [] if rows is None else list(rows)
        items = [(r[0], r[1], float(r[2]) if len(r) > 2 else 0.9) for r in rows]
    else:
        # NEVER `arr or []` on a numpy array (ambiguous-truth-value ValueError):
        # coerce None->[] explicitly, then list()-ify.
        boxes = getattr(result, "boxes", None)
        txts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        boxes = [] if boxes is None else list(boxes)
        txts = [] if txts is None else list(txts)
        scores = [] if scores is None else list(scores)
        items = []
        for i, box in enumerate(boxes):
            txt = txts[i] if i < len(txts) else ""
            sc = float(scores[i]) if i < len(scores) else 0.9
            items.append((box, txt, sc))
    out = []
    for i, (box, text, score) in enumerate(items):
        text = (text or "").strip()
        if not text:
            continue
        poly = [[float(p[0]), float(p[1])] for p in box]
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        bbox = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
        # Angle from the top edge of the quad.
        angle = (
            math.degrees(math.atan2(poly[1][1] - poly[0][1], poly[1][0] - poly[0][0]))
            if len(poly) >= 2
            else 0.0
        )
        out.append(
            normalize_region(
                i,
                text=text,
                bbox=bbox,
                polygon=poly,
                angle_deg=angle,
                confidence=score,
                source="classical_detector",
            )
        )
    return _reading_order(out)


# ---------------------------------------------------------------------------
# Spend-guarded vision calls (recognition escalation + context brief)
#
# Deliberately does NOT import app/services/ocr.py (issue #29 is being fixed on
# a parallel branch): this is a self-contained guarded caller following the
# documented spend.py:157-194 wrapper pattern (pre-check the cap, record usage
# after), with ocr.py's per-SDK image shapes as the dispatch reference. Factored
# as ONE helper so it can merge with ocr.py's once #29 lands.
# ---------------------------------------------------------------------------
def _vision_live(provider: str | None) -> bool:
    """Whether a REAL vision-capable engine is configured (own copy of the
    ocr.is_vision_capable predicate so this module never imports ocr.py)."""
    provider = (provider or config.DEFAULT_PROVIDER or "").lower()
    return bool(
        (provider == "local" and config.LOCAL_BASE_URL)
        or (provider == "openai" and config.OPENAI_API_KEY)
        or (provider == "claude" and config.ANTHROPIC_API_KEY)
        or (provider == "gemini" and config.GEMINI_API_KEY)
    )


# ---------------------------------------------------------------------------
# Typesetting: fit-to-box (research 03)
# ---------------------------------------------------------------------------
def _is_cjk(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if (
            0x2E80 <= cp <= 0x9FFF
            or 0x3040 <= cp <= 0x30FF
            or 0xAC00 <= cp <= 0xD7AF
            or 0xF900 <= cp <= 0xFAFF
            or 0xFF00 <= cp <= 0xFFEF
        ):
            return True
    return False


def _line_width(font, line: str) -> int:
    bbox = font.getbbox(line)
    return bbox[2] - bbox[0]


def _wrap(text: str, font, max_w: int) -> list[str] | None:
    """Wrap text to max_w using getbbox measuring (never per-char sums —
    kerning). Word-wrap for space-delimited scripts, char-wrap for CJK; an
    overlong single word char-wraps as a fallback. Returns None when even a
    single character exceeds max_w (caller shrinks the font)."""
    text = " ".join(text.split())
    if not text:
        return [""]
    cjk = _is_cjk(text)
    lines: list[str] = []
    if cjk:
        cur = ""
        for ch in text:
            cand = cur + ch
            if cur and _line_width(font, cand) > max_w:
                lines.append(cur)
                cur = ch
            else:
                cur = cand
        if cur:
            lines.append(cur)
    else:
        cur = ""
        for word in text.split(" "):
            cand = (cur + " " + word) if cur else word
            if cur and _line_width(font, cand) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = cand
            # An overlong single word: char-wrap it.
            while _line_width(font, cur) > max_w and len(cur) > 1:
                cut = len(cur)
                while cut > 1 and _line_width(font, cur[:cut]) > max_w:
                    cut -= 1
                lines.append(cur[:cut])
                cur = cur[cut:]
        if cur:
            lines.append(cur)
    for ln in lines:
        if _line_width(font, ln) > max_w:
            return None
    return lines


def _luminance(color: tuple) -> float:
    if len(color) >= 3:
        r, g, b = color[0], color[1], color[2]
    else:
        r = g = b = color[0]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _segments_per_unit(doc) -> list[int]:
    """How many Segments each skeleton unit produced (the SAME alignment math
    export.paragraph_texts_for uses). Thin wrapper over the shared
    segment.segments_per_unit so both re-split paths stay identical."""
    from app.services.segment import segments_per_unit

    return segments_per_unit(doc)


def stamp_region_meta(session, doc) -> int:
    """Write each image Segment's region geometry into Segment.meta (the
    shipped-but-unused JSON column — no migration). One region may span several
    sentence segments; each carries {region, part_index, part_count}. Returns
    the number of segments stamped."""
    from sqlmodel import select

    from app.models import Segment

    skel = doc.skeleton or {}
    regions = skel.get("regions")
    if not regions:
        return 0
    counts = _segments_per_unit(doc)
    segs = session.exec(select(Segment).where(Segment.document_id == doc.id)).all()
    by_locale: dict[str, list] = {}
    for s in segs:
        by_locale.setdefault(s.target_locale, []).append(s)
    stamped = 0
    for locale_segs in by_locale.values():
        locale_segs.sort(key=lambda s: s.order_index)
        i = 0
        for u_idx, n in enumerate(counts):
            region = regions[u_idx] if u_idx < len(regions) else None
            for part in range(n):
                if i >= len(locale_segs):
                    break
                if region is not None:
                    locale_segs[i].meta = {
                        "image_region": region,
                        "part_index": part,
                        "part_count": n,
                    }
                    session.add(locale_segs[i])
                    stamped += 1
                i += 1
    session.commit()
    return stamped
