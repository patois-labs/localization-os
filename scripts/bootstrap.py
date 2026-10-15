"""One-command developer setup for any OS.

    python scripts/bootstrap.py            # create .venv, install pinned deps, create .env
    python scripts/bootstrap.py --no-dev   # runtime dependencies only
    python scripts/bootstrap.py --recreate # delete and rebuild .venv first

What it does, in order:

1. Creates `.venv` in the repository root (skipped when it already exists).
2. Installs the hash-pinned locks: `requirements.lock` plus, unless `--no-dev`,
   `requirements-dev.lock` (`pip install --require-hashes`), so what you run is what CI runs.
3. Copies `.env.example` to `.env` when `.env` does not exist (an existing `.env` is never
   touched).
4. Prints the commands to start the app and run the tests.

It needs only the Python standard library, Python 3.10 or newer, and network access to a
package index. It never touches the database or any data directory.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 10)


def venv_python(root=ROOT):
    """The interpreter inside `<root>/.venv`, whichever OS laid it out."""
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def lock_files(dev=True):
    names = ["requirements.lock"] + (["requirements-dev.lock"] if dev else [])
    return names


def install_command(python, dev=True):
    """The pip command line (a list) that installs the hash-pinned locks."""
    command = [str(python), "-m", "pip", "install", "--require-hashes"]
    for name in lock_files(dev):
        command += ["-r", name]
    return command


def ensure_env_file(root=ROOT):
    """Copy .env.example to .env when .env is missing. Returns 'created', 'exists' or 'missing'."""
    target, example = root / ".env", root / ".env.example"
    if target.exists():
        return "exists"
    if not example.is_file():
        return "missing"
    shutil.copyfile(example, target)
    return "created"


def next_steps(python_rel):
    activate = r".venv\Scripts\activate" if os.name == "nt" else "source .venv/bin/activate"
    return [
        "Setup complete. Next:",
        f"  1. Activate the environment:   {activate}",
        "  2. Start the app:              python run.py     (http://127.0.0.1:8000)",
        "  3. Try it offline:             the default engine in .env is 'mock' (no API key needed)",
        "  4. Run the fast checks:        python -m pytest tests/test_verify_suites.py "
        '-k "verify_ruff or verify_branding"',
        "  5. Pick suites for a change:   python scripts/suite_owners.py --files <changed files>",
        f"     (without activating, use {python_rel} in place of python)",
    ]


def run(command, cwd):
    print("+ " + " ".join(command))
    subprocess.run(command, cwd=str(cwd), check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-dev", action="store_true", help="skip the dev/test toolchain")
    parser.add_argument("--recreate", action="store_true", help="delete and rebuild .venv")
    args = parser.parse_args(argv)

    if sys.version_info < MIN_PYTHON:
        print("Python {}.{} or newer is required.".format(*MIN_PYTHON), file=sys.stderr)
        return 1

    venv_dir = ROOT / ".venv"
    if args.recreate and venv_dir.exists():
        print("Removing the existing .venv")
        shutil.rmtree(venv_dir)
    python = venv_python()
    if not python.exists():
        run([sys.executable, "-m", "venv", str(venv_dir)], ROOT)
    else:
        print(".venv already exists; reusing it")

    try:
        run([str(python), "-m", "pip", "install", "--upgrade", "pip"], ROOT)
        run(install_command(python, dev=not args.no_dev), ROOT)
    except subprocess.CalledProcessError as error:
        print(f"Install failed (exit {error.returncode}). Fix the error above and rerun.")
        return error.returncode or 1

    status = ensure_env_file()
    messages = {
        "created": "Created .env from .env.example",
        "exists": ".env already exists; left untouched",
        "missing": ".env.example not found; skipped creating .env",
    }
    print(messages[status])
    print()
    print("\n".join(next_steps(os.path.relpath(python, ROOT))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
