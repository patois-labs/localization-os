# Build the optional native module (`locos_native`)

Why it exists: ADR 0091. How it works: `docs/architecture/native-module.md`.

`locos_native` is optional. It is a Rust (PyO3) twin of four pure text functions (TM normalize and hash, tag damage,
the built-in sentence splitter). Without it the app runs the Python functions, byte for byte the same, only slower on
those paths.

## Docker

The image builds it by default (the `native` stage of the `Dockerfile`, against the same Python as the runtime).
`--build-arg WITH_NATIVE=0` builds an image without it that behaves the same. Nothing else to do.

## A source checkout (Linux, macOS, Windows)

1. Install Rust with [rustup](https://rustup.rs) (on Windows the default MSVC toolchain, which needs the Visual
   Studio C++ build tools).
2. Install maturin in a virtualenv of its own, not in the app's venv:

   ```bash
   python -m venv ~/.venvs/maturin
   ~/.venvs/maturin/bin/python -m pip install maturin      # Windows: ~\.venvs\maturin\Scripts\python.exe
   ```

3. Build against the SAME Python the app runs with. The module reads CPython's own string storage, so it is built
   per CPython minor version (`-i` only reads the interpreter's version; nothing is installed into it):

   ```bash
   cd native/locos_native
   ~/.venvs/maturin/bin/maturin build --release -i ../../.venv/bin/python   # Windows: ..\..\.venv\Scripts\python.exe
   ```

   The wheel lands in `native/locos_native/target/wheels/` (`target/` is git-ignored).

4. Use it. Either install the wheel into the app's venv:

   ```bash
   .venv/bin/python -m pip install native/locos_native/target/wheels/locos_native-*.whl
   ```

   or unzip the wheel into a folder of its own and put that folder on `PYTHONPATH` for the app process. Removing the
   variable turns it off again.

## Check it

```bash
.venv/bin/python -c "from app.services import native, quality_common, segment, tm_common; print(native.status())"
.venv/bin/python -m pytest tests/test_verify_suites.py -k verify_native_parity
```

`status()` shows `native` for each function when the module is in use, or `python: <reason>` (`not installed`,
`stale stamp`, `unicode ...`, `disabled by LOCOS_NATIVE`). The parity suite skips its parity half when the module is
absent and compares 20,000 generated inputs per function when it is present.

## After changing a Python reference

Editing `tm_common.normalize` / `hash_text`, `quality_common.tag_damage`, the splitter core
`segment._legacy_split_py`, or anything they read (the list is `app/services/native.py::SPECS`) switches the native
twin off, with a warning, until the Rust side matches again:

1. Port the change to `native/locos_native/src/`.
2. Re-stamp: `python native/locos_native/tools/gen_tables.py --stamps`.
3. Rebuild, then run `verify_native_parity` with the new build.

After a Python minor upgrade, rebuild; if `unicodedata.unidata_version` changed too, regenerate the tables first
(`gen_tables.py` without `--stamps`). Rules for moving more functions: `.claude/rules/native-module.md`.

## Turn it off

Set `LOCOS_NATIVE=0` (or `off`) for the process, or uninstall the wheel / drop the `PYTHONPATH` entry. The app then
runs the Python functions.
