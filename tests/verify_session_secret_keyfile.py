"""#480: an empty or too-short session secret is never trusted.

THE BUG: config._keyfile read an existing `.session_secret` and returned its stripped
content with no check. The file is created O_EXCL before the value is written, so a
crash (or a second worker racing the first) left or exposed a 0-byte file, and the
empty string became the SessionMiddleware signing key and the audit-chain key seed.

Pinned:
  A  an existing EMPTY (or whitespace-only) keyfile is regenerated, atomically, into a
     long random value and the file on disk holds it.
  B  a racing reader never observes a partially written keyfile (the value appears
     whole or not at all).
  C  a short SESSION_SECRET (env or keyfile) makes config import fail with a clear
     error; a long one is accepted untouched.

Final line: SESSION SECRET KEYFILE OK.
"""

import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_sessionsecret_")
os.environ["LOC_TMS_SESSION_SECRET"] = ""
os.environ["LOC_TMS_ENCRYPTION_KEY"] = ""

from app import config

_CHILD = "import app.config as c; print('SECRET-LEN', len(c.SESSION_SECRET))"


def _boot(data_dir, **env_over):
    env = dict(os.environ)
    for k in (
        "LOC_TMS_SESSION_SECRET",
        "SESSION_SECRET",
        "LOC_TMS_ENCRYPTION_KEY",
        "ENCRYPTION_KEY",
    ):
        env[k] = ""
    env.update(env_over)
    env["LOC_TMS_DATA_DIR"] = data_dir
    return subprocess.run([sys.executable, "-c", _CHILD], env=env, cwd=_ROOT, capture_output=True)


def test_empty_keyfile_is_regenerated():
    for content in ("", "  \n"):
        name = f".empty_{abs(hash(content))}"
        (config.DATA_DIR / name).write_bytes(content.encode())
        val, _created = config._keyfile(name, lambda: "g" * 64)
        assert val == "g" * 64, ("empty keyfile must be regenerated", repr(val))
        assert (config.DATA_DIR / name).read_text(encoding="utf-8").strip() == "g" * 64
    # a subprocess boot on a data dir holding a 0-byte .session_secret gets a real key
    d = tempfile.mkdtemp(prefix="loctms_ss_empty_")
    open(os.path.join(d, ".session_secret"), "wb").close()
    p = _boot(d)
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    n = int(p.stdout.decode().split("SECRET-LEN")[1])
    assert n >= 32, n
    print("  (A) empty / blank keyfile regenerated OK")


def test_reader_never_sees_partial_keyfile():
    name = ".race_keyfile"
    path = config.DATA_DIR / name
    seen = []
    stop = threading.Event()

    def slow_gen():
        time.sleep(0.4)
        return "r" * 64

    def watcher():
        while not stop.is_set():
            with contextlib.suppress(FileNotFoundError):
                seen.append(path.read_bytes())

    t = threading.Thread(target=watcher)
    t.start()
    val, _ = config._keyfile(name, slow_gen)
    stop.set()
    t.join()
    assert val == "r" * 64
    assert all(b == b"r" * 64 for b in seen), {b for b in seen if b != b"r" * 64}
    print("  (B) concurrent reader never sees an empty or partial keyfile OK")


def test_short_secret_refused():
    d = tempfile.mkdtemp(prefix="loctms_ss_short_")
    p = _boot(d, SESSION_SECRET="short")
    assert p.returncode != 0 and b"SESSION_SECRET" in p.stderr and b"too short" in p.stderr, (
        p.stderr[-400:]
    )
    # the refusal names length and source, never the value (any source, gate #480)
    p = _boot(d, LOC_TMS_SESSION_SECRET="Qx9vZk2Wm")
    assert p.returncode != 0 and b"too short" in p.stderr, p.stderr[-400:]
    assert b"Qx9vZk2Wm" not in p.stderr + p.stdout, "the short secret must not be printed"
    d2 = tempfile.mkdtemp(prefix="loctms_ss_short2_")
    with open(os.path.join(d2, ".session_secret"), "w", encoding="utf-8") as f:
        f.write("Kf7Qp")
    p = _boot(d2)
    assert p.returncode != 0 and b"too short" in p.stderr, p.stderr[-400:]
    assert b"Kf7Qp" not in p.stderr + p.stdout, "the keyfile value must not be printed"
    with open(os.path.join(d2, ".session_secret"), encoding="utf-8") as f:
        assert f.read() == "Kf7Qp"
    d3 = tempfile.mkdtemp(prefix="loctms_ss_ok_")
    p = _boot(d3, SESSION_SECRET="x" * 40)
    assert p.returncode == 0 and b"SECRET-LEN 40" in p.stdout, p.stderr[-400:]
    print("  (C) short secrets refused with a clear error, long ones accepted OK")


def main():
    test_empty_keyfile_is_regenerated()
    test_reader_never_sees_partial_keyfile()
    test_short_secret_refused()
    print("SESSION SECRET KEYFILE OK")


if __name__ == "__main__":
    main()
