"""scripts/bootstrap.py: the one-command setup builds the right commands and never clobbers .env."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import bootstrap


def main():
    # Interpreter path per OS.
    py = bootstrap.venv_python(Path("repo"))
    assert py.parts[-3:-1] == (".venv", "Scripts" if os.name == "nt" else "bin"), py

    # Installs are hash-pinned and use the committed locks, dev lock optional.
    full = bootstrap.install_command("py", dev=True)
    assert "--require-hashes" in full and full.count("-r") == 2, full
    assert full[-4:] == ["-r", "requirements.lock", "-r", "requirements-dev.lock"], full
    lean = bootstrap.install_command("py", dev=False)
    assert lean[-2:] == ["-r", "requirements.lock"] and "requirements-dev.lock" not in lean
    for name in ("requirements.lock", "requirements-dev.lock", ".env.example"):
        assert (ROOT / name).is_file(), name

    # .env is created from the example once and then left alone.
    with tempfile.TemporaryDirectory(prefix="loctms_boot_") as tmp:
        root = Path(tmp)
        assert bootstrap.ensure_env_file(root) == "missing"
        (root / ".env.example").write_bytes(b"DEFAULT_PROVIDER=mock\n")
        assert bootstrap.ensure_env_file(root) == "created"
        assert (root / ".env").read_bytes() == b"DEFAULT_PROVIDER=mock\n"
        (root / ".env").write_bytes(b"MINE=1\n")
        assert bootstrap.ensure_env_file(root) == "exists"
        assert (root / ".env").read_bytes() == b"MINE=1\n"

    # The shipped example defaults to the offline engine so a fresh clone runs without a key.
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "\nDEFAULT_PROVIDER=mock\n" in example

    text = "\n".join(bootstrap.next_steps(".venv/bin/python"))
    assert "python run.py" in text and "suite_owners" in text and "—" not in text

    check_wrapper_from_any_folder()
    print("bootstrap setup OK")


def check_wrapper_from_any_folder():
    """The repo-root CLI wrapper works when called from another folder: it once ran
    `python -m app.cli` with no PYTHONPATH, so outside the repo root it died with
    "No module named 'app'". The test interpreter goes first on PATH so the wrapper's
    fallback python is this one when the clone has no .venv."""
    import subprocess

    if os.name == "nt":
        cmd = ["cmd", "/c", str(ROOT / "loc-tms.cmd"), "version"]
    else:
        cmd = ["sh", str(ROOT / "loc-tms"), "version"]
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="loctms_wrap_") as tmp:
        env["LOC_TMS_DATA_DIR"] = tmp
        out = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, (out.returncode, out.stdout[-500:], out.stderr[-500:])
    assert "No module named" not in out.stderr, out.stderr[-500:]


if __name__ == "__main__":
    main()
