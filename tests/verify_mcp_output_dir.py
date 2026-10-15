"""MCP server output dir: LOC_TMS_OUTPUT_DIR is the new name, the old name still works."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = ROOT / "integrations" / "mcp-server"
PROBE = "import server; print(server.OUTPUT_DIR)"


def resolved(**extra):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("LOC_TMS_OUTPUT_DIR", "LOC_STUDIO_OUTPUT_DIR")
    }
    env.update(extra)
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=str(MCP_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    return Path(result.stdout.strip().splitlines()[-1])


def main():
    default = MCP_DIR / "loc-studio-output"
    assert resolved() == default, "default output dir changed"
    new = str(ROOT / "out-new")
    old = str(ROOT / "out-old")
    assert resolved(LOC_TMS_OUTPUT_DIR=new) == Path(new)
    assert resolved(LOC_STUDIO_OUTPUT_DIR=old) == Path(old), "the old name must still be read"
    assert resolved(LOC_TMS_OUTPUT_DIR=new, LOC_STUDIO_OUTPUT_DIR=old) == Path(new), "new name wins"
    assert resolved(LOC_TMS_OUTPUT_DIR="", LOC_STUDIO_OUTPUT_DIR=old) == Path(old), (
        "blank falls back"
    )
    print("mcp output dir alias OK")


if __name__ == "__main__":
    main()
