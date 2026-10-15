"""Generate a keyless-demo image for the Images (Beta) module.

Draws a marketing-banner-style PNG with known text at known coordinates AND
embeds the region sidecar (PNG tEXt "loc_tms_regions") that the module's
deterministic MockDetector reads — so the WHOLE pipeline (detect -> translate
-> overlay render -> export) runs offline with zero OCR engines and zero keys.
This is the same mechanism the hermetic suite uses; nothing here touches the
app or any database.

Since issue #30 the sidecar is honored ONLY when the resolved detector is the
mock (env=mock, or no OCR engine configured or importable). On an install where
a real engine (winocr/rapidocr) is configured or auto-detected, the sidecar is
ignored and the pixels are OCR'd instead, so run this demo keyless (or set
LOC_TMS_IMAGE_DETECTOR=mock) to see the embedded regions drive the pipeline.

Usage:
    python scripts/make_demo_image.py [-o demo_banner.png]

Then upload the file on the /images page (or `loc-tms image upload <project> <file>`).
"""

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

FONT = os.path.join(_ROOT, "assets", "fonts", "NotoSans-Regular.ttf")

REGIONS = [
    {"text": "Summer Sale", "bbox": [60, 40, 520, 90], "size": 56},
    {"text": "Up to 50 percent off everything", "bbox": [60, 160, 520, 50], "size": 28},
    {"text": "Shop now", "bbox": [60, 260, 220, 60], "size": 30},
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="make_demo_image")
    ap.add_argument("-o", "--out", default="demo_banner.png")
    args = ap.parse_args(argv)

    im = Image.new("RGB", (640, 380), (233, 240, 248))
    draw = ImageDraw.Draw(im)
    draw.rectangle([40, 240, 300, 340], fill=(46, 105, 180))
    sidecar = []
    for r in REGIONS:
        x, y, _w, _h = r["bbox"]
        font = (
            ImageFont.truetype(FONT, r["size"])
            if os.path.isfile(FONT)
            else ImageFont.load_default()
        )
        fill = (240, 245, 250) if r["text"] == "Shop now" else (25, 35, 60)
        draw.text((x, y), r["text"], font=font, fill=fill)
        bbox = draw.textbbox((x, y), r["text"], font=font)
        sidecar.append(
            {
                "text": r["text"],
                "bbox": [bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1]],
                "confidence": 0.97,
            }
        )
    meta = PngInfo()
    meta.add_text("loc_tms_regions", json.dumps(sidecar))
    im.save(args.out, format="PNG", pnginfo=meta)
    print(f"wrote {args.out} ({os.path.getsize(args.out)} bytes, {len(sidecar)} regions embedded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
