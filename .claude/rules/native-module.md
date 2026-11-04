---
paths:
  - "native/**"
  - "app/services/native.py"
  - "app/services/tm_common.py"
  - "app/services/segment.py"
  - "app/services/quality_common.py"
  - "app/services/ai_tagging.py"
  - "tests/verify_native_parity.py"
  - "tests/arch_targets/**"
---

# Native (Rust) hot paths: when a function may move, and how not to break the twins

Why: ADR 0091 (`docs/decisions/0091-native-module.md`). How it works:
`docs/architecture/native-module.md`. Build: `docs/guides/native-module.md`.

A function may move to Rust only when ALL of these hold:

- **Pure.** No DB, settings, files, network, clock, randomness or globals that change at run time. A pure core
  can be split out first (the splitter keeps its `_srx_enabled()` settings read in Python; only
  `segment._legacy_split_py` is native).
- **CPU-bound and measured.** Profile it under a real workload first and record the number. Measure what the time
  is actually spent on: `split_sentences` looked like a 400 us regex cost and was a settings read.
  Sub-microsecond functions gain little (call overhead), and hash_text is bound by SHA-256.
- **Parity-tested.** A section in `tests/verify_native_parity.py` with 20,000 generated inputs that hit every
  branch (whitespace kinds, tokens, Unicode classes, window edges), plus fallback cases; then a mutation check
  (break one rule in a scratch copy, the suite must go red).
- **Python fallback.** The Python function stays as `<name>_py`, is rebound through `native.bind(name, ref)`, and is
  listed with everything it reads in `app/services/native.py::SPECS`.

Landmines:

- Editing a reference (or a helper/pattern in its `SPECS` entry, even a docstring inside it) switches its native
  twin OFF with a warning until the Rust side is ported and re-stamped
  (`native/locos_native/tools/gen_tables.py --stamps`). That is the design; do not "fix" it by re-stamping without
  porting. `verify_native_parity` fails while a present module has a stale stamp.
- Never use Rust's own Unicode classes (`char::is_whitespace`, `is_alphanumeric`, regex `\w`): use `text.rs`,
  which reads tables measured from Python `re`. Python indexes by code point; the crate works on code points too.
- Plain `str`/None only on the fast path; forward everything else to the reference so exceptions match.
- Tests that monkeypatch a helper a native function reads (e.g. `segment.find_placeholders`) do not affect the
  native path: patch the public name, or run with `LOCOS_NATIVE=0`.
- Never install the wheel or maturin into a checkout's `.venv` that serves a running install; build in your own worktree.
