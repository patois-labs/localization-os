"""Per-project "translate comments" option (#690, the maintainer's decision 7b, 2026-10-11).

Reviewer comments and annotations in Office files are offered for translation only when
the project says so. One switch covers every Office family:

  * Word (.docx and siblings): the comments part (word/comments.xml), as walk 4 reads it;
  * Excel (.xlsx and siblings): cell comments (notes) and threaded comments;
  * PowerPoint (.pptx and siblings): slide comments, classic and modern (replies too);
  * OpenDocument (.odt/.ods/.odp/.odg and templates): office:annotation paragraphs;
  * Visio (.vsdx/.vsdm): comment entries (visio/comments.xml).

Comment authors, initials and dates are never units and never change.

Storage: a per-project row `translate_comments:<project id>` in the global Setting table
("1" on, "0" off), the same seam as `key_autounverify:<id>`. No schema change.

Default OFF, with one compatibility rule for Word. Before this option existed Word
comments were ALWAYS translated and every other format's never were. So:
  * a project created from now on gets an explicit "0" at creation
    (`stamp_new_project`, called by content.project_create and by the two paths that
    build a Project directly: routing.ensure_department_inbox and the connectors'
    store._ensure_project): comments off everywhere, Word included;
  * a project with NO row (created before this option) keeps the old behaviour: Word
    comments on, other formats off. "Before" is the absence of the row, never a date
    or id comparison, so there is no clock race;
  * once someone switches the option on or off, that choice applies to every format.

The option is read at IMPORT. A document already imported keeps the units it was
imported with (switching the option later changes the next upload or re-import only).
Export never needs the option: comment units sit at fixed indices (`app.filters.
office_comments.COMMENT_BASE`, or the comments part of a DOCX walk), so a document
imported without them simply has no comment translations to write.

Documented in: docs/architecture/content-and-projects.md
"""

from __future__ import annotations

SETTING_PREFIX = "translate_comments"

#: The formats whose filters take the option (canonical fmt names). DOCX is the only one
#: whose comments were translated before the option existed (see the module docstring).
COMMENT_FORMATS = frozenset(("docx", "xlsx", "pptx", "odt", "ods", "odp", "odg", "vsdx"))
LEGACY_ON_FORMATS = frozenset(("docx",))


def _key(project) -> str:
    return f"{SETTING_PREFIX}:{getattr(project, 'id', project)}"


def stored(project) -> str | None:
    """ "1", "0", or None when the project has no row (a pre-option project)."""
    from app.services import settings

    pid = getattr(project, "id", project)
    if pid is None:
        return None
    try:
        raw = settings.get(_key(pid), None)
    except Exception:
        return None
    if raw is None:
        return None
    raw = str(raw).strip()
    return raw if raw in ("0", "1") else None


def enabled_for(project, fmt: str) -> bool:
    """Whether a document of canonical format `fmt` imported into `project` gets its
    comments as units."""
    value = stored(project)
    if value is not None:
        return value == "1"
    return (fmt or "").lower() in LEGACY_ON_FORMATS


def status(project) -> dict:
    """The read-back every surface reports: `translate_comments` (bool, what new imports
    of non-Word formats do), `translate_comments_word` (bool, Word) and
    `translate_comments_source` ("project" = switched explicitly, "legacy" = a project
    from before the option: Word on, the rest off)."""
    value = stored(project)
    if value is None:
        return {
            "translate_comments": False,
            "translate_comments_word": True,
            "translate_comments_source": "legacy",
        }
    on = value == "1"
    return {
        "translate_comments": on,
        "translate_comments_word": on,
        "translate_comments_source": "project",
    }


def parse_switch(value) -> bool | None:
    """True / False from the shapes the surfaces send (JSON bool, "on"/"off", "1"/"0",
    "true"/"false", "yes"/"no"), None for anything else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "on", "true", "yes"):
            return True
        if v in ("0", "off", "false", "no"):
            return False
    return None


def set_for(project, on: bool, *, actor: str = "", ip: str = "") -> dict:
    """Switch the option for one project (every format). Audited. Returns status()."""
    from app.services import audit, settings

    pid = getattr(project, "id", project)
    settings.set_many({_key(pid): "1" if on else "0"}, actor=actor or "system")
    audit.log(
        actor=actor or "system",
        action="project.translate_comments_on" if on else "project.translate_comments_off",
        target=f"project {pid}",
        ip=ip or "",
        customer_id=getattr(project, "customer_id", None),
    )
    return status(pid)


def stamp_new_project(project, session) -> None:
    """A project created from now on starts with comments OFF in every format.

    Written through the CALLER's session and committed with it, never through a second
    write session: a caller may still hold a write transaction (a savepoint-wrapped
    service call, a request session with pending writes), and on SQLite a second writer
    then waits on the first one in the same thread until "database is locked" (the QA-1
    geometry `settings.forget` documents). The cross-process settings version is bumped
    in the same transaction, as `settings.set_many` and `ledger_controls` do, so other
    processes drop any cached value for a reused project id."""
    from app.models import Setting
    from app.services import settings

    pid = getattr(project, "id", project)
    if pid is None:
        return
    name = _key(pid)
    stale = session.get(Setting, name)
    if stale is not None:
        # A leftover row for a reused id (forget_project normally removes it). Replace
        # it rather than assign .value (verify_export_output_escaping pins those writes).
        session.delete(stale)
        session.flush()
    session.add(Setting(key=name, value="0"))
    session.flush()
    settings._bump_version(session)
    session.commit()
    settings.forget([name])
    if project is not pid:
        # The commit expired the caller's Project; reload it so a caller that reads it
        # after closing its session (as before this stamp joined the session) still can.
        session.refresh(project)


def forget_project(session, project_id: int) -> None:
    """Remove the project's row (project delete), so a rowid-reusing successor never
    inherits it."""
    from sqlalchemy import delete as sa_delete
    from sqlmodel import select

    from app.models import Setting
    from app.services import settings

    name = _key(project_id)
    if session.exec(select(Setting).where(Setting.key == name)).first() is not None:
        session.exec(sa_delete(Setting).where(Setting.key == name))
        session.flush()
        settings.forget([name])
