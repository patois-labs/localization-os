"""GitHub #366: import-time guard for the api_cli.py fixed-slice router splices.

app/web/api_cli.py includes its per-feature routers by fixed slice index
(`routes[0:3]`, `routes[3:9]`, ...), so a route added mid-source silently shifts
every later bound and drops (or double-serves) a neighbour; /api/style-guides/{gid}/ab
already shipped silently dead that way. The guard at the bottom of api_cli.py fails
the import unless the assembled router is an exact partition of its source routers.
This suite proves:

  import time (the real check)
    - importing app.web.api_cli ran the guard and it passed (api_cli._srp_problems
      is empty), over a non-vacuous route surface.
  the guard function (the same one the import call uses)
    - reports a route dropped from the served assembly, naming its path.
    - reports a route served twice instead of once.
    - reports a route two source routers both define, served only once.
    - an endpoint-less route (Mount) is labelled, not an AttributeError.
    - an empty route surface on both sides is reported, never a vacuous pass.
  registration
    - every app/web/api_cli_*.py module router is in api_cli._SRP_SOURCES.
  end-to-end in a subprocess (the real import against a mid-mutated source)
    - a route inserted MID-source makes `import app.web.api_cli` raise RuntimeError
      with a per-route diagnosis, instead of shipping a silently dead endpoint
      (also when the route is appended past the last bound); a route REMOVED
      mid-source is accepted (no false positive).

Final line: "ROUTER SLICE GUARD OK".
"""

import os
import subprocess
import sys
import types
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_routerguard_")

from app.web import api_cli

_CHILD = Path(__file__).with_name("_router_slice_guard_child.py")


def _sources():
    # the same source set the import-time check uses (21 spliced sources plus the
    # two whole-router includes at the bottom of api_cli.py)
    return (
        *api_cli._SRP_SOURCES,
        api_cli._page_layouts_api_router,
        api_cli._system_prompts_api_router,
        api_cli._search_api_router,
        api_cli._formality_api_router,
        api_cli._notify_api_router,
        api_cli._account_theme_api_router,
        api_cli._project_automation_api_router,
    )


def check_import_time_guard_passed():
    assert api_cli._srp_problems == [], api_cli._srp_problems
    total = sum(len(api_cli._srp_flattened(src)) for src in _sources())
    assert total > 300, f"guard surface implausibly small: {total}"
    print(f"import_time_guard_empty_over_{total}_routes OK")


def check_guard_reports_a_dropped_route():
    routes = list(api_cli.router.routes)
    served = types.SimpleNamespace(routes=routes[:-1])
    problems = api_cli._srp_slice_problems(served, _sources())
    last = api_cli._srp_flattened(api_cli.router)[-1]
    text = "; ".join(problems)
    assert problems, "dropped route not reported"
    assert last.path in text, (last.path, problems)
    assert "spliced 0x" in text, problems
    print("dropped_route_reported_with_path OK")


def check_guard_reports_a_duplicated_route():
    routes = list(api_cli.router.routes)
    served = types.SimpleNamespace(routes=[*routes, routes[-1]])
    problems = api_cli._srp_slice_problems(served, _sources())
    text = "; ".join(problems)
    assert "spliced 2x, source defines 1x" in text, problems
    print("duplicated_route_reported OK")


def check_guard_reports_a_cross_source_duplicate():
    # Two source routers defining the SAME endpoint (path + methods identical) but
    # the assembly serving it only once: the per-source check passes 1==1 on each
    # side, so only the combined-total check can catch it.
    route = api_cli._srp_flattened(api_cli.router)[-1]
    served = types.SimpleNamespace(routes=[route])
    src_a = types.SimpleNamespace(routes=[route])
    src_b = types.SimpleNamespace(routes=[route])
    problems = api_cli._srp_slice_problems(served, (src_a, src_b))
    text = "; ".join(problems)
    assert problems, "cross-source duplicate not reported"
    assert "served 1x in total, sources define 2x combined" in text, problems
    print("cross_source_duplicate_reported OK")


def check_guard_label_survives_endpointless_routes():
    # a Mount or websocket route has no .methods/.endpoint; the diagnosis must still
    # name it instead of raising AttributeError over the real problem
    mount = types.SimpleNamespace(path="/mounted", name="m")
    served = types.SimpleNamespace(routes=[])
    problems = api_cli._srp_slice_problems(served, (types.SimpleNamespace(routes=[mount]),))
    assert problems and "/mounted" in problems[0] and "MOUNT" in problems[0], problems
    print("endpointless_route_labelled OK")


def check_guard_never_passes_vacuously():
    # if FastAPI internals ever hide every route on both sides, the tallies agree at
    # zero; the guard must report that instead of passing
    empty = types.SimpleNamespace(routes=[])
    problems = api_cli._srp_slice_problems(empty, (empty,))
    assert problems and "no source routes visible" in problems[0], problems
    print("empty_surface_reported OK")


def check_every_api_cli_module_router_is_registered():
    # A new app/web/api_cli_*.py whose router is never added to _SRP_SOURCES is
    # invisible to the partition check (an included-but-unregistered one already
    # fails it as "no source router defines it"). Close that cheaply: every
    # api_cli_* module that exposes an APIRouter `router` must be a registered source.
    import importlib

    from fastapi import APIRouter

    registered = {id(r) for r in _sources()}
    checked = []
    for path in sorted(Path(_ROOT, "app", "web").glob("api_cli_*.py")):
        mod = importlib.import_module(f"app.web.{path.stem}")
        rtr = getattr(mod, "router", None)
        if isinstance(rtr, APIRouter):
            assert id(rtr) in registered, f"{path.stem}.router is not in api_cli._SRP_SOURCES"
            checked.append(path.stem)
    assert len(checked) == len(api_cli._SRP_SOURCES), (checked, len(api_cli._SRP_SOURCES))
    print(f"every_api_cli_module_router_registered_{len(checked)} OK")


def _run_child(mode):
    return subprocess.run(
        [sys.executable, str(_CHILD), mode],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _diag(proc):
    return (proc.stdout or "") + ("\n[stderr]\n" + (proc.stderr or ""))


def check_mid_source_route_fails_the_real_import():
    for mode in ("insert", "append"):
        proc = _run_child(mode)
        assert proc.returncode == 0, mode + _diag(proc)
        assert "refused the stale splice" in proc.stdout, (mode, proc.stdout)
    print("mid_source_route_fails_real_import OK")


def check_route_removal_is_not_a_false_positive():
    proc = _run_child("remove")
    assert proc.returncode == 0, _diag(proc)
    print("route_removal_not_a_false_positive OK")


def main():
    check_import_time_guard_passed()
    check_guard_reports_a_dropped_route()
    check_guard_reports_a_duplicated_route()
    check_guard_reports_a_cross_source_duplicate()
    check_guard_label_survives_endpointless_routes()
    check_guard_never_passes_vacuously()
    check_every_api_cli_module_router_is_registered()
    check_mid_source_route_fails_the_real_import()
    check_route_removal_is_not_a_false_positive()
    print("ROUTER SLICE GUARD OK")


main()
