"""Microsoft 365 Files connector: SharePoint document libraries and OneDrive, a T3 consumer.

Documented in: docs/architecture/connectors.md

WHAT IT DOES. A folder-based file round trip. Every translatable file under the connection's
SOURCE folder is downloaded, parsed with our existing file filters (DOCX, XLSX, PPTX and their
macro / template siblings, HTML, Markdown, text, JSON, PO, SRT), translated by the shared flow,
rebuilt by the same filter and uploaded as a COPY in a per-locale folder under the TARGET root,
mirroring the sub-folder structure. Source files are never written. No new file format: PDF,
images and legacy binary Office files have no filter here and are reported as skipped with a
reason, never guessed at.

TOPOLOGY, re-derived from the real write model: T3. SharePoint and OneDrive have no
per-locale object on a file (the multilingual-page feature localizes site pages, not documents),
so a translation is a separate file and WE own the source-to-copy correlation in the persisted id
map (`ConnectorIdMap`: source drive item id + locale -> copy drive item id, reserve-then-fill).
The stock `DocPerLocaleStrategy` is kept and subclassed only to refuse the placeholder create
when nothing was translated (a withheld file never creates an empty copy).

AUTH. App-only client credentials (an Entra app registration, secret pasted), against the fixed
hosts login.microsoftonline.com and graph.microsoft.com. Least privilege is the Graph
`Sites.Selected` application permission plus a per-site grant; a OneDrive for a named user needs
`Files.ReadWrite.All` (tenant-wide), which the runbook says plainly. The token is cached in
memory only and is scrubbed from every message.

HELD-BACK / ALL-OR-NOTHING PER FILE (taxonomy B/F, #640). A rebuilt file cannot keep a stored
translation for one paragraph while taking the rest from this run (it is rebuilt from the
source), so a file with ANY unit held for review is not written at all, neither created nor
updated: the existing copy stays exactly as it was, no content hash is recorded, and the pair is
retried after approval. This is why `variant_exists` is pinned False for the shared flow.

EDITED COPIES (taxonomy B, human work). After each write the copy's content identity (a content
hash when Graph returns one, else the content tag) is stored. Before a re-translation (source
changed) the copy is read back; if its identity no longer matches what we wrote, a human edited
it and it is left alone: zero MT spend, reported, retried only when the copy is deleted or put
back (or the check is switched off with `protect_edited_copies=0`). The upload also carries
`If-Match` with the eTag just read, so an edit between the check and the write loses the race
safely (412) instead of being overwritten.

STEADY STATE (taxonomy C). The source is read at its content identity (cTag or content hash), so
an unchanged file is neither downloaded nor re-translated: the second run lists metadata, finds
the stored hash equal and the copy still present (one metadata GET per pair), and writes nothing.

NO ALEMBIC. State lives in the connection config (`msgraph_hashes`, `msgraph_target_tags`) and
the existing id map.
"""

from __future__ import annotations

import logging
from collections import deque
from urllib.parse import quote, unquote

import httpx

from app.filters.base import InputLimitError
from app.services.connectors import flow, harvest  # noqa: F401 (harvest: parity import)
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    fetch_cap,
    harvest_context_field,
    to_int,
)
from app.services.connectors.base import (
    content_hash as _content_hash,
)
from app.services.connectors.contract import (
    AuthKind,
    Capability,
    ChangeSignals,
    ConnectorPlugin,
    RichText,
    Topology,
)
from app.services.connectors.msgraph_graph import (
    LIST_ENTRY_LIMIT,
    MAX_DEPTH,
    MAX_FILES_CEILING,
    MAX_FOLDERS,
    SIMPLE_UPLOAD_MAX,
    GraphDriveMixin,
    _safe,
    content_identity,
    copy_changed,
    copy_identity,
    enc_path,
    parse_path,
    valid_locale_folder,
    validate_item_id,
    validate_name,
)
from app.services.connectors.strategy import (
    DocPerLocaleStrategy,
    TargetUnverifiable,
    WriteResult,
)
from app.services.connectors.tracking import SelectionTracker

log = logging.getLogger("loc_tms.connectors")

API_VERSION = "v1.0"

HELD_PREFIX = "held-back:"
_HELD_BACK = (
    HELD_PREFIX + " every unit of the file is awaiting review (NEEDS_REVIEW); "
    "no copy was created or changed, an existing copy (if any) is kept"
)
EDITED_PREFIX = "edited-copy:"
_EDITED_MSG = (
    EDITED_PREFIX + " the translated copy was changed in SharePoint after our last write; "
    "it was left untouched. Delete or rename the copy to have it regenerated."
)

# extension -> our filter name. Everything else is reported as skipped with a reason.
FORMATS = {
    "adoc": "adoc",
    "asciidoc": "adoc",
    "arb": "arb",
    "docx": "docx",
    "eml": "eml",
    "epub": "epub",
    "dxf": "dxf",
    "docm": "docx",
    "dotx": "docx",
    "dotm": "docx",
    "xlsx": "xlsx",
    "xlsm": "xlsx",
    "xltx": "xlsx",
    "xltm": "xlsx",
    "pptx": "pptx",
    "pptm": "pptx",
    "potx": "pptx",
    "potm": "pptx",
    "ppsx": "pptx",
    "ppsm": "pptx",
    "html": "html",
    "htm": "html",
    "xhtml": "html",
    "md": "md",
    "markdown": "md",
    "mif": "mif",
    "mdx": "mdx",
    "txt": "txt",
    "json": "json",
    "json5": "json5",
    "jsonc": "jsonc",
    "po": "po",
    "pot": "po",
    "mo": "mo",
    "srt": "srt",
    "sbv": "sbv",
    "sub": "sub",
    "xml": "xml",
    "dita": "dita",
    "ditamap": "dita",
    "bookmap": "dita",
    "dbk": "docbook",
    "dtd": "dtd",
    "ftl": "ftl",
    "stf": "stf",
    "odt": "odt",
    "ott": "odt",
    "ods": "ods",
    "ots": "ods",
    "odp": "odp",
    "otp": "odp",
    "odg": "odg",
    "otg": "odg",
    # Server-page templates; not .aspx: SharePoint site pages are .aspx files.
    "phtml": "phtml",
    "jsp": "jsp",
    "erb": "erb",
    "shtml": "shtml",
    "php": "php",
    "csv": "csv",  # Batch 6: header-detected columns (no per-file mapping here)
    "tsv": "tsv",
    "wiki": "wiki",
    "mediawiki": "wiki",
    "properties": "properties",
    "strings": "strings",
    "resjson": "resjson",
    "ttml": "ttml",
    "dfxp": "ttml",
    "scc": "scc",
    "stl": "stl",
    "yaml": "yaml",
    "yml": "yaml",
    "resx": "resx",
    "resw": "resx",
    "wxl": "wxl",
    "xaml": "xaml",
    "desktop": "desktop",
    "catkeys": "catkeys",
    "hhc": "hhc",
    "hhk": "hhk",
    "hhp": "hhp",
    "rc": "rc",
    "ass": "ass",
    "ssa": "ass",
    "toml": "toml",
    "vsdx": "vsdx",
    "vsdm": "vsdx",
    "plist": "plist",
    "stringsdict": "stringsdict",
    "xcstrings": "xcstrings",
    "xtb": "xtb",
    "xcloc": "xcloc",
    "xtg": "xtg",
    "tag": "xtg",
    "rst": "rst",
    "rest": "rst",
    "tex": "tex",
    "latex": "tex",
    "ltx": "tex",
    "xsl": "xslt",
    "xslt": "xslt",
}
_NO_FILTER_NOTE = {
    "ppt": "legacy .ppt has no file filter (save as .pptx)",
    "pdf": "PDF files are not round-tripped by this connector",
    "doc": "legacy .doc has no file filter (save as .docx)",
    "xls": "legacy .xls has no file filter (save as .xlsx)",
    "msg": "Outlook .msg has no file filter (save as .eml)",
}


SPEC = ConnectorSpec(
    kind="msgraph",
    name="Microsoft 365 Files",
    icon="📁",
    cat="Files / documents",
    blurb="Translate the Word, Excel, PowerPoint, HTML, Markdown and text files in a SharePoint "
    "document library folder or a OneDrive folder. Each translation is uploaded as a copy into a "
    "per-locale folder (the folder tree is mirrored), and the source files are never changed.",
    config_fields=[
        FieldSpec(
            "tenant_id",
            "Directory (tenant) ID",
            "The Entra directory (tenant) ID of the app registration: a GUID or your tenant "
            "domain.",
            placeholder="00000000-0000-0000-0000-000000000000",
        ),
        FieldSpec(
            "client_id",
            "Application (client) ID",
            "The Entra app registration's application (client) ID.",
            placeholder="00000000-0000-0000-0000-000000000000",
        ),
        FieldSpec(
            "site_url",
            "SharePoint site URL",
            "The site that holds the library, e.g. "
            "https://contoso.sharepoint.com/sites/Localization. Use exactly one of Site URL, "
            "Site ID, Drive ID or OneDrive user.",
            required=False,
            placeholder="https://contoso.sharepoint.com/sites/Localization",
        ),
        FieldSpec(
            "site_id",
            "SharePoint site ID",
            "Alternative to the Site URL: the Graph site id (host,collection-guid,web-guid). "
            "Use when the app may not look a site up by URL.",
            required=False,
            placeholder="contoso.sharepoint.com,guid,guid",
        ),
        FieldSpec(
            "library",
            "Document library",
            "The library's display name on that site. Blank = the site's default Documents "
            "library.",
            required=False,
            placeholder="Documents",
        ),
        FieldSpec(
            "drive_id",
            "Drive ID",
            "Alternative: a Graph drive id (a library or a OneDrive).",
            required=False,
            placeholder="b!abc...",
        ),
        FieldSpec(
            "onedrive_user",
            "OneDrive user",
            "Alternative: the user principal name whose OneDrive to use. Needs the tenant-wide "
            "Files.ReadWrite.All app permission.",
            required=False,
            placeholder="alex@contoso.com",
        ),
        FieldSpec(
            "source_folder",
            "Source folder",
            "Folder path inside the drive that holds the files to translate, e.g. Source. "
            "Never the drive root. Must not contain the target root.",
            placeholder="Source",
        ),
        FieldSpec(
            "target_root",
            "Target root folder",
            "Folder path inside the same drive that receives one sub-folder per locale. Blank "
            "= Translations. Must not be inside the source folder.",
            required=False,
            placeholder="Translations",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "The language of the source files, e.g. en-US.",
            placeholder="en-US",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated locale codes; each becomes a folder name under the target root.",
            required=False,
            placeholder="fr-FR, de-DE",
        ),
        FieldSpec(
            "extensions",
            "File types",
            "Optional comma-separated subset of adoc, arb, docx, eml, epub, dxf, xlsx, pptx, "
            "html, md, mif, mdx, txt, json, json5, jsonc, po, mo, srt, sbv, sub, xml, dita, "
            "docbook, dtd, ftl, stf, odt, ods, odp, odg, phtml, jsp, erb, shtml, php, csv, tsv, "
            "wiki, properties, strings, resjson, ttml, scc, stl, yaml, resx, wxl, xaml, desktop, "
            "catkeys, hhc, hhk, hhp, rc, ass, toml, vsdx, plist, stringsdict, xcstrings, xtb, "
            "xcloc, xtg, rst, tex, xslt. Blank = all of them.",
            required=False,
            placeholder="docx, xlsx",
        ),
        FieldSpec(
            "include_subfolders",
            "Include subfolders",
            "1 to translate files in sub-folders too (the tree is mirrored), 0 for the top "
            "level only.",
            required=False,
            placeholder="1",
        ),
        FieldSpec(
            "max_files",
            "Max files",
            "Most files translated per run (default 50, at most 500). A source tree with "
            "more than 500 folders or 5000 files is refused: narrow the Source folder.",
            required=False,
            placeholder="50",
        ),
        FieldSpec(
            "max_file_mb",
            "Max file size (MB)",
            "Files larger than this are skipped and reported. Default 20.",
            required=False,
            placeholder="20",
        ),
        FieldSpec(
            "protect_edited_copies",
            "Protect edited copies",
            "1 (default) leaves a translated copy alone when someone edited it in SharePoint "
            "after our last write. Set 0 only if live QA shows false positives.",
            required=False,
            placeholder="1",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "client_secret",
            "Client secret",
            "A client secret value of the app registration (Certificates and secrets). Used "
            "for app-only sign-in; never logged.",
            secret=True,
            placeholder="the secret VALUE, not the secret ID",
        ),
    ],
    setup_steps=[
        "In the Entra admin center, register an app (App registrations, New registration) "
        "and note its Application (client) ID and Directory (tenant) ID.",
        "Under Certificates and secrets create a client secret and paste its VALUE here.",
        "Under API permissions add the Microsoft Graph APPLICATION permission Sites.Selected "
        "and grant admin consent, then grant the app write access to your one site (the "
        "runbook has the Graph Explorer call). For a OneDrive use Files.ReadWrite.All instead.",
        "Create the source folder in the library, put the files in it, and enter the folder "
        "path above. Translations appear in the target root, one folder per locale.",
    ],
    other_side="Change detection is poll-based: run on a schedule. A file's content tag "
    "(cTag) changes only when its content changes, and each (file, locale) copy is re-checked "
    "against a stored content hash, so unchanged files are neither downloaded nor "
    "re-translated. A copy that someone edited in SharePoint after our last write is never "
    "overwritten. No webhook receiver in scope.",
    docs_url="https://learn.microsoft.com/en-us/graph/api/resources/onedrive",
    supports_oauth=False,
)


def scope_fingerprint(cfg) -> tuple:
    """The config values that decide WHAT the stored app secret reaches: the app, the drive,
    the folders. Changing any of them clears the stored credential (store_part2
    `_pin_secret_to_host`), the way the MCP connector's tool mapping does: an edit here widens
    or moves what the secret can read and write as surely as a new host would."""
    c = cfg or {}
    keys = (
        "tenant_id",
        "client_id",
        "site_url",
        "site_id",
        "library",
        "drive_id",
        "onedrive_user",
        "source_folder",
        "target_root",
    )
    return tuple(str(c.get(k) or "").strip().lower().strip("/") for k in keys)


class MsGraphStrategy(DocPerLocaleStrategy):
    """The stock T3 id-map lifecycle, plus: a withheld or broken payload never reaches the
    create path (no empty placeholder copy for a file nothing was translated for)."""

    def write_target(self, client, record_id, target_locale, fields, *, draft=True):
        fields = fields or {}
        if fields.get("held"):
            return WriteResult(False, False, message=_HELD_BACK)
        if fields.get("error"):
            return WriteResult(False, False, message=str(fields["error"]))
        return super().write_target(client, record_id, target_locale, fields, draft=draft)


class MsGraphConnector(GraphDriveMixin, Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="msgraph",
        topology=Topology.T3,  # a separate copy per locale, correlated by our id map
        auth=(AuthKind.API_KEY,),  # app-only client credentials (a pasted client secret)
        api_version=API_VERSION,
        rich_text=RichText.NONE,  # the file filters own the structure
        regions="fixed url",  # graph.microsoft.com / login.microsoftonline.com
        change_signals=ChangeSignals(
            webhook_sig=None,  # no webhook receiver in scope
            poll_field="cTag",  # content tag / content hash, never lastModified
            stale_flag=None,
            thin_payload=False,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = MsGraphStrategy()
        self.sel = SelectionTracker(noun="file", label="msgraph", loud_markers=("no file filter",))
        self._drive: str | None = None
        self._tok = ""
        self._tok_exp = 0.0
        self._tags: dict = dict(self.config.get("msgraph_target_tags") or {})
        self._state_cache: dict = {}
        self.edited: list = []  # (article_id, locale) copies left alone because a human edited them

    # --------------------------------------------------------------- config
    def source_locale(self) -> str:
        loc = str(self.config.get("source_locale") or "").strip()
        if not loc:
            raise ConnectorError(
                "Set the connection's Source locale (e.g. en-US): the language of the files."
            )
        return loc

    def _source_parts(self) -> list[str]:
        parts = parse_path(self.config.get("source_folder"), "Source folder")
        if not parts:
            raise ConnectorError(
                "Source folder is required: the drive root cannot be the source (translated "
                "copies would be picked up as source files)."
            )
        return parts

    def _target_parts(self) -> list[str]:
        return parse_path(self.config.get("target_root") or "Translations", "Target root folder")

    def check_config(self) -> None:
        """Validate every operator-supplied string, fail-closed, before any request."""
        self._tenant()
        self._client_id()
        self._secret()
        self._drive_choice()
        src, tgt = self._source_parts(), self._target_parts()
        a = [p.lower() for p in src]
        b = [p.lower() for p in tgt]
        if not b:
            raise ConnectorError("Target root folder cannot be the drive root.")
        if b[: len(a)] == a or a[: len(b)] == b:
            raise ConnectorError(
                "Source folder and Target root folder overlap: translated copies would be "
                "picked up as source files. Choose two separate folders."
            )
        self.extensions()
        self.max_bytes()

    def extensions(self) -> set:
        raw = str(self.config.get("extensions") or "").replace(";", ",").replace("\n", ",")
        names = [x.strip().lower().lstrip(".") for x in raw.split(",") if x.strip()]
        if not names:
            return set(FORMATS)
        out = set()
        for n in names:
            if n in FORMATS:
                out.add(n)
            elif n in set(FORMATS.values()):
                out.update(e for e, f in FORMATS.items() if f == n)
            else:
                raise ConnectorError(
                    f"Unsupported file type {_safe(n)} in File types (supported: "
                    f"{', '.join(sorted(set(FORMATS.values())))})."
                )
        return out

    def max_bytes(self) -> int:
        mb = to_int(self.config.get("max_file_mb"), 20)
        return max(1, min(mb, 200)) * 1024 * 1024

    def _recursive(self) -> bool:
        raw = str(self.config.get("include_subfolders") or "1").strip().lower()
        return raw not in ("0", "false", "no", "off")

    def _protect(self) -> bool:
        raw = str(self.config.get("protect_edited_copies") or "1").strip().lower()
        return raw not in ("0", "false", "no", "off")

    def locale_folder(self, loc: str) -> str:
        from app.services.connectors import store

        return store._canonical_locale(str(loc).strip())

    # ----------------------------------------------------------- test / list
    def test(self) -> tuple[bool, str]:
        try:
            self.check_config()
            with self._client() as c:
                drive = self.drive_id(c)
                src = self._source_parts()
                item = self.item_by_path(c, src)
                if item is None or "folder" not in item:
                    return False, (
                        f"Connected, but the source folder {_safe('/'.join(src))} was not found "
                        f"in drive {_safe(drive)}."
                    )
            return True, f"Connected to Microsoft 365 Files (source folder {_safe('/'.join(src))})."
        except ConnectorError as e:
            return False, self._scrub(e)
        except Exception as e:
            return False, self._scrub(f"{type(e).__name__}: {e}")

    def containers(self) -> list[dict]:
        try:
            with self._client() as c:
                drive = self.drive_id(c)
            return [{"id": drive, "name": f"drive {drive}", "tables": []}]
        except Exception:
            return []

    # ------------------------------------------------------------- discovery
    def _list_source(self, c) -> list[dict]:
        """Metadata for every file under the source folder (bounded). Folders are walked
        breadth-first; nothing is downloaded here."""
        src = self._source_parts()
        root = self.item_by_path(c, src)
        if root is None or "folder" not in root:
            raise ConnectorError(
                f"Source folder {_safe('/'.join(src))} was not found in the drive."
            )
        out: list[dict] = []
        queue = deque([(root["id"], (), 0)])
        folders = 0
        while queue:
            folder_id, rel, depth = queue.popleft()
            folders += 1
            if folders > MAX_FOLDERS:
                # Before any download or MT: a Source folder set to a whole site must not be
                # walked folder by folder or translated a slice at a time.
                raise ConnectorError(
                    f"The source folder holds more than {MAX_FOLDERS} folders. Nothing was "
                    "translated. Narrow the Source folder, or set Include subfolders to 0."
                )
            for it in self.children(c, folder_id):
                if it.get("deleted"):
                    continue
                name = str(it.get("name") or "")
                if it.get("remoteItem"):
                    continue  # a shortcut (possibly to the target tree): never walked or read
                if "folder" in it:
                    if self._recursive() and depth < MAX_DEPTH and it.get("id"):
                        queue.append((it["id"], (*rel, name), depth + 1))
                    continue
                if "file" not in it:
                    continue  # OneNote notebooks and other packages
                out.append({"item": it, "rel": rel})
                if len(out) > LIST_ENTRY_LIMIT:
                    raise ConnectorError(
                        f"The source folder holds more than {LIST_ENTRY_LIMIT} files. Narrow "
                        "the Source folder."
                    )
        return out

    def _classify(self, entry: dict):
        """(article dict | None, reason | None) for one listed file."""
        it, rel = entry["item"], tuple(entry["rel"])
        name = str(it.get("name") or "")
        relpath = "/".join((*rel, name))
        if name.startswith("~$"):
            return None, (relpath, "Office lock file")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in FORMATS:
            note = _NO_FILTER_NOTE.get(ext, f"no file filter for .{ext or 'unknown'} files")
            return None, (relpath, f"no file filter: {note}")
        if ext not in self.extensions():
            return None, (relpath, "file type not selected on this connection")
        try:
            validate_name(name, "file name")
            for seg in rel:
                validate_name(seg, "folder name")
        except ConnectorError:
            return None, (
                relpath,
                "the file or a folder name has a character SharePoint paths cannot carry "
                "(for example # or %)",
            )
        size = int(it.get("size") or 0)
        if size <= 0:
            return None, (relpath, "empty file")
        if size > self.max_bytes():
            return None, (
                relpath,
                f"file is {size // (1024 * 1024)} MB, over the "
                f"{self.max_bytes() // (1024 * 1024)} MB limit",
            )
        ident = content_identity(it)
        return {
            "id": str(it["id"]),
            "name": name,
            "rel": rel,
            "relpath": relpath,
            "fmt": FORMATS[ext],
            "size": size,
            "ident": ident,
            "chash": _content_hash(ident, FORMATS[ext]),
        }, None

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Files to translate. Explicit `article_ids` (drive item ids) win and are SCOPE-
        CHECKED against the configured drive and source folder (taxonomy A): an id outside
        them, a folder, an unsupported file, or anything malformed is refused fail-closed.
        Otherwise the source tree is listed and the shared `cap` is filled ROUND-ROBIN across
        sub-folders, so one folder can never starve the others (taxonomy E)."""
        self.check_config()
        cap = min(fetch_cap(cap or self.config.get("max_files"), 50), MAX_FILES_CEILING)
        with self._client() as c:
            if article_ids:
                return self._explicit(c, list(article_ids)[:cap])
            articles: list[dict] = []
            skipped: dict = {}
            for entry in self._list_source(c):
                art, why = self._classify(entry)
                if art is None:
                    skipped[why[0]] = why[1]
                else:
                    articles.append(art)
            articles.sort(key=lambda a: a["relpath"].lower())
            groups: dict = {}
            for a in articles:
                groups.setdefault(tuple(x.lower() for x in a["rel"]), []).append(a)
            order = sorted(groups)
            chosen: list[dict] = []
            i = 0
            while len(chosen) < cap and any(i < len(groups[k]) for k in order):
                for k in order:
                    if i < len(groups[k]) and len(chosen) < cap:
                        chosen.append(groups[k][i])
                i += 1
            for a in articles:
                if a not in chosen:
                    skipped[a["relpath"]] = "beyond the Max files cap for this run"
            self.sel.record_selection([a["relpath"] for a in chosen], skipped)
            return chosen

    def _explicit(self, c, ids: list) -> list[dict]:
        src = [p.lower() for p in self._source_parts()]
        tgt = [p.lower() for p in self._target_parts()]
        drive = self.drive_id(c)
        out: list[dict] = []
        for raw in ids:
            iid = validate_item_id(raw, "file id")
            it = self.item_by_id(c, iid)
            if it is None:
                self.fetch_notes.append({"id": iid, "meta": {}, "error": f"{iid}: file not found."})
                continue
            pr = it.get("parentReference") or {}
            parent = str(pr.get("path") or "")
            marker = parent.find("root:")
            if "file" not in it or str(pr.get("driveId") or drive) != drive or marker < 0:
                raise ConnectorError(f"{iid}: not a file in the configured drive; refused.")
            rel_all = [unquote(p) for p in parent[marker + 5 :].strip("/").split("/") if p != ""]
            low = [p.lower() for p in rel_all]
            if low[: len(src)] != src or low[: len(tgt)] == tgt:
                raise ConnectorError(
                    f"{iid}: the file is outside the configured source folder; refused."
                )
            art, why = self._classify({"item": it, "rel": tuple(rel_all[len(src) :])})
            if art is None:
                self.fetch_notes.append({"id": iid, "meta": {}, "error": f"{iid}: {why[1]}."})
                continue
            out.append(art)
        return out

    # ------------------------------------------------------- load and render
    def load(self, art: dict):
        """(units, original bytes, filter) for one file: download, then the file filter."""
        from app.filters import get_filter

        filt = get_filter(art["fmt"])
        with self._client() as c:
            data = self.download(c, art["id"], self.max_bytes())
        if not data:
            raise ConnectorError("the download was empty")
        parsed = filt.parse(data)
        # Batch 4 gate: a macro package under a non-macro name is never translated or
        # written back under that name.
        from app.filters.zip_guard import MacroExtensionMismatch, refuse_disguised_macro

        name = str(art.get("name") or "")
        try:
            refuse_disguised_macro(data, name.rsplit(".", 1)[-1] if "." in name else "")
        except MacroExtensionMismatch as exc:
            raise ConnectorError(str(exc)) from exc
        units = [u for u in parsed.units if (u.text or "").strip()]
        return units, data, filt

    def render(self, ex: dict, loc: str, texts: dict) -> bytes:
        """The translated file bytes: the filter rebuilds the ORIGINAL with `texts`, then the
        result must still open and keep the same number of units (a corrupt rebuild is never
        uploaded)."""
        filt, original = ex["filt"], ex["original"]
        out = filt.build_localized(original, texts, self.locale_folder(loc))
        before = len(filt.parse(original).units)
        after = len(filt.parse(out).units)
        # A localized build may legitimately add or drop CLDR plural branches (ARB / ICU /
        # go-i18n into Russian): then the plain rebuild must keep the structure instead.
        if before != after and len(filt.parse(filt.build(original, texts)).units) != before:
            raise ConnectorError(
                f"the rebuilt file changed structure ({before} -> {after} units); not uploaded"
            )
        return out

    def target_parts(self, loc: str, art: dict) -> list[str]:
        folder = self.locale_folder(loc)
        if not valid_locale_folder(folder):
            raise ConnectorError(f"Locale {_safe(loc)} cannot be used as a folder name.")
        return [*self._target_parts(), folder, *art["rel"], art["name"]]

    # ------------------------------------------- pair state (probe, edited copy)
    def _pair_state(self, aid: str, loc: str) -> dict:
        key = (aid, loc)
        if key in self._state_cache:
            return self._state_cache[key]
        from app.services.connectors import idmap

        row = idmap.get(self.connection_id, aid, loc) if self.connection_id is not None else None
        if row is None or row.status != idmap.DONE or not row.target_id:
            st = {"state": "none"}
        else:
            try:
                with self._client() as c:
                    it = self.item_by_id(c, row.target_id)
                st = (
                    {"state": "absent"}
                    if it is None
                    else {"state": "present", "ident": copy_identity(it)}
                )
            except Exception:  # cannot tell: never treat a failed probe as "deleted"
                st = {"state": "unknown"}
        self._state_cache[key] = st
        return st

    def is_current(self, aid: str, loc: str) -> bool:
        """The map says done AND the copy is still there (or we could not tell)."""
        return self._pair_state(aid, loc)["state"] in ("present", "unknown")

    def parked(self, aid: str, loc: str, chash: str) -> bool:
        """True when this pair's source changed but its copy was edited by a human since our
        last write: it is left alone before any download or MT spend. Never true for an
        unchanged source, a pair we never wrote, or a copy that is gone."""
        if not self._protect():
            return False
        key = f"{aid}:{loc}"
        if (self.config.get("msgraph_hashes") or {}).get(key) == chash:
            return False
        recorded = self._tags.get(key)
        if not recorded:
            return False
        st = self._pair_state(aid, loc)
        if st["state"] != "present":
            return False
        if copy_changed(recorded, st.get("ident") or ""):
            if (aid, loc) not in self.edited:
                self.edited.append((aid, loc))
            return True
        return False

    def _record_tag(self, pair: str, ident: str) -> None:
        if not pair or not ident:
            return
        self._tags[pair] = ident
        if self.connection_id is not None:
            from app.services.connectors import store

            store.patch_config_merge(self.connection_id, "msgraph_target_tags", {pair: ident})

    # ------------------------------------- the client the T3 strategy drives
    def find_preexisting(self, source_id: str, loc: str, fields: dict):
        """An item already at the copy's path that our map never made (a human-made
        translation): the strategy then fails closed (needs_link). A zero-byte file is the
        residue of an interrupted build of ours, not a human's, and is resumed instead."""
        with self._client() as c:
            it = self.item_by_path(c, fields["target_parts"])
        if it is None:
            return None
        if "file" in it and int(it.get("size") or 0) == 0:
            return None
        return str(it.get("id") or "") or None

    def _item_rel(self, item: dict, drive: str):
        """The lower-cased folder path of an item inside the drive (root-relative, its own
        name included), or None when Graph did not say / it is in another drive."""
        pr = (item or {}).get("parentReference") or {}
        parent = str(pr.get("path") or "")
        marker = parent.find("root:")
        if marker < 0 or str(pr.get("driveId") or drive) != drive:
            return None
        segs = [unquote(p) for p in parent[marker + 5 :].strip("/").split("/") if p != ""]
        return [p.lower() for p in (*segs, str(item.get("name") or ""))]

    def _under_target(self, rel) -> bool:
        tgt = [p.lower() for p in self._target_parts()]
        src = [p.lower() for p in self._source_parts()]
        return bool(rel) and rel[: len(tgt)] == tgt and rel[: len(src)] != src

    def _check_write_scope(self, c, parts: list[str]) -> str:
        """Before a CREATE: the nearest existing ancestor of the copy's path must be a real
        folder (not a shortcut) that sits inside the target root. Source files are never
        written: a pre-made shortcut or alias that would route the path into the source tree,
        or anywhere outside the target root, is refused. Returns an error text or ''."""
        drive = self.drive_id(c)
        for k in range(len(parts) - 1, 0, -1):
            it = self.item_by_path(c, parts[:k])
            if it is None:
                continue
            if it.get("remoteItem") or "folder" not in it:
                return "an ancestor of the copy's path is a shortcut or not a folder; refused"
            if not self._under_target(self._item_rel(it, drive)):
                return "the copy's folder resolves outside the target root; refused"
            return ""
        return ""

    def create_target(self, source_id: str, loc: str, fields: dict):
        parts = fields["target_parts"]
        with self._client() as c:
            bad = self._check_write_scope(c, parts)
            if bad:
                return None, bad
            drive = quote(self.drive_id(c), safe="")
            r, item = self.put_content(
                c,
                f"/drives/{drive}/root:/{enc_path(parts)}:/content",
                b"",
                conflict="fail",
                create=True,
            )
            if r.status_code in (200, 201) and item and item.get("id"):
                self._record_tag(fields.get("pair", ""), copy_identity(item))
                return str(item["id"]), None
            if r.status_code == 409:
                it = self.item_by_path(c, parts)
                if it and "file" in it and int(it.get("size") or 0) == 0 and it.get("id"):
                    return str(it["id"]), None  # resume our own interrupted placeholder
                return None, "a different item already exists at the copy's path; not overwritten"
            return None, self._graph_error(r)

    def fill_target(self, target_id: str, fields: dict) -> WriteResult:
        data = fields["content"]
        pair = fields.get("pair", "")
        with self._client() as c:
            cur = self.item_by_id(c, target_id)
            if cur is None:
                return WriteResult(
                    False, False, message="the copy disappeared before it could be written"
                )
            if "file" not in cur:
                return WriteResult(False, False, message="the copy's path is not a file")
            if cur.get("remoteItem") or not self._under_target(
                self._item_rel(cur, self.drive_id(c))
            ):
                return WriteResult(
                    False,
                    False,
                    message="the copy is not inside the target root; refused (source files "
                    "are never written)",
                )
            now = copy_identity(cur)
            recorded = self._tags.get(pair, "")
            # A zero-byte item is our own placeholder (an unfinished build), never a human edit.
            if (
                self._protect()
                and int(cur.get("size") or 0) > 0
                and recorded
                and copy_changed(recorded, now)
            ):
                return WriteResult(False, False, target_id=target_id, message=_EDITED_MSG)
            drive = quote(self.drive_id(c), safe="")
            if len(data) <= SIMPLE_UPLOAD_MAX:
                r, item = self.put_content(
                    c,
                    f"/drives/{drive}/items/{quote(validate_item_id(target_id), safe='')}/content",
                    data,
                    if_match=str(cur.get("eTag") or ""),
                )
            else:
                r, item = self.upload_session(
                    c, target_id, data, if_match=str(cur.get("eTag") or "")
                )
            if r.status_code == 412:
                return WriteResult(False, False, target_id=target_id, message=_EDITED_MSG)
            if item is None or not item.get("id"):
                return WriteResult(False, False, target_id=target_id, message=self._graph_error(r))
            self._record_tag(pair, copy_identity(item))
            return WriteResult(True, False, target_id=target_id, message="written")

    def verify_target(self, target_id: str) -> bool:
        try:
            with self._client() as c:
                return self.item_by_id(c, target_id) is not None
        except ConnectorError as e:
            raise TargetUnverifiable(str(e)) from e
        except httpx.TransportError as e:
            raise TargetUnverifiable(str(e)) from e

    # ------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for art in self.discover_articles():
            units, _data, _filt = self.load(art)
            for u in units:
                out.append(
                    PulledItem(
                        external_id=art["id"],
                        field=f"body:{u.p_index}",
                        text=u.text,
                        title=art["relpath"][:60],
                    )
                )
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Microsoft 365 Files writes translated copies - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []


# ===========================================================================
# Translate flow: thin hooks over the shared engine (flow.run_translate_flow).
# Poll-only, no watermark: the content identity + content-hash gate is the change detector.
# ===========================================================================
def _msgraph_preflight(ctx):
    conn = ctx.connector
    src = conn.source_locale().lower()
    conn.check_config()
    enabled = {loc for loc in ctx.locales if valid_locale_folder(conn.locale_folder(loc))}
    return enabled, {"source": src}


def _msgraph_partition(locales, enabled, extra):
    src = (extra or {}).get("source", "")
    rejected = [loc for loc in locales if loc not in enabled]
    usable = [loc for loc in locales if loc in enabled and loc != src]
    return rejected, usable


def _msgraph_prepare(ctx, art):
    conn = ctx.connector
    aid, chash = art["id"], art["chash"]
    skeleton = {"article_id": aid, "title": art["name"]}

    # An unchanged file whose copies are all current (or parked) is neither downloaded nor
    # parsed: stage 6 of the flow then skips every locale.
    def idle(loc):
        if conn.parked(aid, loc, chash):
            return True
        return ctx.hashes.get(f"{aid}:{loc}") == chash and conn.is_current(aid, loc)

    if all(idle(loc) for loc in ctx.usable):
        return flow.ArticlePrep(
            aid=aid,
            chash=chash,
            title_text="",
            body_units=[],
            extra_skeleton=skeleton,
            doc_title=art["name"][:60],
            extra={"lazy": True, "connector": conn, "art": art},
        )
    try:
        units, data, filt = conn.load(art)
    except InputLimitError:
        raise
    except Exception as e:  # a bad download or a corrupt file must not stop the other files
        msg = conn._scrub(str(e))[:140] if str(e) else type(e).__name__
        return flow.SkipArticle(
            "unreadable_skipped",
            error=f"{art['relpath']}: could not read the file ({type(e).__name__}: {msg})",
        )
    if not units:
        return flow.SkipArticle("empty_skipped")
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text="",
        body_units=units,
        extra_skeleton=skeleton,
        doc_title=art["name"][:60],
        extra={"original": data, "filt": filt, "art": art, "connector": conn},
    )


def _msgraph_is_suppressed(connector, aid, loc):
    return connector.is_current(aid, loc)


def _msgraph_parked(connector, aid, loc, chash):
    return connector.parked(aid, loc, chash)


def _msgraph_build_payload(prep, loc, seg0_target, mapping):
    ex = prep.extra
    if ex.get("lazy"):
        # Nothing was loaded for this file: it is unchanged with a current copy, or its copy
        # was edited by hand. Only an explicit retry reaches here (a normal run skips it).
        if (prep.aid, loc) in ex["connector"].edited:
            return {"error": _EDITED_MSG}
        return {"error": "the file is unchanged and its copy is current; nothing to rewrite"}
    texts = {
        str(u.p_index): mapping[str(u.p_index)]
        for u in prep.body_units
        if str(u.p_index) in mapping
    }
    if not texts:
        return {"held": True}
    conn = ex["connector"]
    try:
        content = conn.render(ex, loc, texts)
        parts = conn.target_parts(loc, ex["art"])
    except Exception as e:  # the pair fails alone; nothing is created or changed
        return {"error": f"{ex['art']['relpath']}: {type(e).__name__}: {conn._scrub(str(e))[:160]}"}
    return {"pair": f"{prep.aid}:{loc}", "content": content, "target_parts": parts}


def _is_held_back(f):
    return str((f or {}).get("error", "")).startswith(HELD_PREFIX)


def _held_back(ctx):
    return [f for f in ctx.failures if _is_held_back(f)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f)]


def _dropped_as_source(ctx):
    return [loc for loc in ctx.locales if loc not in ctx.usable and loc not in ctx.rejected]


def _msgraph_compute_ok(ctx):
    if not ctx.usable:
        return False
    real = _real_failures(ctx)
    unreadable = ctx.counters["unreadable_skipped"]
    if ctx.created + ctx.updated > 0:
        return True
    return not real and not unreadable


def _msgraph_build_message(ctx):
    conn = ctx.connector
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} translated cop(ies) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} file(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) copy(ies).")
    if ctx.counters["empty_skipped"]:
        parts.append(f"Skipped {ctx.counters['empty_skipped']} file(s) with no translatable text.")
    if ctx.counters["unreadable_skipped"]:
        parts.append(
            f"{ctx.counters['unreadable_skipped']} file(s) could not be read (see errors)."
        )
    if ctx.counters["parked_skipped"]:
        parts.append(
            f"Left {ctx.counters['parked_skipped']} copy(ies) untouched because they were "
            "edited in SharePoint after our last write (delete a copy to regenerate it)."
        )
    if ctx.rejected:
        parts.append(f"Rejected {len(ctx.rejected)} locale(s): {', '.join(ctx.rejected)}.")
    dropped = _dropped_as_source(ctx)
    if dropped:
        parts.append(
            f"Skipped {len(dropped)} target(s) equal to the source locale ({', '.join(dropped)})."
        )
    held = _held_back(ctx)
    if held:
        parts.append(
            f"Held back {len(held)} (file, locale) pair(s) awaiting review (NEEDS_REVIEW): no "
            "copy was created or changed; they will be re-attempted next run."
        )
    sel = conn.sel.selection_summary()
    if sel:
        parts.append(sel)
    if ctx.errors:
        parts.append("Errors: " + "; ".join(str(e) for e in ctx.errors[:3]))
    real = _real_failures(ctx)
    if real and not ctx.errors:
        parts.append(
            "Errors: "
            + "; ".join(
                f"{f.get('article_id')}/{f.get('locale')}: {f.get('error')}" for f in real[:3]
            )
        )
    if ctx.budget.ceiling_hit:
        parts.append(ctx.budget.message("file write(s)").strip())
        log.warning(
            "msgraph_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _msgraph_build_result(ctx):
    conn = ctx.connector
    return {
        "files": len(ctx.articles),
        "locales": ctx.usable,
        "created": ctx.created,
        "updated": ctx.updated,
        "skipped": ctx.skipped,
        "held_back": len(_held_back(ctx)),
        "empty_skipped": ctx.counters["empty_skipped"],
        "unreadable": ctx.counters["unreadable_skipped"],
        "edited_copies": [{"article_id": a, "locale": loc} for a, loc in conn.edited],
        "rejected": ctx.rejected,
        "dropped_source": _dropped_as_source(ctx),
        "files_selected": conn.sel.selected_sorted(),
        "files_skipped": conn.sel.skipped_dict(),
        "failures": _real_failures(ctx),
    }


def msgraph_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate the files in the connection's source folder into one or more locales, each
    uploaded as a copy in the locale's folder under the target root via the T3 id-map strategy.
    Thin wrapper over flow.run_translate_flow; poll-only with content-hash suppression.
    only_pairs restricts the write loop to exactly those (drive item id, locale) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="msgraph",
        no_conn_msg="No such Microsoft 365 Files connection.",
        hash_key="msgraph_hashes",
        probe_label="Microsoft 365 Files",
        preflight=_msgraph_preflight,
        prepare=_msgraph_prepare,
        is_suppressed=_msgraph_is_suppressed,
        build_payload=_msgraph_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_msgraph_compute_ok,
        build_message=_msgraph_build_message,
        build_result=_msgraph_build_result,
        held_prefix=HELD_PREFIX,
        report_held_updates=False,
        # A rebuilt file replaces the whole copy, so a file with ANY held unit is not written
        # at all (not created, not updated): the existing copy keeps every stored translation.
        variant_exists=lambda connector, aid, loc: False,
        parked=_msgraph_parked,
        extra_counters=("empty_skipped", "unreadable_skipped", "parked_skipped"),
        partition=_msgraph_partition,
    )
    return flow.run_translate_flow(
        spec,
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )
