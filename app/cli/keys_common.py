"""Shared context resolution and feature gating for the `keys` CLI verbs.

This module resolves project, actor, label, and key inputs and enforces the
`key_projects_enabled` gate shared by the command modules.

Documented in: docs/architecture/string-keys.md
"""

from ._common import (
    EXIT_USAGE,
    CliError,
    bounded_id,
    get_scoped_or_404,
    op_org_id,
)


def _project_arg(p) -> None:
    p.add_argument(
        "--project",
        required=True,
        type=bounded_id,
        dest="project_id",
        help="the key-based project's id.",
    )


# --- helpers -----------------------------------------------------------------
def _flag_gate():
    """Layer-2 CLI kill switch (the message lives in the registry; see feature_gate)."""
    from ._common import feature_gate

    feature_gate("keys")


def _project(session, args):
    """The key project, org-scoped, or a clean 'no such project'. A DOCUMENT project
    answers the same refusal a missing id gets -- the two types never leak each
    other's shape on any surface."""
    from app.kernel import keys as keys_svc
    from app.models import Project

    project = get_scoped_or_404(
        session.get(Project, args.project_id), args.project_id, "project", op_org_id(args)
    )
    if not keys_svc.is_key_project(project):
        raise CliError(
            f"project #{args.project_id} is not a key-based project.",
            EXIT_USAGE,  # noqa: RUF100, UP032
        )
    return project


def _resolve_key(session, project, args):
    from app.kernel import keys as keys_svc

    key = keys_svc.get_key(
        session,
        project.id,
        args.name,
        getattr(args, "namespace", "") or "",
        getattr(args, "context", "") or "",
    )
    if key is None:
        raise CliError(
            "no such key: {}".format(
                _label(getattr(args, "namespace", ""), args.name, getattr(args, "context", ""))
            ),
            EXIT_USAGE,
        )
    return key


def _label(namespace, name, context="") -> str:
    out = "{}::{}".format(namespace or "(default)", name)
    return out + (f" [{context}]" if context else "")


def _actor(args) -> str:
    return getattr(args, "actor", None) or "cli"


def _clip(s, n=32) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."
