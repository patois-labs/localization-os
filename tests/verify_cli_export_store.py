"""CLI `export download` resolves the artifact through the registered ExportStore (#528).

Before the fix the CLI read `config.EXPORTS_DIR / key` directly, so on an S3-backed install
(artifact lives in the bucket, not on disk) every download failed with "artifact file is
gone" while the web UI served the same file. The pins use a registered in-memory stub store
(the S3 adapter's contract) with NO file on disk:

  1. a stub-store artifact downloads byte-identical and exits 0;
  2. an artifact missing from the store is the clean "gone" exit 1, leaving no partial file;
  3. the default disk store still downloads (no regression);
  4. a stream failing midway exits 1 and leaves neither the target nor a .part file.

Final line: "CLI EXPORT STORE OK".
"""

import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cli_exstore_")

from sqlmodel import Session

from app import ports
from app.db import engine, init_db
from app.models import DataExportRun
from tests._cli_runner import run_cli

_PAYLOAD = b"PK\x03\x04" + bytes(range(256)) * 40


class _Stub:
    def __init__(self, blobs=None, fail_midway=False):
        self.blobs = dict(blobs or {})
        self.fail_midway = fail_midway

    def put_stream(self, key, chunks):
        self.blobs[key] = b"".join(chunks)
        return len(self.blobs[key])

    def open_range(self, key, start, end):
        if key not in self.blobs:
            raise FileNotFoundError(key)
        data = self.blobs[key][start : (None if end is None else end + 1)]

        def gen():
            yield data[:100]
            if self.fail_midway:
                raise OSError("connection reset")
            yield data[100:]

        return gen()

    def presign_get(self, key, *, ttl_seconds):
        return f"https://s3.example/{key}"

    def delete(self, key):
        return self.blobs.pop(key, None) is not None


def _run_row(uuid):
    with Session(engine) as s:
        s.add(
            DataExportRun(
                run_uuid=uuid,
                scope_kind="self",
                status="ok",
                artifact_key=f"dataexport_{uuid}.zip",
                artifact_bytes=len(_PAYLOAD),
            )
        )
        s.commit()


def _download(uuid, out):
    return run_cli(["--json", "export", "download", uuid, "-o", out, "--force"])


def test_s3_store_download():
    tmp = tempfile.mkdtemp(prefix="loctms_exstore_")
    _run_row("s3run")
    stub = _Stub({"dataexport_s3run.zip": _PAYLOAD})
    ports.register("exportstore", stub)
    try:
        out = os.path.join(tmp, "a.zip")
        code, stdout, err = _download("s3run", out)
        assert code == 0, (code, stdout, err)
        assert json.loads(stdout.strip().splitlines()[-1])["bytes"] == len(_PAYLOAD)
        with open(out, "rb") as fh:
            assert fh.read() == _PAYLOAD
        # 2. absent from the store
        stub.blobs.clear()
        out2 = os.path.join(tmp, "b.zip")
        code, stdout, err = _download("s3run", out2)
        assert code == 1 and "gone" in (stdout + err), (code, stdout, err)
        assert not os.path.exists(out2) and not os.path.exists(out2 + ".part")
        # 4. a stream failing midway
        stub.blobs["dataexport_s3run.zip"] = _PAYLOAD
        stub.fail_midway = True
        out3 = os.path.join(tmp, "c.zip")
        code, stdout, err = _download("s3run", out3)
        assert code == 1, (code, stdout, err)
        assert not os.path.exists(out3) and not os.path.exists(out3 + ".part")
    finally:
        ports.unregister("exportstore")
    print(
        "  1/2/4. S3-style store: byte-identical download, gone -> exit 1, mid-stream failure clean"
    )


def test_disk_store_still_downloads():
    from app import config
    from app.ports import exportstore

    tmp = tempfile.mkdtemp(prefix="loctms_exstore_disk_")
    _run_row("diskrun")
    exportstore.get().put_stream("dataexport_diskrun.zip", [_PAYLOAD])
    assert (config.EXPORTS_DIR / "dataexport_diskrun.zip").exists()
    out = os.path.join(tmp, "d.zip")
    code, stdout, err = _download("diskrun", out)
    assert code == 0, (code, stdout, err)
    with open(out, "rb") as fh:
        assert fh.read() == _PAYLOAD
    print("  3. default disk store still downloads")


def main_() -> None:
    init_db()
    test_s3_store_download()
    test_disk_store_still_downloads()
    print("CLI EXPORT STORE OK")


if __name__ == "__main__":
    main_()
