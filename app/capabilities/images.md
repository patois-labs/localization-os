---
module: images
title: Image localization (Beta)
summary: Upload an image and get it back with its text in another language. The pipeline is detect the text regions, translate them, re-render the image - and the middle step is the ordinary document pipeline, so TM, glossary, the refusal guard and workflows all apply to the text in an image for free.
nouns:
  Image document: "An uploaded image (svg / png / jpg / jpeg / tif / tiff / bmp / gif / webp) held as a document in a project. It behaves like any other document, except its content is text regions detected on the picture rather than parsed file text."
  Region: "One detected block of text on the image, with its pixel geometry and a confidence. Each region becomes an ordinary translatable segment, which is what lets TM, glossary and workflows apply to it."
  Detector: "The local component that finds the text and its coordinates. Geometry always comes from a detector, never from a vision model - pixel coordinates are a measurement, not something to ask an LLM to guess."
  Confidence: "How sure the detector is of a region's text. A low-confidence region is the only case a vision model is asked to re-read the text, and even then only the text, never the coordinates."
  Overflow: "The flag raised when a translation is longer than its box. Overflowing text is flagged, never truncated - a clipped word is a defect you cannot see in a thumbnail."
  Typesetting: "The re-render step: each region is erased with a border-matched fill and the translation is fitted to the box with a bundled font."
  Context brief: "One optional, spend-guarded vision read per image that describes what the picture is, so the translation of its text has context."
verbs:
  Add an image: "Web: upload it on the project like any document. CLI: `image upload 3 ./banner.png`. It is ingested as an image document and its text regions are detected on upload."
  See what was detected: "Web: the image's page shows each region, its translation, its confidence and any overflow flag. CLI: `image show 12 --locale es-ES` (defaults to the first target locale), and `image list 3` to see a project's image documents."
  Translate the text: "Web: the Translate control, per locale. CLI: `image translate 12 --locale es-ES`. Each region is translated exactly as a document segment is - through TM, glossary, the refusal guard and any workflow."
  Re-render the image: "Web: the localized preview. CLI: `image render 12 --locale es-ES` reports the per-region fit flags (it writes a file only when you pass -o)."
  Get the file back: "Web: download the localized image. CLI: `image export 12 --locale es-ES`. Image export API: 401 without a token; 400 when locale is missing or blank."
needs:
  - "The image_localization_enabled switch on. With it off every image surface returns not-found (404), the CLI refuses, and the MCP image tools relay the same 404 - the feature is removed, not merely hidden."
  - "A project to hold the image, exactly as for a document."
  - "A text detector. The engine is resolved first: an explicit LOC_TMS_IMAGE_DETECTOR override, else Windows OCR when available, then rapidocr as a blessed opt-in (`pip install rapidocr onnxruntime`). With none of them, detection honestly returns zero regions and says so. An embedded fixture sidecar (the deterministic offline path) feeds the mock detector, but only when the resolution above already lands on mock; a real, configured engine is never overridden by uploaded image bytes."
  - "For low-confidence regions and the one context brief per image: a live, vision-capable engine. Both reads are spend-guarded; with only the mock engine they are skipped."
limits:
  - "Geometry is never asked of an LLM. A vision model only re-reads a low-confidence region's text; the coordinates are always the local detector's measurement."
  - "Text that will not fit its box is flagged as overflow, never shortened or clipped - fix it by editing the translation or accepting a smaller fit, not by hoping the thumbnail hides it."
  - "With no detector available, an image detects zero regions. That is reported plainly rather than pretended to be a blank image."
  - "Exported images are always served as attachments, and an exported SVG additionally under a locked-down CSP - an SVG can carry script, so it is never rendered inline."
  - "Beta. The pipeline is stable but the surface is still small; treat it as an add-on to the document pipeline, which is where the quality machinery actually lives."
connects:
  - module: documents
    how: "An image's regions are ordinary segments, so the whole document pipeline - segment status, QA flags, the editor, the export tag guard - applies to the text in an image for free. This is the middle step of the detect / translate / re-render pipeline."
  - module: workflows
    how: "A region is translated by the same engines and workflows a document is, so a multi-agent workflow (translate, review, LQA) runs over image text unchanged."
  - module: tm
    how: "Region text reads from and writes to translation memory exactly as a document segment does, so repeated on-image strings are leveraged across images and documents alike."
  - module: glossary
    how: "Glossary and do-not-translate checks fire on region text and surface as QA flags on the region, the same as on a document segment."
---

## What this is for

You have a screenshot, a banner, a diagram, or a UI mock with text baked into the
pixels, and you need it in another language. Image localization detects the text,
translates it through the same machinery that translates your documents, and
re-renders the picture with the translated text fitted back into place.

The important idea is that the **middle step is the ordinary document pipeline**.
The image is only a container: once a region's text is a segment, every quality
feature you already rely on - translation memory, glossary enforcement, the refusal
guard, a review workflow - applies to it with nothing to wire up.

## The normal path

1. **Upload** the image into a project (`image upload 3 ./banner.png`). Its text
   regions are detected immediately.
2. **Review** what was detected - regions, confidence, and (after translating) any
   overflow flags - with `image show 12 --locale es-ES`.
3. **Translate** into a target locale (`image translate 12 --locale es-ES`). Each
   region goes through TM, glossary and any workflow, exactly as a document segment.
   If any region fails the run ends FAILED (same rule as document translation): the web page,
   `/api/images/{id}/translate`, the CLI (non-zero exit) and MCP report the error, the per-region
   counts stay on the job result, and the regions that did translate stay saved. Run translate
   again to pick up only the missing ones.
4. **Render** to see the localized image and its fit flags, then **export** the file.

## Geometry versus text

Two different questions get two different answers on purpose. *Where is the text?*
is a measurement, so it always comes from a local detector - never from a vision
model, which would only guess at coordinates. *What does the text say?* is the one
place a vision model helps, and only for a **low-confidence** region, and only for
the words, never the box.

## Current warts worth knowing

- If no detector is available, an image honestly detects **zero regions** and says
  so, rather than pretending the image had no text.
- A translation longer than its box is **flagged as overflow, never truncated** -
  the fix is a shorter translation or an accepted smaller fit.
- An exported SVG is served as an attachment under a locked-down CSP, because an SVG
  can carry script; it is never rendered inline.
- Beta: the pipeline is solid, but treat this as an extension of the document
  pipeline, which is where the depth is.
