"""Google Docs connector: a Drive folder of Google Docs, a T3 consumer.

Documented in: docs/architecture/connectors.md

WHAT IT DOES. A folder-based round trip. Every Google Doc under the connection's SOURCE Drive
folder (My Drive or a shared drive) is translated and written as a NEW Google Doc in
`<target folder>/<locale>/<same sub-folders>/<same name>`. Source documents are never written.

FIDELITY, DECIDED WITH EVIDENCE. The translated Doc is a
Drive `files.copy` of the source, so styles, lists, tables, images, headers, footers, page setup
and tabs survive exactly, and then ONE atomic Docs `documents.batchUpdate` replaces the text of each
unit (insert the translation, delete the old text, put each span's text style back, links
included). The alternative, export to DOCX + our DOCX filter + re-import with conversion, rewrites
the whole document through Google's converter, which is documented to be lossy for Docs-only
features (tabs, smart chips, suggestions), and costs two extra conversions. The Docs-native route
touches only the text. Its limits are reported, never hidden: a document with pending
suggestions is skipped (indices would lie), non-text elements (images, footnote references, chips,
equations) are left exactly in place, and text inside drawings or images is not translated.

TOPOLOGY, re-derived from the real write model: T3. A Doc has no per-locale object, so a
translation is a separate Doc and WE own the source-to-copy correlation in the persisted id map
(`ConnectorIdMap`: source file id + locale -> copy file id, reserve-then-fill). `files.copy` makes
the copy under a visible "[Translating] " name; only a completed, verified batchUpdate renames it to
the real name, so a half-built copy can never be mistaken for a finished translation. An UPDATE (the
source changed) rewrites the existing translated Doc IN PLACE with one atomic batchUpdate under
`requiredRevisionId` whenever the source's structure is unchanged and the Doc still says what we
wrote (same file id: links, sharing and comments survive; `_update_in_place`). Otherwise a fresh
copy is built, gets the sharing a person added to the old one (`_copy_sharing`, no notification
email), and the old one is moved to the Drive trash (30 days recoverable, never a permanent delete)
only AFTER the id map names the replacement (`after_commit`); that link change is warned. If the
sharing cannot be copied the old Doc is kept, not trashed, with a warning. Google Docs has no draft
state: every write here is LIVE for anyone reading the translated Doc.

AUTH. A service account (JSON key pasted into the secret store, signed JWT against the fixed
oauth2.googleapis.com token endpoint) with the two folders shared to it. Scope = `drive`: the
non-sensitive `drive.file` scope only reaches files an app created or a user opened through a
picker, which excludes folders shared with a service account. The account sees only what is
shared to it, so the reach is the shared folders. Optional domain-wide delegation (Impersonate
user) makes the copies owned by a person; it is never required.

STORAGE QUOTA. A service account owns no Drive storage, so copies it creates in a plain My Drive
folder fail with storageQuotaExceeded. The supported shape is a SHARED DRIVE (the organisation
owns the files); My Drive needs Impersonate user. The error is translated into that advice.

HELD-BACK / ALL-OR-NOTHING PER DOCUMENT (taxonomy B/F, #640). A document with ANY unit held for
review is neither created nor updated: no copy appears, an existing translation stays exactly as
it was, no content hash is recorded, and the pair is retried after approval.

EDITED COPIES (taxonomy B, human work). After each write the copy's Drive modifiedTime is stored.
Before a re-translation the copy is read back; if it moved, a human edited it and it is left
alone: zero MT spend, reported, regenerated only when the copy is deleted (switch:
`protect_edited_copies=0`). The Docs batchUpdate also carries `requiredRevisionId` of the copy it
just read, so an edit between the read and the write loses the race safely (400) instead of being
overwritten.

STEADY STATE (taxonomy C). The listing carries each Doc's modifiedTime; a stored content digest
per Doc means an unchanged Doc is neither read nor re-translated, and a rename or share-only touch
re-reads the Doc once (no MT) and finds the same digest.

NO ALEMBIC. State lives in the connection config (`gdocs_hashes`, `gdocs_seen`,
`gdocs_target_tags`) and the existing id map.
"""

from __future__ import annotations

import json
import logging
from collections import deque

import httpx

from app.filters.base import InputLimitError, check_unit_texts
from app.services.connectors import flow, harvest  # noqa: F401 (harvest: parity import)
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    fetch_cap,
    harvest_context_field,
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
from app.services.connectors.gdocs_google import (
    DOC_MIME,
    FOLDER_MIME,
    MAX_DEPTH,
    MAX_UNITS_PER_DOC,
    PENDING_PREFIX,
    SHORTCUT_MIME,
    GoogleMixin,
    _safe,
    _style_key,
    build_requests,
    build_requests_inplace,
    doc_shape,
    layout_map,
    parse_folder_ref,
    parse_units,
    strip_tokens,
    text_digest,
    valid_locale_folder,
    valid_name,
    validate_file_id,
)
from app.services.connectors.strategy import (
    DocPerLocaleStrategy,
    TargetUnverifiable,
    WriteResult,
)
from app.services.connectors.tracking import SelectionTracker

log = logging.getLogger("loc_tms.connectors")

API_VERSION = "v1"

HELD_PREFIX = "held-back:"
_HELD_BACK = (
    HELD_PREFIX + " every unit of the document is awaiting review (NEEDS_REVIEW); "
    "no copy was created or changed, an existing copy (if any) is kept"
)
EDITED_PREFIX = "edited-copy:"
_EDITED_MSG = (
    EDITED_PREFIX + " the translated Doc was changed in Google Docs after our last write; "
    "it was left untouched. Delete (trash) the copy to have it regenerated."
)

_SKIP_MIME_NOTE = {
    "application/vnd.google-apps.spreadsheet": "Google Sheets are not translated here",
    "application/vnd.google-apps.presentation": "Google Slides are not translated here",
    "application/vnd.google-apps.form": "Google Forms are not translated by this connector",
    "application/pdf": "PDF files are not translated by this connector (only Google Docs)",
}


SPEC = ConnectorSpec(
    kind="gdocs",
    name="Google Docs",
    icon="📄",
    cat="Files / documents",
    blurb="Translate the Google Docs in a Drive folder (My Drive or a shared drive). Each "
    "translation is written as a new Google Doc in a per-locale folder with the formatting, "
    "lists, tables and links kept, and the source documents are never changed.",
    config_fields=[
        FieldSpec(
            "source_folder",
            "Source folder",
            "The Drive folder that holds the Docs to translate: paste its folder ID or its URL. "
            "Share it with the service account (Viewer is enough). Never My Drive root.",
            placeholder="1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        ),
        FieldSpec(
            "target_folder",
            "Target folder",
            "The Drive folder that receives one sub-folder per locale: its ID or URL. Share it "
            "with the service account as Editor (Content manager on a shared drive). Must not "
            "be inside the source folder or contain it.",
            placeholder="1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "The language of the source documents, e.g. en-US.",
            placeholder="en-US",
        ),
        FieldSpec(
            "target_locales",
            "Target locales",
            "Comma-separated locale codes; each becomes a folder name under the target folder.",
            required=False,
            placeholder="fr-FR, de-DE",
        ),
        FieldSpec(
            "include_subfolders",
            "Include subfolders",
            "1 to translate Docs in sub-folders too (the tree is mirrored), 0 for the top level "
            "only.",
            required=False,
            placeholder="1",
        ),
        FieldSpec(
            "max_files",
            "Max documents",
            "Safety cap on documents scanned per run.",
            required=False,
            placeholder="50",
        ),
        FieldSpec(
            "impersonate_user",
            "Impersonate user",
            "Optional, Google Workspace only, ADMIN ONLY, off by default: a user email to act as "
            "through domain-wide delegation, so translated Docs are owned by that user instead "
            "of the service account. It lets the key act as that person everywhere in Drive, so "
            "leave it blank unless you need a plain My Drive folder.",
            required=False,
            placeholder="alex@example.com",
        ),
        FieldSpec(
            "protect_edited_copies",
            "Protect edited copies",
            "1 (default) leaves a translated Doc alone when someone edited it after our last "
            "write. Set 0 only if live QA shows false positives.",
            required=False,
            placeholder="1",
        ),
        FieldSpec(
            "update_in_place",
            "Update in place",
            "1 (default) rewrites an existing translated Doc in place when the source changed "
            "(same link, sharing and comments kept) whenever its structure is unchanged; 0 always "
            "builds a new Doc and trashes the old one. Google Docs has no draft state: every "
            "write is live, so people who have the translated Doc open see the new text at once.",
            required=False,
            placeholder="1",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "service_account_json",
            "Service account key (JSON)",
            "The JSON key file of a service account (Google Cloud, IAM, Service accounts, "
            "Keys). Paste the whole file, or its one-line form. It is checked, stored encrypted "
            "and never shown or logged again.",
            secret=True,
            placeholder='{"type": "service_account", ...}',
        ),
    ],
    setup_steps=[
        "In Google Cloud create a project, enable the Google Drive API and the Google Docs API, "
        "create a service account and download a JSON key for it.",
        "Put the Docs in a Drive folder (best: a folder in a shared drive) and share it with the "
        "service account's email address. Create an empty target folder in the same place and "
        "share it as Editor.",
        "Paste the JSON key here and the two folder IDs (the last part of each folder's URL). "
        "Translations appear in the target folder, one sub-folder per locale.",
        "A plain My Drive folder works only when you also set Impersonate user (domain-wide "
        "delegation): service accounts have no storage of their own, so a shared drive is the "
        "simple route. The runbook has the details.",
    ],
    other_side="Google Docs has no draft state: every write is LIVE, so readers of a "
    "translated Doc see the new text at once (it is rewritten in place, one atomic batch, or "
    "replaced by a new Doc with a new link when its structure changed). "
    "Change detection is poll-based: run on a schedule. A document's modified time "
    "moves only when it is edited, and each (document, locale) copy is re-checked against a "
    "stored content hash, so unchanged documents are neither re-read nor re-translated. A "
    "translated Doc that someone edited after our last write is never overwritten. No webhook "
    "receiver in scope.",
    docs_url="https://developers.google.com/workspace/docs/api/how-tos/overview",
    supports_oauth=False,
)


def scope_fingerprint(cfg) -> tuple:
    """The config values that decide WHAT the stored service account key reaches: the two
    folders and the user it acts as. Changing any of them clears the stored credential
    (store_part2 `_pin_secret_to_host`), the way the MCP connector's tool mapping does."""
    c = cfg or {}
    keys = ("source_folder", "target_folder", "impersonate_user")
    out = []
    for k in keys:
        v = str(c.get(k) or "").strip()
        try:
            if k != "impersonate_user":
                v = parse_folder_ref(v, k) if v else ""
        except ConnectorError:
            pass
        out.append(v.lower() if k == "impersonate_user" else v)
    return tuple(out)


class GoogleDocsStrategy(DocPerLocaleStrategy):
    """The stock T3 id-map lifecycle, plus: a withheld or broken payload never reaches the
    create path (no source-language copy for a document nothing was translated for), and an
    update may hand back a NEW copy id (the old translation moves to the trash)."""

    def write_target(self, client, record_id, target_locale, fields, *, draft=True):
        fields = fields or {}
        if fields.get("held"):
            return WriteResult(False, False, message=_HELD_BACK)
        if fields.get("error"):
            return WriteResult(False, False, message=str(fields["error"]))
        return super().write_target(client, record_id, target_locale, fields, draft=draft)

    def _fill_existing(self, client, cid, src, loc, target_id, fields, *, created):
        from app.services.connectors import idmap

        try:
            res = client.fill_target(str(target_id), fields)
        except Exception as e:
            return WriteResult(
                False, created, target_id=str(target_id), message=f"{type(e).__name__}: {e}"
            )
        if res.ok:
            new_id = str(res.target_id or target_id)
            idmap.set_status(cid, src, loc, idmap.DONE, target_id=new_id)
            after = getattr(client, "after_commit", None)
            if callable(after):
                after(new_id)  # the superseded Doc is trashed only now the map names the new one
            return WriteResult(
                True, created, target_id=new_id, message="created" if created else "updated"
            )
        return WriteResult(
            False, created, target_id=str(target_id), message=res.message or "fill_target failed"
        )


class GoogleDocsConnector(GoogleMixin, Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="gdocs",
        topology=Topology.T3,  # a separate Doc per locale, correlated by our id map
        auth=(AuthKind.API_KEY,),  # a pasted service account key
        api_version=API_VERSION,
        rich_text=RichText.NONE,  # the Docs structure is kept by copying, not re-encoded
        regions="fixed url",  # googleapis.com only
        change_signals=ChangeSignals(
            webhook_sig=None,  # no webhook receiver in scope
            poll_field="modifiedTime",
            stale_flag=None,
            thin_payload=False,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = GoogleDocsStrategy()
        self.sel = SelectionTracker(
            noun="document", label="gdocs", loud_markers=("not a Google Doc",)
        )
        self._tok = ""
        self._tok_exp = 0.0
        self._tags: dict = dict(self.config.get("gdocs_target_tags") or {})
        self._seen: dict = dict(self.config.get("gdocs_seen") or {})
        self._written: dict = dict(self.config.get("gdocs_written") or {})
        self._shapes: dict = {}
        self._supersede: dict = {}  # new copy id -> the superseded Doc to trash after commit
        self._state_cache: dict = {}
        self._folder_cache: dict = {}
        self._folders_ok = False
        self.edited: list = []  # (article_id, locale) copies left alone because a human edited them
        self.warnings: list = []

    # --------------------------------------------------------------- config
    def source_locale(self) -> str:
        loc = str(self.config.get("source_locale") or "").strip()
        if not loc:
            raise ConnectorError(
                "Set the connection's Source locale (e.g. en-US): the language of the documents."
            )
        return loc

    def source_id(self) -> str:
        return parse_folder_ref(self.config.get("source_folder"), "Source folder")

    def target_id(self) -> str:
        return parse_folder_ref(self.config.get("target_folder"), "Target folder")

    def check_config(self) -> None:
        """Validate every operator-supplied string, fail-closed, before any request."""
        self._sa()
        self._impersonate()
        src, tgt = self.source_id(), self.target_id()
        if src == tgt:
            raise ConnectorError(
                "Source folder and Target folder are the same folder: translated copies would "
                "be picked up as source documents. Choose two separate folders."
            )

    def _recursive(self) -> bool:
        raw = str(self.config.get("include_subfolders") or "1").strip().lower()
        return raw not in ("0", "false", "no", "off")

    def _inplace(self) -> bool:
        raw = str(self.config.get("update_in_place") or "1").strip().lower()
        return raw not in ("0", "false", "no", "off")

    def _protect(self) -> bool:
        raw = str(self.config.get("protect_edited_copies") or "1").strip().lower()
        return raw not in ("0", "false", "no", "off")

    def locale_folder(self, loc: str) -> str:
        from app.services.connectors import store

        return store._canonical_locale(str(loc).strip())

    def _verify_folders(self, c) -> None:
        """Once per run, with the network: both ids are real, non-trashed folders, the target is
        writable, and neither folder sits inside the other (so a copy can never become a
        source). Everything later (explicit ids, writes) is scoped against these two ids."""
        if self._folders_ok:
            return
        src, tgt = self.source_id(), self.target_id()
        sm = self.file_meta(c, src)
        tm = self.file_meta(c, tgt)
        who = self._sa().get("client_email", "the service account")
        if sm is None or sm.get("trashed"):
            raise ConnectorError(
                f"Source folder not found. Share it with {who} (Viewer or better) and check the ID."
            )
        if tm is None or tm.get("trashed"):
            raise ConnectorError(
                f"Target folder not found. Share it with {who} (Editor or better) and check the ID."
            )
        if sm.get("mimeType") != FOLDER_MIME or tm.get("mimeType") != FOLDER_MIME:
            raise ConnectorError("Source folder and Target folder must both be Drive folders.")
        if not (tm.get("capabilities") or {}).get("canAddChildren", True):
            raise ConnectorError(
                f"{who} cannot add files to the Target folder: share it as Editor "
                "(Content manager on a shared drive)."
            )
        if tgt in self.ancestors(c, src, sm) or src in self.ancestors(c, tgt, tm):
            raise ConnectorError(
                "Source folder and Target folder overlap (one is inside the other): translated "
                "copies would be picked up as source documents. Choose two separate folders."
            )
        self._folders_ok = True

    # ----------------------------------------------------------- test / list
    def test(self) -> tuple[bool, str]:
        try:
            self.check_config()
            with self._client() as c:
                self._verify_folders(c)
            return True, "Connected to Google Drive (source and target folders reached)."
        except ConnectorError as e:
            return False, self._scrub(e)
        except Exception as e:
            return False, self._scrub(f"{type(e).__name__}: {e}")

    def containers(self) -> list[dict]:
        try:
            return [{"id": self.source_id(), "name": "Drive source folder", "tables": []}]
        except Exception:
            return []

    # ------------------------------------------------------------- discovery
    def _list_source(self, c) -> list[dict]:
        """Metadata for every Doc under the source folder (bounded), breadth-first. Nothing is
        read from a document here."""
        self._verify_folders(c)
        out: list[dict] = []
        skipped: list = []
        queue = deque([(self.source_id(), (), 0)])
        seen_folders = {self.source_id()}
        while queue:
            fid, rel, depth = queue.popleft()
            for it in self.children(c, fid):
                name = str(it.get("name") or "")
                mime = it.get("mimeType")
                relpath = "/".join((*rel, name))
                if it.get("trashed"):
                    continue
                if mime == SHORTCUT_MIME:
                    skipped.append((relpath, "a Drive shortcut is never followed"))
                    continue
                if mime == FOLDER_MIME:
                    iid = str(it.get("id") or "")
                    if (
                        self._recursive()
                        and depth < MAX_DEPTH
                        and iid
                        and iid not in seen_folders
                        and iid != self.target_id()
                    ):
                        seen_folders.add(iid)
                        queue.append((iid, (*rel, name), depth + 1))
                    continue
                if mime != DOC_MIME:
                    note = _SKIP_MIME_NOTE.get(str(mime), "not a Google Doc")
                    skipped.append((relpath, f"not a Google Doc: {note}"))
                    continue
                out.append({"item": it, "rel": rel})
        self._list_skipped = skipped
        return out

    def _classify(self, entry: dict):
        it, rel = entry["item"], tuple(entry["rel"])
        name = str(it.get("name") or "")
        relpath = "/".join((*rel, name))
        if not valid_name(name) or not all(valid_name(s) for s in rel):
            return None, (
                relpath,
                "the document or a folder name has a control character or is too long",
            )
        try:
            iid = validate_file_id(it.get("id"))
        except ConnectorError:
            return None, (relpath, "unusable Drive id")
        return {
            "id": iid,
            "name": name,
            "rel": rel,
            "relpath": relpath,
            "modified": str(it.get("modifiedTime") or ""),
        }, None

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Docs to translate. Explicit `article_ids` (Drive file ids) win and are SCOPE-CHECKED
        against the source folder (taxonomy A): an id outside it, inside the target folder, a
        folder, a non-Doc, a trashed file or anything malformed is refused fail-closed.
        Otherwise the source tree is listed and the shared `cap` is filled ROUND-ROBIN across
        sub-folders, so one folder can never starve the others (taxonomy E)."""
        self.check_config()
        cap = fetch_cap(cap or self.config.get("max_files"), 50)
        with self._client() as c:
            if article_ids:
                self._verify_folders(c)
                return self._explicit(c, list(article_ids)[:cap])
            articles: list[dict] = []
            skipped: dict = {}
            for entry in self._list_source(c):
                art, why = self._classify(entry)
                if art is None:
                    skipped[why[0]] = why[1]
                else:
                    articles.append(art)
            for rp, why in getattr(self, "_list_skipped", []):
                skipped[rp] = why
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
                    skipped[a["relpath"]] = "beyond the Max documents cap for this run"
            self.sel.record_selection([a["relpath"] for a in chosen], skipped)
            return chosen

    def _explicit(self, c, ids: list) -> list[dict]:
        src, tgt = self.source_id(), self.target_id()
        out: list[dict] = []
        for raw in ids:
            fid = validate_file_id(raw, "document id")
            it = self.file_meta(c, fid)
            if it is None or it.get("trashed"):
                self.fetch_notes.append(
                    {"id": fid, "meta": {}, "error": f"{fid}: document not found."}
                )
                continue
            if it.get("mimeType") != DOC_MIME:
                raise ConnectorError(f"{fid}: not a Google Doc; refused.")
            # walk up: the chain must reach the source folder and never the target folder
            rel_names: list[str] = []
            cur, reached = it, False
            for _ in range(25):
                parents = cur.get("parents") or []
                if not parents:
                    break
                pid = str(parents[0])
                if pid == tgt:
                    raise ConnectorError(
                        f"{fid}: the document is inside the target folder; refused."
                    )
                if pid == src:
                    reached = True
                    break
                nxt = self.file_meta(c, pid, "id,name,parents")
                if nxt is None:
                    break
                rel_names.append(str(nxt.get("name") or ""))
                cur = nxt
            if not reached:
                raise ConnectorError(
                    f"{fid}: the document is outside the configured source folder; refused."
                )
            art, why = self._classify({"item": it, "rel": tuple(reversed(rel_names))})
            if art is None:
                self.fetch_notes.append({"id": fid, "meta": {}, "error": f"{fid}: {why[1]}."})
                continue
            out.append(art)
        return out

    # ------------------------------------------------------- load and write
    def load(self, art: dict):
        """(units, problems) for one Doc: read it with all tabs, then walk its structure."""
        with self._client() as c:
            doc = self.get_document(c, art["id"])
        self._shapes[art["id"]] = doc_shape(doc)
        return parse_units(doc)

    def seen(self, aid: str):
        raw = self._seen.get(aid)
        if not raw or "|" not in str(raw):
            return None
        m, _, d = str(raw).partition("|")
        return m, d

    def record_seen(self, aid: str, modified: str, digest: str) -> None:
        val = f"{modified}|{digest}"
        self._seen[aid] = val
        if self.connection_id is not None:
            from app.services.connectors import store

            store.patch_config_merge(self.connection_id, "gdocs_seen", {aid: val})

    @staticmethod
    def digest(units) -> str:
        parts = []
        for u in units:
            parts.append(
                f"{u.tab}/{u.seg}"
            )  # stable source ids: moving text between segments counts
            parts.append(u.text)
            parts.append(";".join(_style_key(s) for s, _n in u.spans))
        return _content_hash(*parts)

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
                    it = self.file_meta(c, row.target_id)
                if it is None or it.get("trashed"):
                    st = {"state": "absent"}
                else:
                    st = {"state": "present", "ident": self._ident(it)}
            except Exception:  # cannot tell: never treat a failed probe as "deleted"
                st = {"state": "unknown"}
        self._state_cache[key] = st
        return st

    @staticmethod
    def _ident(meta: dict) -> str:
        return str(meta.get("modifiedTime") or "")

    def is_current(self, aid: str, loc: str) -> bool:
        """The map says done AND the copy is still there (or we could not tell)."""
        return self._pair_state(aid, loc)["state"] in ("present", "unknown")

    def parked(self, aid: str, loc: str, chash: str) -> bool:
        """True when this pair's source changed but its copy was edited by a human since our
        last write: it is left alone before any MT spend. Never true for an unchanged source, a
        pair we never wrote, or a copy that is gone."""
        if not self._protect():
            return False
        key = f"{aid}:{loc}"
        if (self.config.get("gdocs_hashes") or {}).get(key) == chash:
            return False
        recorded = self._tags.get(key)
        if not recorded:
            return False
        st = self._pair_state(aid, loc)
        if st["state"] != "present":
            return False
        if st.get("ident") and st["ident"] != recorded:
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

            store.patch_config_merge(self.connection_id, "gdocs_target_tags", {pair: ident})

    # ----------------------------------------------- folders for the translations
    def target_parts(self, loc: str, art: dict) -> list[str]:
        folder = self.locale_folder(loc)
        if not valid_locale_folder(folder):
            raise ConnectorError(f"Locale {_safe(loc)} cannot be used as a folder name.")
        return [folder, *art["rel"]]

    def _resolve_folder(self, c, parts: list[str], *, create: bool):
        """The folder id for a path under the target folder; None when it does not exist (and
        `create` is off). Folders found are real folders directly under the previous one
        (a shortcut or a Doc of the same name is never one), so nothing resolves outside the
        target folder."""
        parent = self.target_id()
        for name in parts:
            if not valid_name(name):
                raise ConnectorError(f"Unsafe folder name {_safe(name)}.")
            key = (parent, name)
            fid = self._folder_cache.get(key)
            if fid is None:
                f = self.child_folder(c, parent, name)
                if f is None:
                    if not create:
                        return None
                    f = self.create_folder(c, parent, name)
                fid = validate_file_id(f.get("id"))
                self._folder_cache[key] = fid
            parent = fid
        return parent

    # ------------------------------------- the client the T3 strategy drives
    def find_preexisting(self, source_id: str, loc: str, fields: dict):
        """A Doc already at the copy's place that our map never made (a human-made
        translation): the strategy then fails closed (needs_link)."""
        with self._client() as c:
            self._verify_folders(c)
            folder = self._resolve_folder(c, fields["target_parts"], create=False)
            if folder is None:
                return None
            it = self.child_named(c, folder, fields["name"])
        return str(it.get("id") or "") or None if it else None

    def create_target(self, source_id: str, loc: str, fields: dict):
        with self._client() as c:
            self._verify_folders(c)
            folder = self._resolve_folder(c, fields["target_parts"], create=True)
            r, item = self.copy_file(c, source_id, PENDING_PREFIX + fields["name"], folder)
            if item and item.get("id"):
                return str(item["id"]), None
            return None, self._google_error(r)

    def _under_target(self, c, meta: dict) -> bool:
        return self.target_id() in self.ancestors(c, str(meta.get("id") or ""), meta)

    def _build_into(self, c, doc_id: str, fields: dict) -> tuple[str, str, list | None]:
        """Apply the translation to a PRISTINE copy. Returns (state, error, layout): state is
        'applied' (done now), 'already' (an earlier attempt landed) or 'mismatch' (not a pristine
        copy) or 'error'; layout maps the source's segments to the copy's (None when unknown)."""
        doc = self.get_document(c, doc_id)
        units, problems = parse_units(doc)
        if [u.text for u in units] == fields["src_sig"] and not problems:
            layout = layout_map(fields.get("units") or [], units)
            reqs = build_requests(units, fields["translations"])
            if not reqs:
                return "applied", "", layout
            if len(reqs) > MAX_UNITS_PER_DOC * 3 + 3:
                return "error", "the document needs too many edits for one batch", None
            r = self.batch_update(c, doc_id, reqs, str(doc.get("revisionId") or ""))
            if r.status_code == 200:
                return "applied", "", layout
            return "error", self._batch_error(r), None
        if [u.plain for u in units] == fields["new_plain"]:
            return "already", "", None
        return "mismatch", "", None

    def _batch_error(self, r) -> str:
        if r.status_code == 400 and "revision" in self._reasons(r)[2].lower():
            return "the copy changed while it was being written; it will be retried"
        return f"{self._google_error(r)} writing the translation"

    # ------------------------------------------- what we wrote (in-place update state)
    def _written_rec(self, pair: str) -> dict | None:
        raw = self._written.get(pair)
        try:
            rec = json.loads(raw) if isinstance(raw, str) and raw else None
        except ValueError:
            return None
        if isinstance(rec, dict) and rec.get("m") and rec.get("sk") and rec.get("td"):
            return rec
        return None

    def _record_written(self, pair: str, rec: dict | None) -> None:
        val = json.dumps(rec, separators=(",", ":")) if rec else ""
        self._written[pair] = val
        if self.connection_id is not None:
            from app.services.connectors import store

            store.patch_config_merge(self.connection_id, "gdocs_written", {pair: val})

    def _forget(self, pair: str) -> None:
        """We could not read back what we just wrote: drop what we remember, so the next run
        rebuilds the Doc instead of guessing (never a false 'edited by hand')."""
        self._record_written(pair, None)
        self._tags.pop(pair, None)
        if self.connection_id is not None:
            from app.services.connectors import store

            store.patch_config_merge(self.connection_id, "gdocs_target_tags", {pair: ""})

    def _seal(self, c, pair: str, work: str, fields: dict, layout: list | None) -> str:
        """After a successful write: remember the Drive modified time (hand-edit detection) and
        what the Doc now says plus how it maps to the source (in-place updates). Returns an
        error text when the read-back failed ('' when fine)."""
        try:
            meta = self.file_meta(c, work)
            doc = self.get_document(c, work)
            units, _ = parse_units(doc)
        except Exception:
            self._forget(pair)
            return "the translation was written but could not be read back"
        if meta is None:
            self._forget(pair)
            return "the translation was written but the Doc could not be read back"
        self._record_tag(pair, self._ident(meta))
        if layout and fields.get("src_shape"):
            self._record_written(
                pair, {"m": layout, "sk": fields["src_shape"], "td": text_digest(units)}
            )
        else:
            self._record_written(pair, None)
        return ""

    def _update_in_place(self, c, meta: dict, target_id: str, fields: dict, name: str):
        """Rewrite the existing translated Doc IN PLACE: the same file id, so the link readers
        have, its sharing and its comments all survive. Safe only when (a) we remember how we
        last wrote it, (b) it still says exactly what we wrote (no hand edit), (c) the source
        has the same structure as when we wrote it (only text changed), (d) it sits in the folder
        it should. Returns a WriteResult, or None to fall back to building a replacement."""
        pair = fields.get("pair", "")
        rec = self._written_rec(pair)
        src_units = fields.get("units") or []
        if not (self._inplace() and rec and src_units and fields.get("src_shape") == rec["sk"]):
            return None
        folder = self._resolve_folder(c, fields["target_parts"], create=False)
        if folder is None or (meta.get("parents") or [""])[0] != folder:
            return None
        doc = self.get_document(c, target_id)
        units_t, problems = parse_units(doc)
        if problems:
            return WriteResult(
                False,
                False,
                target_id=target_id,
                message="the translated Doc has pending suggestions; it was left untouched",
            )
        if text_digest(units_t) != rec["td"]:
            if not self._protect():
                return None  # protection is off: replace the hand-edited Doc with a fresh one
            if pair and tuple(pair.split(":", 1)) not in self.edited:
                self.edited.append(tuple(pair.split(":", 1)))
            return WriteResult(False, False, target_id=target_id, message=_EDITED_MSG)
        mapping = {(a, b): (cc, d) for a, b, cc, d in rec["m"]}
        by_t: dict = {}
        for u in units_t:
            by_t.setdefault((u.tab, u.seg), []).append(u)
        by_s: dict = {}
        for u in src_units:
            by_s.setdefault((u.tab, u.seg), []).append(u)
        if set(by_t) != set(mapping.values()) or set(by_s) != set(mapping):
            return None
        pairs: list = []
        for key, su in by_s.items():
            tu = by_t[mapping[key]]
            if len(tu) != len(su):
                return None
            # Same unit count is not enough: text that moved to another (blank) paragraph or
            # table cell of an unchanged structure would pair with the wrong place. Every pair
            # must sit in the same paragraph / position, else rebuild from a fresh copy.
            if [u.slot for u in su] != [u.slot for u in tu]:
                return None
            pairs.extend(zip(su, tu, strict=True))
        reqs = build_requests_inplace(pairs, fields["translations"])
        if reqs is None or len(reqs) > MAX_UNITS_PER_DOC * 3 + 3:
            return None
        if reqs:
            r = self.batch_update(c, target_id, reqs, str(doc.get("revisionId") or ""))
            if r.status_code != 200:
                return WriteResult(False, False, target_id=target_id, message=self._batch_error(r))
        if str(meta.get("name") or "") != name:
            r = self.update_file(c, target_id, {"name": name})
            if r.status_code != 200:
                self._forget(pair)
                return WriteResult(
                    False,
                    False,
                    target_id=target_id,
                    message=f"{self._google_error(r)} renaming the copy",
                )
        elif not reqs:
            return WriteResult(True, False, target_id=target_id, message="written")
        err = self._seal(c, pair, target_id, fields, rec["m"])
        if err:
            return WriteResult(False, False, target_id=target_id, message=err)
        return WriteResult(True, False, target_id=target_id, message="written")

    def fill_target(self, target_id: str, fields: dict) -> WriteResult:
        pair, src_id, name = fields.get("pair", ""), fields["source_id"], fields["name"]
        with self._client() as c:
            meta = self.file_meta(c, target_id)
            if meta is None or meta.get("trashed"):
                return WriteResult(
                    False, False, message="the copy disappeared before it could be written"
                )
            if meta.get("mimeType") != DOC_MIME:
                return WriteResult(False, False, message="the copy is not a Google Doc")
            if not self._under_target(c, meta):
                return WriteResult(
                    False,
                    False,
                    message="the copy is not inside the target folder; refused (source "
                    "documents are never written)",
                )
            pending = str(meta.get("name") or "").startswith(PENDING_PREFIX)
            if (
                not pending
                and self._protect()
                and self._tags.get(pair)
                and self._ident(meta) != self._tags[pair]
            ):
                return WriteResult(False, False, target_id=target_id, message=_EDITED_MSG)
            folder = (meta.get("parents") or [""])[0]
            work, old, layout = target_id, None, None
            if pending:
                state, err, layout = self._build_into(c, target_id, fields)
                if state == "error":
                    return WriteResult(False, False, target_id=target_id, message=err)
                if state == "mismatch":
                    old, work = target_id, ""  # not pristine: replace it with a fresh copy
            else:
                done = self._update_in_place(c, meta, target_id, fields, name)
                if done is not None:
                    return done
                old, work = target_id, ""
                self.warnings.append(
                    "a translated Doc could not be updated in place (its structure changed or "
                    "its record is missing) and was rebuilt as a new Doc: its link changed"
                )
            if not work:
                r, item = self.copy_file(c, src_id, PENDING_PREFIX + name, folder)
                if not (item and item.get("id")):
                    return WriteResult(
                        False, False, target_id=target_id, message=self._google_error(r)
                    )
                work = str(item["id"])
                state, err, layout = self._build_into(c, work, fields)
                if state != "applied" and state != "already":
                    self._trash(c, work)
                    return WriteResult(
                        False,
                        False,
                        target_id=target_id,
                        message=err or "the fresh copy did not match the source layout",
                    )
            r = self.update_file(c, work, {"name": name})
            if r.status_code != 200:
                return WriteResult(
                    False,
                    False,
                    target_id=work,
                    message=f"{self._google_error(r)} renaming the copy",
                )
            err = self._seal(c, pair, work, fields, layout)
            if err:
                self.warnings.append(err)
            if old and old != work:
                if pending or self._copy_sharing(c, old, work):
                    # Trashed by `after_commit`, once the id map points at the replacement.
                    self._supersede[work] = old
                else:
                    self.warnings.append(
                        "the sharing of a rebuilt translated Doc could not be copied to its "
                        "replacement, so the old Doc was left in place (not trashed) for its "
                        "readers: share the new Doc, then trash the old one"
                    )
            return WriteResult(True, False, target_id=work, message="written")

    def _copy_sharing(self, c, old: str, new: str) -> bool:
        """Give the replacement Doc the sharing a person added to the Doc it replaces (Drive
        files.copy keeps only what the folder passes down). No notification emails. False when
        any entry could not be read or copied (the caller then keeps the old Doc)."""
        try:
            wanted = self.explicit_permissions(self.list_permissions(c, old))
            for perm in wanted:
                r = self.add_permission(c, new, perm)
                if r.status_code not in (200, 201):
                    return False
        except (ConnectorError, httpx.TransportError):
            return False
        return True

    def after_commit(self, target_id: str) -> None:
        """Called by the strategy once the id map names `target_id`: only now is the superseded
        Doc (if any) moved to the Drive trash, so a crash in between can never leave the map
        pointing at a trashed file with the live translation unrecorded."""
        old = self._supersede.pop(str(target_id), None)
        if not old:
            return
        with self._client() as c:
            warn = self._trash(c, old)
        if warn:
            self.warnings.append(warn)

    def _trash(self, c, file_id: str) -> str:
        """Move one of OUR copies to the Drive trash (recoverable; never a permanent delete).
        Returns a warning text when it could not be done."""
        try:
            r = self.update_file(c, file_id, {"trashed": True})
        except Exception as e:
            return f"could not trash the superseded copy: {type(e).__name__}"
        if r.status_code != 200:
            return "could not trash the superseded copy (give the service account Content manager)"
        return ""

    def verify_target(self, target_id: str) -> bool:
        try:
            with self._client() as c:
                it = self.file_meta(c, target_id)
            return it is not None and not it.get("trashed")
        except ConnectorError as e:
            raise TargetUnverifiable(str(e)) from e
        except httpx.TransportError as e:
            raise TargetUnverifiable(str(e)) from e

    # ------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for art in self.discover_articles():
            units, problems = self.load(art)
            if problems:
                continue
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
        raise ConnectorError("Google Docs writes translated copies - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []


# ===========================================================================
# Translate flow: thin hooks over the shared engine (flow.run_translate_flow).
# Poll-only, no watermark: the modifiedTime + content-digest gate is the change detector.
# ===========================================================================
def _gdocs_preflight(ctx):
    conn = ctx.connector
    src = conn.source_locale().lower()
    conn.check_config()
    enabled = {loc for loc in ctx.locales if valid_locale_folder(conn.locale_folder(loc))}
    return enabled, {"source": src}


def _gdocs_partition(locales, enabled, extra):
    src = (extra or {}).get("source", "")
    rejected = [loc for loc in locales if loc not in enabled]
    usable = [loc for loc in locales if loc in enabled and loc != src]
    return rejected, usable


def _gdocs_prepare(ctx, art):
    conn = ctx.connector
    aid = art["id"]
    skeleton = {"article_id": aid, "title": art["name"]}

    def idle(loc, chash):
        if conn.parked(aid, loc, chash):
            return True
        return ctx.hashes.get(f"{aid}:{loc}") == chash and conn.is_current(aid, loc)

    def lazy(chash):
        return flow.ArticlePrep(
            aid=aid,
            chash=chash,
            title_text="",
            body_units=[],
            extra_skeleton=skeleton,
            doc_title=art["name"][:60],
            extra={"lazy": True, "connector": conn, "art": art},
        )

    # An unchanged Doc whose copies are all current (or parked) is not even read.
    seen = conn.seen(aid)
    if seen and seen[0] == art["modified"] and all(idle(loc, seen[1]) for loc in ctx.usable):
        return lazy(seen[1])
    try:
        units, problems = conn.load(art)
        if not problems:
            check_unit_texts([u.text for u in units])
    except InputLimitError:
        raise
    except Exception as e:  # a bad read must not stop the other documents
        msg = conn._scrub(str(e))[:140] if str(e) else type(e).__name__
        return flow.SkipArticle(
            "unreadable_skipped",
            error=f"{art['relpath']}: could not read the document ({type(e).__name__}: {msg})",
        )
    if problems:
        return flow.SkipArticle(
            "unreadable_skipped",
            error=f"{art['relpath']}: skipped, the document has {', '.join(problems)}; "
            "accept or reject them first",
        )
    if not units:
        return flow.SkipArticle("empty_skipped")
    if len(units) > MAX_UNITS_PER_DOC:
        return flow.SkipArticle(
            "unreadable_skipped",
            error=f"{art['relpath']}: skipped, {len(units)} text units is over the "
            f"{MAX_UNITS_PER_DOC} limit for one document; split the document",
        )
    chash = conn.digest(units)
    conn.record_seen(aid, art["modified"], chash)
    if all(idle(loc, chash) for loc in ctx.usable):
        return lazy(chash)
    return flow.ArticlePrep(
        aid=aid,
        chash=chash,
        title_text="",
        body_units=units,
        extra_skeleton=skeleton,
        doc_title=art["name"][:60],
        extra={"units": units, "art": art, "connector": conn},
    )


def _gdocs_is_suppressed(connector, aid, loc):
    return connector.is_current(aid, loc)


def _gdocs_parked(connector, aid, loc, chash):
    return connector.parked(aid, loc, chash)


def _gdocs_build_payload(prep, loc, seg0_target, mapping):
    ex = prep.extra
    if ex.get("lazy"):
        # Nothing was read for this Doc: it is unchanged with a current copy, or its copy was
        # edited by hand. Only an explicit retry reaches here (a normal run skips it).
        if (prep.aid, loc) in ex["connector"].edited:
            return {"error": _EDITED_MSG}
        return {"error": "the document is unchanged and its copy is current; nothing to rewrite"}
    units = ex["units"]
    translations = {
        str(u.p_index): mapping[str(u.p_index)] for u in units if str(u.p_index) in mapping
    }
    if not translations:
        return {"held": True}
    conn, art = ex["connector"], ex["art"]
    try:
        parts = conn.target_parts(loc, art)
        new_plain = [
            strip_tokens(u, translations[str(u.p_index)])
            if str(u.p_index) in translations
            else u.plain
            for u in units
        ]
    except Exception as e:  # the pair fails alone; nothing is created or changed
        return {"error": f"{art['relpath']}: {type(e).__name__}: {conn._scrub(str(e))[:160]}"}
    return {
        "pair": f"{prep.aid}:{loc}",
        "source_id": prep.aid,
        "name": art["name"],
        "target_parts": parts,
        "translations": translations,
        "src_sig": [u.text for u in units],
        "units": units,
        "src_shape": conn._shapes.get(prep.aid, ""),
        "new_plain": new_plain,
    }


def _is_held_back(f):
    return str((f or {}).get("error", "")).startswith(HELD_PREFIX)


def _held_back(ctx):
    return [f for f in ctx.failures if _is_held_back(f)]


def _real_failures(ctx):
    return [f for f in ctx.failures if not _is_held_back(f)]


def _dropped_as_source(ctx):
    return [loc for loc in ctx.locales if loc not in ctx.usable and loc not in ctx.rejected]


def _gdocs_compute_ok(ctx):
    if not ctx.usable:
        return False
    real = _real_failures(ctx)
    unreadable = ctx.counters["unreadable_skipped"]
    if ctx.created + ctx.updated > 0:
        return True
    return not real and not unreadable


def _gdocs_build_message(ctx):
    conn = ctx.connector
    parts = [
        f"Created {ctx.created}, updated {ctx.updated} translated Doc(s) across "
        f"{len(ctx.usable)} locale(s) from {len(ctx.articles)} document(s)."
    ]
    if ctx.skipped:
        parts.append(f"Skipped {ctx.skipped} unchanged (content-hash) Doc(s).")
    if ctx.counters["empty_skipped"]:
        parts.append(
            f"Skipped {ctx.counters['empty_skipped']} document(s) with no translatable text."
        )
    if ctx.counters["unreadable_skipped"]:
        parts.append(
            f"{ctx.counters['unreadable_skipped']} document(s) could not be read or were "
            "skipped (see errors)."
        )
    if ctx.counters["parked_skipped"]:
        parts.append(
            f"Left {ctx.counters['parked_skipped']} Doc(s) untouched because they were edited in "
            "Google Docs after our last write (trash a copy to regenerate it)."
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
            f"Held back {len(held)} (document, locale) pair(s) awaiting review (NEEDS_REVIEW): "
            "no Doc was created or changed; they will be re-attempted next run."
        )
    sel = conn.sel.selection_summary()
    if sel:
        parts.append(sel)
    if conn.warnings:
        parts.append("Warnings: " + "; ".join(sorted(set(conn.warnings))[:3]))
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
        parts.append(ctx.budget.message("document write(s)").strip())
        log.warning(
            "gdocs_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            ctx.cid,
            ctx.budget.ceiling,
        )
    return " ".join(parts)


def _gdocs_build_result(ctx):
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
        "warnings": sorted(set(conn.warnings)),
        "failures": _real_failures(ctx),
    }


def gdocs_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Translate the Google Docs in the connection's source Drive folder into one or more
    locales, each written as a new Doc in the locale's folder under the target folder via the T3
    id-map strategy. Thin wrapper over flow.run_translate_flow; poll-only with content-hash
    suppression. only_pairs restricts the write loop to exactly those (file id, locale) pairs."""
    spec = flow.TranslateFlowSpec(
        kind="gdocs",
        no_conn_msg="No such Google Docs connection.",
        hash_key="gdocs_hashes",
        probe_label="Google Docs",
        preflight=_gdocs_preflight,
        prepare=_gdocs_prepare,
        is_suppressed=_gdocs_is_suppressed,
        build_payload=_gdocs_build_payload,
        compute_draft=lambda cfg: True,
        compute_ok=_gdocs_compute_ok,
        build_message=_gdocs_build_message,
        build_result=_gdocs_build_result,
        held_prefix=HELD_PREFIX,
        report_held_updates=False,
        # A rebuilt Doc replaces the whole copy, so a document with ANY held unit is not
        # written at all (not created, not updated): the existing copy keeps every stored
        # translation.
        variant_exists=lambda connector, aid, loc: False,
        parked=_gdocs_parked,
        extra_counters=("empty_skipped", "unreadable_skipped", "parked_skipped"),
        partition=_gdocs_partition,
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
