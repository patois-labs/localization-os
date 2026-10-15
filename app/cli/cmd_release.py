"""Release identity commands.

Documented in: docs/architecture/deployment.md
"""

import argparse
import json

from ._common import EXIT_FAIL, CliError, remote_client, remote_mode


def register(subparsers):
    p = subparsers.add_parser("version", help="Show the baked release identity.")
    p.add_argument("--json", dest="as_json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(func=_version)
    p = subparsers.add_parser("release", help="Release inventory and build identity.")
    sub = p.add_subparsers(dest="release_cmd", required=True)
    p = sub.add_parser("manifest", help="Read release, dependencies, extras and Alembic head.")
    p.add_argument("--json", dest="as_json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(func=_manifest)


def _remote_release_body(args):
    """GET /api/release over --remote and pin its shape (issue #317): these leaves read
    the body directly (`version` indexes three keys, `release manifest` dumps the whole
    object), so a JSON answer that is not an object -- a proxy list, a bare string --
    must refuse cleanly instead of dumping a surprise object."""
    body = remote_client(args).get("/api/release")
    if not isinstance(body, dict):
        from app import branding

        raise CliError(
            "server: /api/release answered with JSON that is not an object; "
            f"is --remote pointing at a {branding.brand_name()} server?",
            EXIT_FAIL,
        )
    return body


def _version(args, emit):
    # remote_mode() is the two-mode dispatch seam (not a bare args.remote check): over
    # --remote read the identity from /api/release, else the baked local identity. Both
    # `version` and `release manifest` are remote-capable read leaves.
    from app.release import release_identity

    body = _remote_release_body(args) if remote_mode(args) else release_identity()
    if remote_mode(args):
        missing = [k for k in ("version", "tag", "commit") if k not in body]
        if missing:
            raise CliError(
                "server: the /api/release response is missing " + ", ".join(missing),
                EXIT_FAIL,
            )
    result = {k: body[k] for k in ("version", "tag", "commit")}
    emit.result(result["version"], result)
    return 0


def _manifest(args, emit):
    from app.release import release_manifest

    body = _remote_release_body(args) if remote_mode(args) else release_manifest()
    emit.result(json.dumps(body, indent=2), body)
    return 0
