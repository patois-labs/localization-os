"""The target language a bilingual vendor file declares for itself (#687).

A Trados SDLXLIFF, Phrase (mxliff) or memoQ (mqxliff) XLIFF, Wordfast TXLF or TXML, a
Trados TagEditor TTX, a Trados package member and the XLIFF members of a zipped memoQ
(.mqxlz) or WorldServer (.xlz / .wsxz) package (#759) each name their own target language
(`target-language`, `targetlocale`, `TargetLanguage`). The export never changes that
declaration (the vendor tool matches the file to its project), so translating such a file
into ANOTHER language would hand back, say, German text under `fr-FR`.

Owner decision (2026-10-11, decision 6): accept the file with a clear warning and translate
and export it ONLY into the language it declares, when that language is one of the project's
targets. When none of the project's targets is the file's language, the upload is refused
with a message that says so: nothing of another language is ever written over the file.

The rules live here so every door applies the same ones:
  * ingest (`app/services/ingest.py`): the segment rows are seeded only for the matching
    target(s), the declaration is stamped on the skeleton (`file_target_locales`) and the
    warning rides the existing `file_warnings` channel (document page, upload JSON, CLI);
  * add-a-language and translate-time seeding (`app/services/locale_seed.py`): a locale the
    file does not declare is never seeded (the translate door refuses it, add-a-language
    skips it with a note on the document);
  * export (`app/services/export.py`): a locale the file does not declare is refused, which
    also covers documents uploaded before this rule.

Matching: case and `_`/`-` are ignored; a bare language on either side (`ja` in a memoQ file,
`ja-JP` in the project) matches any region of that language; exact matches win over such
loose ones.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re

from app.filters.base import InputLimitError

SKELETON_KEY = "file_target_locales"
WARNING_KIND = "file_target_language"

# Bilingual vendor formats whose own target language the export keeps. These are the
# document `fmt` values, i.e. upload extensions (a .wsxz is fmt `wsxz`, served by the xlz
# filter).
BILINGUAL_FMTS = (
    "sdlxliff",
    "sdlxlf",
    "mqxliff",
    "mxliff",
    "txlf",
    "txml",
    "ttx",
    "sdlrpx",
    "mqxlz",
    "xlz",
    "wsxz",
)

_FILE_TAG = re.compile(r"(?:^|})file$")


class FileTargetMismatch(InputLimitError):
    """The file declares a target language this project (or this locale) is not. A
    subclass of InputLimitError, so every door that shows a refusal's own message (web page
    error, CLI error line, MCP tool error, ZIP batch `failed` line) shows this one. It is
    not an over-limit refusal, so /api answers it 422 (`http_status`), not the 413 of
    InputLimitError."""

    http_status = 422


def norm(tag: str | None) -> str:
    return (tag or "").strip().replace("_", "-").lower()


def _xliff_targets(data: bytes) -> list[str]:
    from app.filters.xliff_filter import _parse_xml

    root = _parse_xml(bytes(data))
    out = []
    for el in root.iter():
        if isinstance(el.tag, str) and _FILE_TAG.search(el.tag):
            tl = (el.get("target-language") or "").strip()
            if tl:
                out.append(tl)
    return out


def _txml_targets(data: bytes) -> list[str]:
    from app.filters.wordfast import _parse_txml

    tl = (_parse_txml(bytes(data)).get("targetlocale") or "").strip()
    return [tl] if tl else []


def _ttx_targets(data: bytes) -> list[str]:
    from app.filters.ttx_filter import _langs, _parse

    tl = _langs(_parse(bytes(data))).target
    return [tl] if tl else []


def _package_member_targets(data: bytes) -> list[str]:
    from app.filters.sdl_package import member_meta

    meta = member_meta(data) or {}
    tl = str(meta.get("locale") or "").strip()
    return [tl] if tl else []


def _xliff_package_targets(fmt: str):
    """Every XLIFF member of a memoQ .mqxlz or WorldServer .xlz / .wsxz package (#759):
    `xliff_package.build_package` keeps each member's own `target-language`, like the
    single-file formats above. Opened through the filter's own zip_guard checks."""

    def read(data: bytes) -> list[str]:
        from app.filters.xliff_package import _members, open_zip

        out: list[str] = []
        with open_zip(data, fmt) as zf:
            for _i, info in _members(zf, fmt):
                out.extend(_xliff_targets(zf.read(info)))
        return out

    return read


def declared_targets(fmt: str, data: bytes) -> list[str]:
    """The target language(s) the file declares, de-duplicated in file order; [] for a
    format that declares none or a file that names none (an unreadable file too: the
    filter's own parse already decided whether it is accepted)."""
    fmt = (fmt or "").lower()
    reader = {
        "sdlxliff": _xliff_targets,
        "sdlxlf": _xliff_targets,
        "mqxliff": _xliff_targets,
        "mxliff": _xliff_targets,
        "txlf": _xliff_targets,
        "txml": _txml_targets,
        "ttx": _ttx_targets,
        "sdlrpx": _package_member_targets,
        "mqxlz": _xliff_package_targets("mqxlz"),
        "xlz": _xliff_package_targets("xlz"),
        "wsxz": _xliff_package_targets("xlz"),
    }.get(fmt)
    if reader is None:
        return []
    try:
        found = reader(data)
    except Exception:  # never turn a readable upload into a crash here
        return []
    seen: set[str] = set()
    return [t for t in found if not (norm(t) in seen or seen.add(norm(t)))]


def _loose(a: str, b: str) -> bool:
    la, lb = a.split("-")[0], b.split("-")[0]
    return la == lb and ("-" not in a or "-" not in b)


def matching(locales: list[str], declared: list[str]) -> list[str]:
    """The project locales the file may be translated into (order kept): exact matches
    when there are any, else loose ones (a bare language on either side)."""
    want = [norm(d) for d in declared if norm(d)]
    if not want:
        return list(locales)
    exact = [loc for loc in locales if norm(loc) in want]
    if exact:
        return exact
    return [loc for loc in locales if any(_loose(norm(loc), w) for w in want)]


def allows(skeleton: dict | None, locale: str) -> bool:
    """False when the document carries a declared target that `locale` is not."""
    declared = (skeleton or {}).get(SKELETON_KEY) if isinstance(skeleton, dict) else None
    if not declared or not locale or locale == "—":
        return True
    return bool(matching([locale], declared))


def _names(declared: list[str]) -> str:
    return ", ".join(declared)


def locale_refusal(declared: list[str], locale: str) -> str:
    return (
        f"This file is a bilingual file for {_names(declared)} (its own target language), so "
        f"it is not translated or exported into {locale}: the file would declare "
        f"{_names(declared)} over {locale} text. Upload a copy of the file made for {locale} "
        "in the CAT tool instead."
    )


def plan_ingest(declared: list[str], locales: list[str]) -> tuple[list[str], dict | None]:
    """(locales to seed, warning or None) for a new upload. Raises FileTargetMismatch when
    none of the project's targets is the file's language. A project with no targets yet
    (`["—"]`, the inbox placeholder) is left alone: the translate door decides later."""
    if not declared or not locales or locales == ["—"]:
        return locales, None
    keep = matching(locales, declared)
    if not keep:
        raise FileTargetMismatch(
            f"This file is a bilingual file whose own target language is {_names(declared)}, "
            f"but this project translates into {', '.join(locales)}. Nothing was imported: "
            f"translating it would put another language under {_names(declared)}. Add "
            f"{_names(declared)} to the project's target languages, or upload the file into "
            "a project that translates into it."
        )
    if len(keep) == len(locales):
        return keep, None
    others = [loc for loc in locales if loc not in keep]
    return keep, {
        "kind": WARNING_KIND,
        "part": "target language",
        "message": (
            f"This file's own target language is {_names(declared)}, so it is translated and "
            f"exported only into {', '.join(keep)}. It is not translated into "
            f"{', '.join(others)}: those need their own copy of the file from the CAT tool."
        ),
    }
