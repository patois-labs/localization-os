"""Regenerate the API/MCP wire-contract snapshot pinned by verify_branding CHECK 5.

    .venv\\Scripts\\python.exe tests/gen_branding_contract_snapshot.py

Writes tests/branding_contract_snapshot.json = the sorted registered route paths of
app.main.app plus the sorted MCP tool NAMES from integrations/mcp-server/registry.py.

WHEN TO RUN IT (SPEC-brand-as-config §D9.6): the brand campaign itself changes NOTHING
on the wire, so the pin must NOT be regenerated to paper over an accidental rename — a
drift is a real bug. Regenerate ONLY when the wire contract legitimately changed for a
reason unrelated to branding (e.g. a rebase pulls in a feature train that ADDS routes or
tools). Then run this, `git diff` the JSON, confirm every change is an intentional
ADD/REMOVE (never a rename of an existing path/tool), and commit the reviewed diff with
the same change. The equality assertion in verify_branding then re-arms at the new baseline.
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT = os.path.join(_ROOT, "tests", "branding_contract_snapshot.json")


def main() -> int:
    from app.web._routes import walk_routes

    sys.path.insert(0, _ROOT)
    sys.path.insert(0, os.path.join(_ROOT, "integrations", "mcp-server"))
    # SAFETY (M5): boot against a THROWAWAY data dir + blanked keys, never the operator's
    # real DATA_DIR — importing app.main must not touch a live install.
    from tests._bootstrap import init_env

    init_env("loctms_snap_")
    import registry  # integrations/mcp-server/registry.py

    from app.main import app
    from app.services import settings

    # route path -> sorted HTTP methods (L11); aggregate across handlers sharing a path.
    routes: dict[str, set] = {}
    for r in walk_routes(app):
        p = getattr(r, "path", None)
        if p:
            routes.setdefault(p, set()).update(getattr(r, "methods", None) or [])
    snapshot = {
        "routes": {p: sorted(m) for p, m in sorted(routes.items())},
        "mcp_tools": sorted({t.name for t in registry.TOOLS}),
        # Settings KEYS are part of the payload contract (D9.3): brand_* keys are ADDITIVE
        # only; a rename/removal of any key breaks the wire. (Value SCHEMAS and audit-event
        # names are deliberately NOT pinned here — noted in the report, not machined.)
        "settings_keys": sorted(settings.DEFAULTS.keys()),
    }
    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(
        f"wrote {_OUT}: {len(snapshot['routes'])} routes, "
        f"{len(snapshot['mcp_tools'])} MCP tools, {len(snapshot['settings_keys'])} settings keys"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
