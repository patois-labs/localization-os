# TBX-Basic (DCA) schema files - provenance

Localization OS does **not** ship the official TBX-Basic v3 (DCA) schema files. The upstream
LTAC-Global repositories publish no licence, so they are not redistributed here. Strict TBX
validation (`strict=true` on a TBX import) needs two of them; normal TBX import and export work
without them, using the built-in picklists in `app/services/tbx_conformance.py`.

## What strict validation needs

| File | Upstream repo | Path | Ref (commit / tag) | Bytes | sha256 |
|---|---|---|---|---|---|
| `TBXcoreStructV03.rng` | `LTAC-Global/TBX_Core_RNG` | `TBXcoreStructV03.rng` | master `14eb02ba5e16ea4f6be4acd142ce4b47f39b1eb0` | 24369 | `e91e89f82e69fe42f3bdad6f2cd8ab2dbdcdd73c7bd63ab2bfa417262054e461` |
| `TBX-Basic_DCA.sch` | `LTAC-Global/TBX-Basic_dialect` | `DCA/TBX-Basic_DCA.sch` | master `317fb299eac50176549e0bb360370cb49e53faf7` (release `v1.2.1`) | 10874 | `0584301b1afdd855e6a325d6e6945dc86a86f1dce265e61ed362227a8149907f` |

Source URLs (raw):

- https://raw.githubusercontent.com/LTAC-Global/TBX_Core_RNG/master/TBXcoreStructV03.rng
- https://raw.githubusercontent.com/LTAC-Global/TBX-Basic_dialect/master/DCA/TBX-Basic_DCA.sch

## How to install them

Run `python scripts/fetch_tbx_schemas.py`. It downloads both files, checks the size and sha256
above, and writes them to `app/resources/tbx/` (git-ignored for these names). The product looks
for them, first hit wins, in: the folder named by `LOC_TMS_TBX_SCHEMA_DIR`, `<data dir>/tbx/`, then
`app/resources/tbx/`. The product never fetches them at runtime. Without them a strict request is
refused with a message naming the missing file and these URLs.

`los-ext-v1.xcs` in this folder is Localization OS's own authored fragment (not third-party).

## Why the evaluator is in-repo

The DCA Schematron declares `queryBinding="xslt2"`. lxml's ISO Schematron refuses it, and the
only Python XSLT-2 engine (`saxonche`, about 40 MB, MPL-2.0) is not acceptable as a product
dependency. So `app/services/tbx_conformance.py` is a faithful Python evaluator of that `.sch`: it
parses every `<rule context=...>`/`<assert test=...>` and evaluates each with lxml XPath 1.0, with
two hand-translations for constructs XPath 1.0 cannot run verbatim. The conformance suite asserts
the evaluator's assert count equals the number of `<assert>` elements in the file (when installed),
so an upstream change that adds a rule fails the build until it is translated.

The upstream layout is modular: TBX-Basic v3 (DCA) is the TBX core structure RNG plus the dialect's
DCA Schematron (which carries the level constraints and the picklists). The dialect's module RNGs
are DCT-style grammars and are not used for DCA validation.
