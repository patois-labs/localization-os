# Native Module: Architecture (L1)

## 1. Role

This subsystem is **the optional Rust module `locos_native` and the loader that decides, function by
function, whether the app runs the Rust version or the Python one**. It exists because a few pure,
CPU-bound text functions run once or more per segment, and the decision for the
engine redesign was Python plus Rust inside the Python app (PyO3), not a rewrite (ADR 0091).

Four functions have a native twin today: TM whitespace normalization and the exact-match hash
(`app/services/tm_common.py::normalize_py`, `app/services/tm_common.py::hash_text_py`), the
inline-tag damage verdict (`app/services/quality_common.py::tag_damage_py`), and the built-in
sentence splitter core, versions 1 and 2 (`app/services/segment.py::_legacy_split_py`). Each module
keeps its Python function as the reference under that `*_py` name and rebinds the public name at the
bottom of the module through `app/services/native.py::bind`. Everything that calls the public name
(TM lookup and write-back, the write gate, export, ingest) gets the native version with no change of
its own.

The module is optional. An install without it (no Rust toolchain, a platform without a build, or
`LOCOS_NATIVE=0`) runs the Python functions exactly as before. The Docker image builds and ships it
(the `native` stage of the `Dockerfile`); a Windows developer builds it by hand
(`docs/guides/native-module.md`); CI does not build it yet.

It does not own what the four functions mean: segmentation (segmentation), TM matching
(tm-and-glossary) and the tag gate (inline-tags, quality-lqa-evaluations) stay the source of truth,
and every behaviour change is made in the Python reference first. It does not cover the SRX engine
or `app/services/segment.py::split_sentences` itself (the settings read and SRX routing stay in
Python; only the regex splitter behind it is native).

## 2. Invariants

- **Byte parity or Python.** A native function returns exactly what its reference returns, and the
  same exception type and message. Plain `str` or None arguments take the native path; anything else
  (a `str` subclass, a lone surrogate where the reference raises, other types, keyword or odd-arity
  calls) is forwarded unchanged to the registered reference. Enforced by `verify_native_parity`
  (20,000 generated inputs per function plus fallback cases).
- **A changed reference switches its twin off.** `app/services/native.py::fingerprint` hashes the
  source of each reference and of every helper, constant and pattern it reads (listed in
  `app/services/native.py::SPECS`); the module carries the stamps it was built for, and
  `app/services/native.py::bind` keeps the Python function, with a warning, on any mismatch. Editing
  `_ABBREV_RULES` or `find_placeholders` therefore can never leave a stale Rust copy in charge.
  Enforced by `verify_native_parity` (stale-stamp guard).
- **Python's character classes, not Rust's.** `\w`, `\s`, `\d`, `str.split`/`strip` whitespace and
  the `(?i)` letter sets are measured from this Python's `re` into generated tables
  (`native/locos_native/tools/gen_tables.py`); the loader refuses a module whose tables came from a
  different `unicodedata` version. Enforced by `verify_native_parity` (Unicode guard).
- **Absent means unchanged.** Without the module every rebound name IS its reference. Enforced by
  `verify_native_parity` (absent and kill-switch checks).
- **The image builds it against its own Python.** The `native` stage uses the same `PYTHON_BASE` as
  the runtime stage and checks the Unicode tables before copying the module. Enforced by
  `verify_docker_image` (stage and base-pin mutations).

## 3. Where the work happens

| Concern | Pin | One line |
|---|---|---|
| Loader and fallback | `app/services/native.py::bind` | Returns the native function only when loaded, Unicode-matched and stamped for this exact reference. |
| Reference inventory | `app/services/native.py::SPECS` | Per native function: the Python reference and everything it reads (the fingerprint's input). |
| Diagnostics | `app/services/native.py::status` | What the loader decided per function, and why. |
| Rust crate | `native/locos_native/src/lib.rs` | CPython entry points (fast path, forwarding to the reference). |
| Generated tables | `native/locos_native/tools/gen_tables.py` | Writes the character tables and the reference stamps. |

## 4. Data & settings

- **Models:** none.
- **Settings:** none. The environment variable `LOCOS_NATIVE=0` (also `off`, `false`, `no`) keeps
  every Python function; it is read once at import.
- **Build:** `native/locos_native/Cargo.lock` pins every crate (PyO3 for the bindings, ring for
  SHA-256). The module is NOT abi3: it reads CPython's internal string storage (PEP 393) directly,
  so it is built per CPython minor version.

## 5. Enforced by

- `verify_native_parity` - parity on generated inputs, fallback and exceptions, the stamp, Unicode and
  kill-switch guards, and the public splitter with and without the module. Skips its parity half
  cleanly when the module is absent.
- `verify_docker_image` - the `native` and `rust` stages, the `RUST_BASE` pin, and the crate sources
  in the build-context allow-list.
- `verify_segment_abbrev`, `verify_srx` - the splitter's behaviour, whichever implementation runs.

## 6. Battle scars

- **The first target was self-defeating.** Target T8 timed `tm_common.normalize` against the native
  function while its own check 4 required that name to BE the native function, so its speed check read
  1.0x. The Python side now uses the `*_py` references (S5 sprint, 2026-10-22).
- **`split_sentences` looked slow for a non-regex reason.** Called in a loop, about 400 us of each
  call is `app/services/segment.py::_srx_enabled` reading a setting; the regex splitter is about
  15 us. A real ingest reads that setting once per document, so the job never paid it; the first
  target's baseline did.
- **Small in a real job.** Profiling a 1,902-segment HTML job showed the four functions at about 1.4%
  of CPU (SQLAlchemy dominates), so the native module is a per-call win, not a job-time win, until the
  query work (sprint S4) lands. ADR 0091 lists what was measured next.
- **The first Rust build was barely faster.** UTF-8 round trips, PyO3's generic call wrapper and the
  Windows heap allocator cost as much as the work for sub-microsecond functions. The crate now reads
  PEP 393 storage directly, uses raw `METH_FASTCALL` entry points and stack scratch buffers.

## 7. Limits & accepted risks

- **hash_text is bound by SHA-256.** On CPUs without SHA instructions (an i5-10400F desktop) the
  digest is most of the call, so the native version is about 4x, not 5x; on CPUs with them (most
  servers) the gap is larger. Recorded in ADR 0091.
- **One build per Python minor version and platform.** No wheels are published yet (CI minutes are
  out); an install whose Python differs from the build simply runs Python.
- **Parity is proven on generated inputs, not exhaustively.** The generator covers every Python
  whitespace character, the token grammar (including non-ASCII digits and malformed tokens), the
  masking sentinels and every abbreviation rule; a mutation check during the S5 gate showed it
  catches single-rule and single-character changes.
