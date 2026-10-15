# Bundled fonts (Image Localization module)

The raster typesetting pipeline (`app/services/image_localization.py`) draws
translated text with a **bundled** font so rendering is deterministic and identical
on every machine (dev, CI-Linux, a demo server) - a system font would make the
hermetic suite non-portable. This implements the approved D5 decision
(SPEC-image-localization §7): bundle Noto Sans + a CJK subset.

## Contents

| File | What | License |
|---|---|---|
| `NotoSans-Regular.ttf` | Noto Sans (Latin/Greek/Cyrillic) - the default face | SIL OFL 1.1 (`OFL.txt`) |
| `NotoSansCJK-Regular.otf` | Noto Sans SC subset (unified CJK coverage for zh; ja/ko render via fallback) | SIL OFL 1.1 (`LICENSE-NotoCJK.txt`) |

Both were fetched from the official sources: `github.com/notofonts/notofonts.github.io`
(hinted NotoSans TTF) and `github.com/notofonts/noto-cjk` (SubsetOTF/SC). SIL OFL is
commercial-safe: bundling with the app is explicitly permitted (the fonts must keep
their license files alongside - hence `OFL.txt` / `LICENSE-NotoCJK.txt`).

## Loader behavior

`image_localization._font_path(text)` picks `NotoSansCJK-Regular.otf` when the text
contains CJK codepoints, else `NotoSans-Regular.ttf`. If a bundled file is missing
(a trimmed install), the loader degrades to Pillow's built-in bitmap font and the
render carries a quality flag - it never fails the pipeline.

Full Noto (all ~150 scripts, JP/KR-preferred CJK variants) stays an optional
manual download per D5; drop additional faces in this directory and extend
`_FONT_FILES` in the service.
