# 0111. Bilingua is the LocOS parsing engine, per format behind a setting

Status: Accepted
Date: 2026-11-07, amended 2026-11-09

## Context

LocOS has about 100 Python filters with many format-specific stamps (`read_gen`, `docx_walk`, `text_bom`,
`xliff_tokens`, ...). Bilingua is the open source Rust parsing engine (`patois-labs/bilingua`, Apache-2.0, PyO3 wheel `bilingua`). It has a `locos` compatibility profile so each format can behave as the LocOS filter does.
The decision was to make it the core of LocOS file parsing, behind a per-format setting so any format can be
switched back.

## Decision

- **Core, not an add-on (ADR 0089).** The maintainer places Bilingua and its adapter in the Apache 2.0 core.

- **Per format, behind a setting.** The setting `bilingua_formats` (env `LOC_TMS_BILINGUA_FORMATS` overrides it)
  lists the LocOS canonical formats whose NEW imports are parsed by Bilingua. `get_filter` stays the only door;
  `BilinguaFilter` (the adapter) implements the `Filter` interface on top of the wheel, with `profile="locos"`.
- **A document keeps the engine it was imported with.** A skeleton carrying the `bilingua` stamp is always read by
  Bilingua (even after the switch is turned off); any other skeleton stays on the Python filter. The stamp holds the
  profile name and version, schema, segment ids, a units hash and the sha256 of the original. Profile name, profile
  version and schema must match at build; the crate version is informational as long as the fresh extract gives
  the same segment ids and units hash. Otherwise export refuses with "re-import the file", never a 500.
- **Tokens are numbered by Bilingua, not by the adapter** (`to_locos_text` in the wheel), so the token text matches
  the Python filter's and TM leverage is kept.
- **A shadow mode** (`bilingua_shadow_formats`) parses an upload with Bilingua in the background after the Python
  parse and logs only counts and hashes; nothing stored changes.
- **Packaging.** The Dockerfile has a `bilingua` stage modelled on `native` (ADR 0091): `ARG WITH_BILINGUA=0`
  (default), wheel built from source with a hash-pinned maturin (its own lock,
  `scripts/locks/requirements-maturin.lock`), the wheel's RECORD hashes checked before it is unpacked, source from
  a Docker named build context (`--build-context bilingua-src=<dir>`) or a checksum-pinned tarball URL, smoke check
  in the stage.

## Amendment 2026-11-09: on by default

Bilingua handles every format by default.
- `bilingua_formats` defaults to `all`: every format in `bilingua_adapter._FORMATS` (85 at the time), so a format
  the adapter learns later is on too. `all` stands alone in the list. Empty is the kill switch.
- The wheel comes from PyPI: `bilingua==0.9.1` in requirements.txt and hash-pinned in requirements.lock, with the
  marker `platform_machine == "x86_64" or platform_machine == "AMD64"` because only those wheels are published.
  Elsewhere the package is skipped and every format stays on its Python filter (`available()` is False). The
  Dockerfile's source stage stays as the from-source alternative.
- The Python filters stay: they read every document imported before the switch and run on machines without the
  wheel. Suites that test their machinery build their documents with the switch off
  (`tests._bootstrap.bilingua_switch_off`).

## Consequences

- A document imported before the switch keeps its Python filter; only new imports use Bilingua.
- A Bilingua release must never move the units of a stored document under the same profile version.
- On a machine with no wheel (any architecture other than x86_64 and AMD64) the setting changes nothing.
- The from-source wheel build needs cargo and network access to crates.io during `docker build` (like `native`).
  maturin has its own lock, so no other build carries it. The build-context source is trusted as the operator
  gives it; the tarball path is checksum-pinned.

## Links

- `docs/runbooks/bilingua-wheel.md`, ADR 0091, `.claude/rules/formats-and-tokens.md`
