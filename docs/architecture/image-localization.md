# Image localization (Beta): Architecture (L1)

## 1. Role

This subsystem is the **image translation pipeline**: detect text regions in a raster image,
recognize and translate them, and typeset the translation back into the image, plus the per-string
in-context images that give a translator visual context for a UI key.

Images are documents under projects, so the scoping and picker conventions mirror the document
surfaces. The core (`app/services/image_localization.py`) owns everything image-specific so the
`/images` web routes, the `image` CLI and the `/api/images` routes stay thin callers. Region
geometry always comes from a pluggable local detector (mock, Windows OCR, or RapidOCR), never from
a vision LLM: a VLM is used only to recognize low-confidence region text and to write a one-per-image
context brief, both spend-guarded. Overlay-mode composition erases a region with a border-ring
flat-fill and typesets the translation with bundled Noto fonts, flagging overflow rather than
truncating.

Vision-LLM OCR (`app/services/ocr.py`) is the transcription path for a whole page image through any
vision-capable engine, with a deterministic placeholder when none is configured so ingestion still
works offline. Per-string context images (`app/services/key_images.py`) are the second, separate
feature: the service both the web and CLI call to attach a downscaled screenshot to a string key,
with orphan-free storage ordering (row committed not-ready, blob put, then flipped ready) and a
server-derived blob key so no caller input reaches a path.

It does not own the general document ingest/export (translation-pipeline), the string-key store the
context images attach to (string-keys), the provider seam the VLM calls go through
(engines-and-providers), or the blob storage port itself (kernel-and-ports).

## 2. Invariants

- **Region geometry is always a local detector, never a VLM.**
  `app/services/image_localization.py::detect_regions` resolves a local detector
  (`app/services/image_localization.py::resolve_detector`), and the VLM is used only for
  `app/services/image_localization.py::escalate_recognition` (text) and the context brief. Enforced
  by `verify_image_localization`.
- **A VLM call is spend-guarded and OCR degrades offline.**
  `app/services/ocr.py::ocr_image` applies the same spend brake before a real call and returns a
  deterministic placeholder when no vision engine is configured. Enforced by
  `verify_image_localization`.
- Local vision calls for image localization and whole-page OCR are guarded by
  `app/services/egress.py::validate_local_engine_target` (`egress.validate_local_engine_target`)
  before client construction, enforced by `verify_local_engine_egress`.
- **An image is refused from its header before any decode.**
  `app/services/image_localization.py::open_raster` refuses a side over `MAX_PIXEL_DIM` (8000 px) or
  a total over `MAX_TOTAL_PIXELS` (25 megapixels by default, from `app/config.py::IMAGE_MAX_TOTAL_PIXELS`,
  environment `IMAGE_MAX_TOTAL_PIXELS`), because the per-side guard alone allowed about 256 MB of
  RGBA per upload (#546, #589). Enforced by `verify_image_localization`.
- **A raster opens only through the plugin its magic bytes name, and only as one frame.**
  `app/services/image_localization.py::open_raster` passes Pillow the single format
  `app/services/image_localization_part1.py::sniff_kind` found (PNG, JPEG, TIFF, BMP, GIF, WebP), and
  `app/services/image_localization.py::_check_single_frame` refuses an animated GIF or WebP or a
  multi-page TIFF with `ImageRefusedError` (an `InputLimitError`, so every upload door shows the
  reason) instead of letting the overlay build flatten it. The build writes the source format back.
  Enforced by `verify_image_raster_aliases`.
- **An image with no detected text still ingests; any failed region makes the run FAILED.**
  `app/services/image_localization.py::ingest_image` passes `allow_empty` so a raster with zero regions
  (the honest state with no OCR engine) lands as a document, unlike an empty text file (#573).
  `app/services/image_localization.py::translate_image` records a run in which any region failed as a
  FAILED job and raises `ImageLocalizationError`, which the web route, the `/api` route and the CLI
  each turn into their error answer (#588, #624). It uses the same rule as translation jobs
  (`app/services/jobs.py::run_error`, #612): all regions failed carries the first engine error; a
  partial failure names the failed count and keeps the per-region stats on the result. Regions that
  did translate stay saved, and only a run with no failed region ends COMPLETED. Enforced by
  `verify_image_localization`.
- **Key-image storage is orphan-free and its blob key is server-derived.**
  `app/services/key_images.py::attach_image` commits the row not-ready, puts the blob, then flips
  ready, and derives a flat `keyimage_<uuid>.png` key so no caller input reaches a path. Enforced by
  `verify_key_images`.
- **Key-image tenancy is decided by the caller's proven key, not by this service.**
  `app/services/key_images.py::attach_image` sets `KeyImage.project_id` server-side from the key the
  caller already proved it may write. Enforced by `verify_key_images`.
- **Both features 404 when their kill switch is off.**
  `app/services/image_localization_part1.py::enabled` reads `setting:image_localization_enabled` and
  `app/services/key_images.py::enabled` reads `setting:key_image_context_enabled`; every route 404s
  when off. Enforced by `verify_image_localization` and `verify_key_image_vision`.

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Detection + typesetting | `app/services/image_localization.py::detect_regions` · `app/services/image_localization.py::resolve_detector` · `app/services/image_localization.py::escalate_recognition` · `app/services/image_localization_part1.py::enabled` | The local region detector seam, the geometry normalizer, and overlay composition. |
| Vision-LLM OCR | `app/services/ocr.py::ocr_image` · `app/services/ocr.py::OCR_PROMPT` | Whole-page transcription through any vision engine, spend-guarded, with an offline placeholder. |
| Per-string context images | `app/services/key_images.py::attach_image` · `app/services/key_images.py::validate_and_render` · `app/services/key_images.py::view` | Attach, validate/downscale, and read the in-context screenshots for a string key. |
| Web + /api surface | `app/web/images.py::images_index` · `app/web/images.py::image_translate` · `app/web/images.py::api_images_list` | The `/images` module pages and the read+export `/api/images` parity. |
| CLI surface | `app/cli/cmd_image.py::register` | The `image` command group (upload/translate/render/export). |

## 4. Data & settings

- **Models:** `app/models.py::KeyImage` (a per-key in-context image with its ordinal and ready
  flag); images-as-documents reuse `app/models.py::Document` and `app/models.py::Segment`.
- **Settings:** `setting:image_localization_enabled` (default on, a kill switch) gates the image
  module; `setting:key_image_context_enabled` (default on, a kill switch) gates the per-string
  context images.

## 5. Enforced by

- `verify_image_localization`: the local-detector-only geometry, the spend-guarded VLM, and the
  feature gate.
- `verify_key_images`: the orphan-free storage ordering and the server-derived tenancy.
- `verify_key_image_vision`: the per-string image kill switch and the vision path.

## 6. Battle scars

- **Geometry from a VLM was rejected by design.** A vision model hallucinates coordinates, so
  region geometry is always a local detector and the VLM is confined to recognizing text, recorded
  in the `app/services/image_localization.py` module
  docstring.
- **A crash between the row and the blob must leave a skippable stub, not a dangling blob.** The
  key-image write commits the row not-ready before the blob and flips ready after, recorded in the
  `app/services/key_images.py` module docstring.

## 7. Limits & accepted risks

- **Overlay typesetting flags overflow, it does not truncate.** A translation that does not fit its
  region is flagged rather than clipped, an intentional choice stated in the
  `app/services/image_localization.py` module docstring.
- **The tier is Beta but ships on as kill switches.** Both `setting:image_localization_enabled` and
  `setting:key_image_context_enabled` default on (each is a kill switch, so a stock install keeps the
  surfaces live), stated in the `app/services/image_localization.py` module docstring and the
  `app/services/key_images.py::enabled` docstring; `app/web/images.py` 404s every image route when the localization switch is off.
