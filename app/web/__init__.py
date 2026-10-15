"""Web layer: one APIRouter per feature area, assembled by app.main.

Import direction is strictly main -> app.web.* -> app.web.deps -> app.kernel.*
(the loc-core facade; composable-core phase 4, K2 -- a small permanent set of
shell-facing services stays on the app.services spelling: trust_auth,
vendor_studio_sso, connectors.base/.wordpress); nothing in this package may
import app.main.

Router GROUPS (composable-core phase 0, Train 0a). The routers are split into three
NAMED boundaries so the embedded/headless profile can assemble an agent-only app:

  * HEALTH_ROUTERS — the container liveness/readiness surface (/health, /ready).
    Mounted in EVERY mode; the embedded profile needs them as its liveness answer.
  * API_ROUTERS    — the AGENT-drivable surface an embedded install serves: the
    token-authed /api routers (api + api_cli), the /api sub-routers split out of the
    otherwise-web keys/images modules (Delta API + key/image /api routes — an
    embedder's PRIMARY surface, so they must NOT be dropped with the web pages), and
    the read-only /utcp tool-manual (not /api-prefixed, but a machine-readable agent
    surface generated from the OpenAPI schema and a member of _ALLOW_EXACT — an agent
    tool, so headless serves it).
  * WEB_ROUTERS    — the Jinja/web surface (dashboard, projects, help, admin console,
    the keys/images REVIEW pages, …). A normal install mounts these; the embedded
    profile drops them entirely.

ALL_ROUTERS = HEALTH_ROUTERS + WEB_ROUTERS + API_ROUTERS is the full default assembly
(a normal, non-embedded boot). It is DERIVED from the three groups (single source of
truth) — so a router added to any group is picked up by the default install, and the
embedded install deterministically mounts only HEALTH_ROUTERS + API_ROUTERS. The
groups partition every router with no overlap; the paths are prefix-disjoint (/health,
/ready, /api/*, /utcp, and the web paths), so the composed order is behavior-neutral.

ACCEPTED ONE-TIME CHURN (NIT): deriving ALL_ROUTERS from the groups (health first,
the /api routers last) reorders router registration relative to the historical flat
list, so /openapi.json enumerates paths in a different order. No behavior or test
depends on that order (route matching is by path, and the /help + LLM-card mirrors are
set comparisons); this is a cosmetic, one-time reordering, accepted deliberately.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from app.modules import router_groups

# The three NAMED router groups, now SOURCED from the module manifest's ROUTER_GROUPS
# (composable-core phase 5, M1). router_groups() resolves each dotted router attr LAZILY
# (a function-body import inside app/modules.py, so `import app.modules` drags in no
# router tree) and returns the three ordered lists -- BYTE-IDENTICAL to the historical
# hand-split HEALTH_ROUTERS / API_ROUTERS / WEB_ROUTERS lists this file used to carry.
# The router->group tagging now has ONE home (app/modules.py's ROUTER_GROUPS) instead of
# three lists maintained here; the group SEMANTICS are unchanged (see the module docstring
# above). Resolving the routers here also eagerly imports every app.web submodule, exactly
# as the old explicit `from app.web import (...)` tuple did.
HEALTH_ROUTERS, API_ROUTERS, WEB_ROUTERS = router_groups()

# The full default assembly (non-embedded boot). Derived from the three groups so the
# boundary stays a single source of truth.
ALL_ROUTERS = HEALTH_ROUTERS + WEB_ROUTERS + API_ROUTERS
