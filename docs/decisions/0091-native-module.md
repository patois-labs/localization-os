# 0091. Native hot paths: an optional Rust module with the Python functions as reference and fallback

Status: Accepted
Date: 2026-10-23

## Context

The engine redesign is Python plus Rust inside the Python app (PyO3), not a rewrite: move measured, pure, CPU-bound hot paths to Rust and keep Python as the reference and the fallback. The
module ships in the Linux Docker image and local builds first; prebuilt wheels may follow; installs
without it must keep working unchanged. Stage S5, target T8. First functions: `tm_common.normalize`, `tm_common.hash_text`,
`quality_common.tag_damage`, and the built-in sentence splitter.

The risk is drift: a Rust copy that quietly disagrees with Python moves segment boundaries, TM hashes and tag
verdicts. Python `re` and Rust also disagree on Unicode classes (`\w` and combining marks, `\s` and U+001C..U+001F,
`(?i)i` and U+0130/U+0131).

## Decision

- **Module and loader.** Crate `native/locos_native` (PyO3 0.29) builds the extension `locos_native`.
  `app/services/native.py::bind` rebinds a public name to the native function at the bottom of its module, and only
  when: the module imports, `LOCOS_NATIVE` is not `0`/`off`, its Unicode tables match this Python's
  `unicodedata.unidata_version`, and its compiled stamp equals the fingerprint of the Python reference (the source of
  the reference and every helper, constant and pattern it reads, listed in `native.SPECS`). Otherwise the Python
  function stays, with a warning when the module is present but refused.
- **References stay.** Each module keeps its Python function under `<name>_py` (`normalize_py`, `hash_text_py`,
  `tag_damage_py`, `segment._legacy_split_py`). Behaviour changes are made there first; editing one switches its native
  twin off until the Rust side is ported and re-stamped (`gen_tables.py --stamps`). `segment.split_sentences` keeps the
  settings read and SRX routing in Python; only the regex splitter behind it (versions 1 and 2) is native.
- **Parity rule.** A native function returns exactly what its reference returns, exceptions included. The fast path
  takes exact `str` or None; anything else (subclasses, lone surrogates where the reference raises, other types,
  keyword or odd-arity calls) is forwarded unchanged to the registered reference. Character classes come from tables
  measured from Python's own `re` (`native/locos_native/tools/gen_tables.py`), never Rust's. `verify_native_parity`
  checks 20,000 generated inputs per function, the fallbacks and the guards, and skips its parity half when the module
  is absent.
- **Performance shape.** Not abi3: the crate reads CPython's PEP 393 string storage directly (no UTF-8 round trip, no
  UTF-8 cache left on the caller's strings) and uses raw `METH_FASTCALL` entry points, because for sub-microsecond
  functions PyO3's generic wrapper and UTF-8 conversion cost as much as the work. SHA-256 uses `ring` (assembly).
- **Packaging.** The Dockerfile gains a `native` stage on the same `PYTHON_BASE` as the runtime, with the toolchain
  copied from a digest-pinned `rust` image (`RUST_BASE`, pinned by `scripts/pin_base_images.py`), `cargo build
  --locked`, and an import plus Unicode check before the `.so` is copied into the venv; `--build-arg WITH_NATIVE=0`
  builds without it. A local checkout builds it by hand with maturin in its own
  tool venv (`docs/guides/native-module.md`). CI is untouched.
- **When a function may move.** Pure, CPU-bound, measured, parity-tested, with a Python fallback:
  `.claude/rules/native-module.md`.

## Consequences

- A default install is byte-identical to before; the module is a speed-up only.
- Measured on T8's inputs, native vs the Python reference (a desktop i5-10400F, and the Linux image on a 16-core test server):
  tag_damage 50 to 100x, splitter core 12 to 15x, normalize about 5.5x (best of 7; T8's single 2 ms timing swings
  3.9 to 6.0x on a busy box), hash_text about 4.3x. hash_text is bound by SHA-256 on CPUs without SHA instructions
  (about 480 of 760 ns on the desktop) and by fixed per-call work elsewhere; normalize competes with CPython's own C
  split/join. So T8's "5x each" check holds for two of four functions; the parity, presence and wiring checks hold.
- Calling the public `segment.split_sentences` in a loop costs about 400 us per call, almost all of it the
  `_srx_enabled()` settings read, against about 15 us for the splitter itself. A real ingest reads that setting once
  per document (profiled: one call for 1,200 splits), so this is not a job cost; T8 now times the splitter core.
- End to end the four functions are small: in a profiled 1,902-segment HTML job (mock MT into two languages, then
  export) they were about 1.4% of 42 s of CPU; SQLAlchemy dominates, as the engine study found. The next measured
  pure hot spot is `ai_tagging.restore` (about 11% of that job: it compiles a fresh regex per call because the
  pattern contains the call's nonce), which is a Python fix before it is a Rust candidate. Fixed in Python
  2026-10-23: the pattern is compiled once with a generic body, and the per-call pattern is kept only for text
  that carries a generated-shape id this call never issued (`verify_ai_tagging_restore_parity`).
- Each Python minor version needs its own build; an install whose Python differs from the build runs Python.
- Every function moved later adds a `SPECS` entry, a parity section and a re-stamp; the rule file keeps the bar.
- 2026-10-24: PyO3 0.27 -> 0.29.x for GHSA-36hh-v3qg-5jq4 (not reachable: no list/tuple iteration).

## Links

- `docs/architecture/native-module.md`, `docs/guides/native-module.md`, `.claude/rules/native-module.md`
