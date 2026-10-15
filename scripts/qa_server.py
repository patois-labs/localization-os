"""Throwaway-environment QA/preview server launcher (issue #13).

Ships as a TOOL for QA/dev previews — it is NEVER invoked by the app. It exists so
preview/QA tooling (e.g. Claude Code's `preview_start`) that honours ONLY a
launch.json config's runtimeExecutable + args CANNOT end up serving the REAL
production data dir. Everything that makes a launch safe is baked in HERE, not in
uvicorn flags a launcher might drop:

  * LOC_TMS_DATA_DIR is pointed at a fresh throwaway temp dir (never the repo
    `data/`), so no real data is ever touched;
  * every provider API key is blanked so no live provider call can fire;
  * uvicorn is started IN-PROCESS on the requested port.

Defense in depth: even if a launcher ignored this script and ran a bare
`uvicorn app.main:app` against the real dir, the data-dir protection guard
(app.config.assert_data_dir_allowed, wired into app.main._startup) refuses to
start once that dir carries the `.protected` marker. This script is the belt; the
guard is the suspenders.

Usage:
    python scripts/qa_server.py [--port PORT] [--host HOST] [--data-dir DIR]

--data-dir defaults to a fresh mkdtemp() dir; pass one to reuse a fixture DB.
"""

import argparse
import os
import sys
import tempfile

# Make `from app import ...` work regardless of CWD.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="qa_server",
        description="Launch Localization OS against a THROWAWAY data dir with blanked "
        "provider keys — the safe way to preview/QA the server.",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8110)
    ap.add_argument(
        "--data-dir", default="", help="data dir to serve (default: a fresh throwaway temp dir)."
    )
    args = ap.parse_args(argv)

    data_dir = args.data_dir or tempfile.mkdtemp(prefix="loctms_qa_")
    # Set the environment BEFORE importing app.config (it resolves these once, at
    # import time). A throwaway data dir + blanked keys = zero risk to real data.
    os.environ["LOC_TMS_DATA_DIR"] = data_dir
    os.environ["LOC_TMS_TEST_MODE"] = "1"  # #291: explicit test mode (loopback only)
    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "LOCAL_BASE_URL"):
        os.environ[key] = ""

    import uvicorn

    sys.stderr.write(
        f"[qa_server] serving app.main:app on {args.host}:{args.port} against "
        f"THROWAWAY data dir {data_dir} (provider keys blanked).\n"
    )
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
