"""Compatibility imports for the production route walk, shared by mirror guards.

Two suites pin a hand-maintained mirror to this surface -- `verify_help` (the
/help Part 2 API reference) and `verify_mcp_parity` (the MCP tool registry) --
and a third mirror is only ever one feature away. Each having its own copy of the
walk is how two guards come to disagree about what "the routes" are: one filters
HEAD/OPTIONS, the next forgets, one reads `app.routes`, the next reads the
OpenAPI schema, and the difference is a set of routes that neither guard covers
while both report green. The walk lives in app.web._routes; this module re-exports
it so existing mirror guards and the runtime help counter use the same traversal.

WHY `app.routes` AND NOT THE OPENAPI SCHEMA. A route with
`include_in_schema=False` is absent from Swagger -- which is a reason to need
documentation and MCP accounting MORE, not less. The schema is what the app
chooses to advertise; the route table is what it actually serves.
"""

from app.web._routes import real_api_routes, walk_routes  # noqa: F401


def mount_problems(app) -> list:
    """The blindspot this walk would have if anything were ever mounted under /api.

    `real_api_routes` reads `route.methods`, which a `Mount` does not have: a
    sub-application mounted at `/api/v2` would contribute ZERO pairs to the walk
    while serving an unbounded number of real endpoints. Every mirror guard built
    on this module would then report a complete, closed accounting over a surface
    it cannot see. Nothing is mounted under /api today and this keeps it that
    way -- if one is ever wanted, the walk must learn to recurse into it FIRST."""
    problems = []
    for route in walk_routes(app):
        if getattr(route, "routes", None) is None:  # not a Mount/sub-router
            continue
        if getattr(route, "methods", None):  # a plain Route, not a Mount
            continue
        path = getattr(route, "path", "") or ""
        if path.startswith("/api"):
            problems.append(
                f"MOUNT UNDER /api: {path!r} is a mounted sub-application, so its "
                "endpoints are invisible to the route walk every mirror guard "
                "uses -- teach real_api_routes to recurse before mounting here"
            )
    return problems
