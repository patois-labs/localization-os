# Runbook: the Bilingua wheel, the image and the format switch

Why: ADR 0111 (`docs/decisions/0111-bilingua-parsing-engine.md`).

Bilingua is the Rust parsing engine (PyO3, module `bilingua`, one `abi3` wheel for Python 3.11+). It is installed
and on by default: `bilingua_formats` is `all`. Without the module (a machine with no wheel), Localization OS runs
its Python filters unchanged. Documents that Bilingua imported need the module to export.

## 1. Where the wheel comes from

`requirements.txt` pins `bilingua==0.9.1` and `requirements.lock` hash-pins it, both with the marker
`platform_machine == "x86_64" or platform_machine == "AMD64"`. The published wheels are manylinux 2_28 x86_64 and
Windows x64. So `pip install -r requirements.lock` installs it on those machines and skips it on any other (for
example aarch64 Linux or Apple silicon), where every format stays on its Python filter.

Check an install: `python -c "import bilingua; print(bilingua.__version__)"`. In the app, the Settings page (File parsing card) shows
whether the module is installed.

## 2. The Docker image

The normal image installs the wheel from `requirements.lock` and its last step imports `bilingua` on x86_64, so a
build without the engine fails.

The Dockerfile also has a `bilingua` stage that builds the wheel from source, like the `native` stage (ADR 0091).
Default `WITH_BILINGUA=0`: the stage makes an empty folder. With `WITH_BILINGUA=1` the built wheel is unpacked over
the locked one. Give it the source in ONE of two ways (both set, or neither, fails the build).

A. A build context folder, the Bilingua workspace root (Cargo.toml and Cargo.lock inside it). The build trusts it as
   given, so point it only at a clean, reviewed checkout at a known commit.

   ```
   docker build --build-arg WITH_BILINGUA=1 --build-context bilingua-src=../bilingua \
       --build-arg RELEASE=... --build-arg RELEASE_COMMIT=... -t localizationos:bilingua .
   ```

B. A pinned tarball:

   ```
   docker build --build-arg WITH_BILINGUA=1 \
       --build-arg BILINGUA_SRC_URL=https://github.com/patois-labs/bilingua/archive/refs/tags/v0.9.1.tar.gz \
       --build-arg BILINGUA_SRC_SHA256=<64 hex of that tarball> ...
   ```

   Get the checksum once with `curl -L <url> | sha256sum` and review it; the build checks it before unpacking.

Other build args: `BILINGUA_MANIFEST` (path of `bilingua-py/Cargo.toml` inside the source; default
`bilingua-py/Cargo.toml`) and `BILINGUA_LOCOS_PROFILE` (the expected `locos` profile version; the smoke check fails
on a different one).

Notes:
- cargo fetches crates from crates.io (pinned by the source's Cargo.lock, `--locked`). Expect a long release build.
- The built wheel is unpacked, not pip-installed (it has no published hash). `scripts/bilingua_stage.py unpack`
  first checks its RECORD the way pip does: every member listed with a matching sha256 and size, nothing unlisted or
  missing, no `.data` directory, no unsafe names.
- maturin comes from its own lock, `scripts/locks/requirements-maturin.lock`, installed with `--require-hashes
  --only-binary`. No other build reads it.
- The stage ends with `scripts/bilingua_stage.py smoke` (import, `locos` profile present, an untouched merge of one
  md, json and po sample returns the original bytes).

## 3. The format switch

1. `bilingua_formats` is an admin setting (settings page, `loc config set`, the API). The environment variable
   `LOC_TMS_BILINGUA_FORMATS` overrides it. Values: `all` (the default), a comma list of Localization OS format
   names such as `md,json,po`, or blank. An unknown name or a list of more than 256 names is refused.
2. Only NEW imports use Bilingua. A document keeps the engine that imported it, so changing the list never moves
   an existing document.
3. To switch back to the Python filters, set it to blank. Documents already imported by Bilingua keep needing the
   wheel to export.
4. `bilingua_shadow_formats` parses an upload with Bilingua in the background and logs only counts and hashes;
   nothing stored changes.
5. Before an upgrade that would drop the module (an image without it), check that no stored document carries a
   `bilingua` skeleton stamp.
