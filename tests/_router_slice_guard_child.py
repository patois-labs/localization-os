"""#366 guard child process: re-run the REAL api_cli import against a mid-mutated source.

Run by tests/verify_router_slice_guard.py as its own subprocess (never by pytest
directly). It reproduces the exact #366 failure mode: a route added mid-source
shifts every stale fixed slice bound in app/web/api_cli.py, so one real route is
left unsliced. The import-time guard must make `import app.web.api_cli` fail
loudly instead of shipping a silently dead endpoint.

Note: importing the app.web package ITSELF assembles (and splices) api_cli, so the
mutation cannot be prepared the usual way (`from app.web import api_cli_misc_1`
already ran the splice). Instead: import once (guard passes on the real routes),
mutate the cached source module, drop app.web.api_cli from sys.modules, and re-import
so the splice + guard re-run against the mutated source.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_routerguard_child_")

import app.web.api_cli_misc_1 as misc1
from app.web import api_cli  # noqa: F401 (first import: the guard passes on the real routes)

MODE = sys.argv[1] if len(sys.argv) > 1 else "insert"
if MODE in ("insert", "append"):
    # "insert": a route added MID-source (index 5) shifts the stale bounds
    # [0:2], [2:8], [8:13], [13:20], [20:23] so they cover 23 of the 24 routes and drop
    # the last one. "append": a route added at the END falls past the last fixed bound.

    @misc1.router.get("/api/__probe366__")
    def _probe366():
        return {}

    if MODE == "insert":
        route = misc1.router.routes.pop()
        misc1.router.routes.insert(5, route)
elif MODE == "remove":
    # A route REMOVED mid-source is a legitimate edit: the shifted bounds still
    # cover every remaining route exactly once, so the guard must NOT fire (no
    # false positive).
    del misc1.router.routes[5]
else:
    raise SystemExit(f"unknown mode {MODE}")

del sys.modules["app.web.api_cli"]  # force the splice + guard to re-run
try:
    import app.web.api_cli  # noqa: F401
except RuntimeError as exc:
    if MODE == "remove":
        print(f"child: false positive on a route removal: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    text = str(exc)
    assert "exact partition" in text, text
    assert "spliced 0x" in text, text
    assert "/api/" in text, text
    print("child: the import-time guard refused the stale splice")
    raise SystemExit(0) from None

if MODE == "remove":
    print("child: removal accepted (no false positive)")
    raise SystemExit(0)
print("child: the guard did not fire", file=sys.stderr)
raise SystemExit(1)
