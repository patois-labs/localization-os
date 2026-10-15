"""Documented in: docs/architecture/identity-and-access.md"""

from starlette._utils import get_route_path


def route_path(scope) -> str:
    """The one wrapper over Starlette's private route-path API.

    Isolate the private dependency here to contain upgrade risk; security callers
    must compare exactly the mount-relative path that Starlette routes.
    """
    return get_route_path(scope)
