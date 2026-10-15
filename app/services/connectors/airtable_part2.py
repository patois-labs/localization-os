"""airtable: part2

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

from app.services.connectors.airtable_part1 import API
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorSpec,
    FieldSpec,
    PulledItem,
    _validate_target,
    harvest_context_field,
    to_int,
)
from app.services.connectors.vendor_text import vendor_excerpt

SPEC = ConnectorSpec(
    kind="airtable",
    name="Airtable",
    icon="🗂",
    cat="Intake / content base",
    blurb="Pull text from Airtable records, translate it, and write the results back "
    "into a target field — turning a content base into a localization workflow.",
    config_fields=[
        FieldSpec(
            "base_id",
            "Default base ID",
            "The base this connection may read and write. The inbound webhook, "
            "webhook registration and field picker only accept this base; leave it "
            "blank and they refuse every base. Once a token is saved, only an admin "
            "can change it (or a linguist who enters their own token in the same save).",
            required=False,
            placeholder="appXXXXXXXXXXXXXX",
        ),
        FieldSpec(
            "table",
            "Default table",
            "The table this connection may read and write (same rules as the base).",
            required=False,
            placeholder="Content",
        ),
        FieldSpec(
            "source_field",
            "Source field",
            "Field holding the text to translate.",
            placeholder="Notes",
        ),
        FieldSpec(
            "target_field",
            "Target field",
            "Optional — for manual mode. Dynamic webhook "
            "auto-detects target-language columns instead.",
            required=False,
            placeholder="Notes (fr-FR)",
        ),
        FieldSpec(
            "naming_pattern",
            "Target column pattern",
            "How target-language columns are named. "
            "{src} = source field, {loc} = locale. Default matches 'Notes (es-MX)'.",
            required=False,
            placeholder="{src} ({loc})",
        ),
        FieldSpec(
            "source_locale",
            "Source locale",
            "Language of the source field.",
            required=False,
            placeholder="en-US",
        ),
        FieldSpec(
            "target_locale",
            "Target locale",
            "Default target for manual mode.",
            required=False,
            placeholder="fr-FR",
        ),
        FieldSpec(
            "view",
            "View (optional)",
            "Limit manual pull to a view's rows.",
            required=False,
            placeholder="Grid view",
        ),
        FieldSpec(
            "max_records", "Max records", "Safety cap per run.", required=False, placeholder="500"
        ),
        FieldSpec(
            "auto_translate_on_change",
            "Auto-translate on change",
            "'1' = a webhook ping immediately translates changed records; blank/'0' "
            "(default) only marks them pending. Official Webhooks API path.",
            required=False,
            placeholder="0",
        ),
        FieldSpec(
            "allow_select_typecast",
            "Allow writing to select fields",
            "'1' = write translated text into single/multiple-select target "
            "columns via typecast, CREATING a new option when the value doesn't "
            "already exist (reported loudly). Blank/'0' (default) skips select "
            "targets and reports them instead — a bad translation can never "
            "silently mint a permanent option in your base.",
            required=False,
            placeholder="0",
        ),
        harvest_context_field(),
    ],
    secret_fields=[
        FieldSpec(
            "api_key",
            "Personal Access Token",
            "Created at airtable.com/create/tokens.",
            secret=True,
            placeholder="patXXXX.XXXXXXXX",
        ),
    ],
    setup_steps=[
        "In Airtable, open the builder hub → Personal access tokens → Create token.",
        "Give it scopes: data.records:read, data.records:write, schema.bases:read, and "
        "webhook:manage (required for live push — without it webhook registration fails 403).",
        "Grant it access to the base(s) you want to localize, then copy the token here.",
        "Find the Base ID in the base's API docs (airtable.com/api) — it starts with 'app'.",
    ],
    other_side="Optional: install the Localization OS Airtable Extension (integrations/airtable-extension) "  # noqa: E501
    "to add a “Send to Localization OS” button right inside the base.",
    docs_url="https://airtable.com/developers/web/api/introduction",
    supports_oauth=True,
)


class AirtableConnector(Connector):
    spec = SPEC

    def _bearer(self) -> str:
        """The bearer token for the Airtable API, from either auth mode.

        Token mode (legacy, v1 secret): the flat 'api_key' PAT.
        OAuth mode (v2 secret, auth_mode='oauth'): the oauth sub-dict's
        access_token (kept fresh upstream by store.build_fresh /
        ensure_fresh_token — the connector never refreshes; it just reads). Both
        modes send an identical `Authorization: Bearer <tok>` to the SAME
        api.airtable.com host, so only the token SOURCE differs — the request
        path/transport is byte-identical (existing token-mode tests unaffected)."""
        if self.secret.get("auth_mode") == "oauth":
            return (self.secret.get("oauth") or {}).get("access_token") or ""
        return (self.secret.get("api_key") or "").strip()

    def _headers(self) -> dict:
        key = self._bearer()
        if not key:
            raise ConnectorError("Missing Airtable credentials (API token or OAuth).")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _api(self) -> str:
        """The Airtable API root, SSRF-guarded. Every request URL is built off this
        (records + meta paths), so validating here covers all request paths. The
        host is a fixed public constant — the user can't point it at an internal
        address — but the guard is centralized here per the shared-validator design
        (see base._validate_target)."""
        _validate_target(API)
        return API

    def _records_url(self) -> str:
        base = (self.config.get("base_id") or "").strip()
        table = (self.config.get("table") or "").strip()
        if not base or not table:
            raise ConnectorError("Base ID and table are required.")
        # table names with spaces/encoding are handled by httpx params/path quoting
        return f"{self._api()}/{base}/{table}"

    # --- operations ---
    def test(self) -> tuple[bool, str]:
        try:
            with self._client(headers=self._headers()) as c:
                r = self._request(c, "GET", self._records_url(), params={"maxRecords": 1})
            if r.status_code == 200:
                n = len(r.json().get("records", []))
                return True, f"Connected. Read the table ({n} sample record)."
            return False, f"Airtable returned {r.status_code}: {vendor_excerpt(r.text)}"
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        """List bases (and their tables) the token can see. Needs schema.bases:read."""
        out: list[dict] = []
        try:
            api = self._api()
            with self._client(headers=self._headers()) as c:
                r = self._request(c, "GET", f"{api}/meta/bases")
                if r.status_code != 200:
                    return []
                for b in r.json().get("bases", []):
                    entry = {"id": b.get("id"), "name": b.get("name"), "tables": []}
                    tr = self._request(c, "GET", f"{api}/meta/bases/{b.get('id')}/tables")
                    if tr.status_code == 200:
                        entry["tables"] = [
                            {"id": t.get("id"), "name": t.get("name")}
                            for t in tr.json().get("tables", [])
                        ]
                    out.append(entry)
        except Exception:
            return out
        return out

    def pull(self) -> list[PulledItem]:
        src = (self.config.get("source_field") or "").strip()
        tgt = (self.config.get("target_field") or "").strip()
        if not src or not tgt:
            raise ConnectorError("Source field and target field are required.")
        cap = to_int(self.config.get("max_records"), 100)
        # #576: never fetch more than one record past the document cap; the pull's cap
        # check then refuses the run with a clear message instead of reading a whole base.
        from app import config as _app_config

        cap = min(cap, _app_config.MAX_DOCUMENT_SEGMENTS + 1)
        view = (self.config.get("view") or "").strip()

        items: list[PulledItem] = []
        offset = None
        page_size = min(100, max(1, cap))
        with self._client(headers=self._headers()) as c:
            while True:
                params = {"pageSize": page_size, "fields[]": self._record_fields()}
                if view:
                    params["view"] = view
                if offset:
                    params["offset"] = offset
                r = self._get_page(
                    lambda **kw: self._request(c, "GET", self._records_url(), **kw),
                    params=params,
                    size_key="pageSize",
                )
                page_size = params["pageSize"]
                if r.status_code != 200:
                    raise ConnectorError(
                        f"Airtable pull failed ({r.status_code}): {vendor_excerpt(r.text)}"
                    )
                body = r.json()
                for rec in self._refuse_oversize(body.get("records", [])):
                    val = (rec.get("fields", {}) or {}).get(src)
                    if isinstance(val, str) and val.strip():
                        items.append(
                            PulledItem(external_id=rec["id"], field=tgt, text=val, title=val[:60])
                        )
                        if len(items) >= cap:
                            return items
                offset = body.get("offset")
                if not offset:
                    break
        return items

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        if not items:
            return 0, "Nothing to push."
        # merge multiple field writes per record (a row can get several locales at once)
        updates: dict[str, dict] = {}
        for it in items:
            updates.setdefault(it.external_id, {})[it.field] = it.text
        n, _failed, msg = self.patch_records(
            self.config.get("base_id"), self.config.get("table"), updates
        )
        return n, (f"Wrote {n} translation(s) back to Airtable." if n else msg)

    # --- dynamic / convention-driven helpers (used by the webhook path) ---
    def _url(self, base: str, table: str) -> str:
        if not base or not table:
            raise ConnectorError("Base and table are required.")
        return f"{self._api()}/{base}/{table}"

    def schema_fields(self, base: str, table: str) -> list[str]:
        """Field names of a table via the metadata API (needs schema.bases:read)."""
        with self._client(headers=self._headers()) as c:
            r = self._request(c, "GET", f"{self._api()}/meta/bases/{base}/tables")
        if r.status_code != 200:
            raise ConnectorError(
                f"Could not read table schema ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        for t in r.json().get("tables", []):
            if t.get("id") == table or t.get("name") == table:
                return [f.get("name") for f in t.get("fields", [])]
        raise ConnectorError(f"Table {table!r} not found in base {base!r}.")

    def schema_fields_typed(self, base: str, table: str) -> dict[str, str]:
        """{field_name: field_type} for a table (needs schema.bases:read). Used to
        route rich-text vs plain extraction (field_kind). Best-effort: a table the
        meta API can't return raises like schema_fields, but callers should degrade
        to plain handling when the map is empty/partial (a field absent from the map
        just gets the 'plain' fallback in field_kind)."""
        with self._client(headers=self._headers()) as c:
            r = self._request(c, "GET", f"{self._api()}/meta/bases/{base}/tables")
        if r.status_code != 200:
            raise ConnectorError(
                f"Could not read table schema ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        for t in r.json().get("tables", []):
            if t.get("id") == table or t.get("name") == table:
                return {f.get("name"): f.get("type") for f in t.get("fields", []) if f.get("name")}
        raise ConnectorError(f"Table {table!r} not found in base {base!r}.")

    def schema_fields_detailed(self, base: str, table: str) -> dict[str, dict]:
        """{field_name: {"id","type","options"}} for a table (needs schema.bases:read).

        The one meta call that round-2 needs everywhere: `type` for write-target
        validation (#1) and extract routing, `id` for field-ID rename-proofing (#5),
        and `options` (choices) for select-typecast new-option detection (#4). Same
        raise-on-missing-table contract as schema_fields_typed; callers that can
        degrade (dynamic_translate's explicit-jobs / meta-unreachable paths) catch and
        fall back to name-based, type-unknown handling."""
        with self._client(headers=self._headers()) as c:
            r = self._request(c, "GET", f"{self._api()}/meta/bases/{base}/tables")
        if r.status_code != 200:
            raise ConnectorError(
                f"Could not read table schema ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        for t in r.json().get("tables", []):
            if t.get("id") == table or t.get("name") == table:
                return {
                    f.get("name"): {
                        "id": f.get("id"),
                        "type": f.get("type"),
                        "options": f.get("options") or {},
                    }
                    for f in t.get("fields", [])
                    if f.get("name")
                }
        raise ConnectorError(f"Table {table!r} not found in base {base!r}.")

    def resolve_ids(self, base: str, table: str, source_field: str) -> tuple[str, str, str]:
        """Resolve (table_id, source_field_id, table_name) for a base/table via the
        meta API (needs schema.bases:read). The official Webhooks API scopes on
        table/view IDS and watches on field IDS (not names), so registration must
        turn the connection's human-named table + source field into ids.

        `table` may be a table id OR a table name; `source_field` is a field name.
        Raises ConnectorError when the table or field can't be found — registration
        MUST fail loudly rather than register a webhook watching the wrong scope."""
        with self._client(headers=self._headers()) as c:
            r = self._request(c, "GET", f"{self._api()}/meta/bases/{base}/tables")
        if r.status_code != 200:
            raise ConnectorError(
                f"Could not read base schema ({r.status_code}): {vendor_excerpt(r.text)}"
            )
        for t in r.json().get("tables", []):
            if t.get("id") == table or t.get("name") == table:
                tbl_id = t.get("id")
                fld_id = ""
                for f in t.get("fields", []):
                    if f.get("name") == source_field or f.get("id") == source_field:
                        fld_id = f.get("id")
                        break
                if not fld_id:
                    raise ConnectorError(
                        f"Source field {source_field!r} not found in table {table!r}."
                    )
                return tbl_id, fld_id, t.get("name") or table
        raise ConnectorError(f"Table {table!r} not found in base {base!r}.")

    def _record_fields(self, fields=None):
        """Select only used columns; stable IDs take precedence after a rename."""
        names = (
            fields
            if fields is not None
            else [
                self.config.get("source_field"),
                self.config.get("target_field"),
                self.config.get("title_field"),
            ]
        )
        ids = self.config.get("airtable_field_ids") or {}
        return list(dict.fromkeys(ids.get(name, name) for name in names if name))

    def fetch(
        self,
        base: str,
        table: str,
        record_ids: list[str] | None = None,
        cap: int = 500,
        by_field_id: bool = False,
        fields: list[str] | None = None,
    ) -> list[dict]:
        """Return raw records ({id, fields}). If record_ids given, only those.

        by_field_id (round-2 #5, rename-proofing): request `returnFieldsByFieldId=true`
        so each record's `fields` dict is keyed by field ID, not field NAME — a source
        column renamed in Airtable still reads by its stable id. Callers still fall
        back to a name lookup on the same record (a fake/degraded response that ignores
        the flag stays name-keyed), so this is safe to pass unconditionally when any
        id is known."""
        want = set(record_ids or [])
        out: list[dict] = []
        offset = None
        page_size = min(100, max(1, cap))
        with self._client(headers=self._headers()) as c:
            while True:
                params = {"pageSize": page_size}
                selected = self._record_fields(fields)
                if selected:
                    params["fields[]"] = selected
                if by_field_id:
                    params["returnFieldsByFieldId"] = "true"
                if offset:
                    params["offset"] = offset
                r = self._get_page(
                    lambda **kw: self._request(c, "GET", self._url(base, table), **kw),
                    params=params,
                    size_key="pageSize",
                )
                page_size = params["pageSize"]
                if r.status_code != 200:
                    raise ConnectorError(
                        f"Airtable fetch failed ({r.status_code}): {vendor_excerpt(r.text)}"
                    )
                body = r.json()
                for rec in self._refuse_oversize(body.get("records", [])):
                    if want and rec["id"] not in want:
                        continue
                    out.append(rec)
                    if len(out) >= cap:
                        return out
                offset = body.get("offset")
                if not offset:
                    break
        return out

    def patch_records(
        self, base: str, table: str, updates: dict, typecast: bool = False
    ) -> tuple[int, list[str], str]:
        """updates = {record_id: {field: text}}; merged, batched in 10s. `field` may
        be a field NAME or (round-2 #5) a field ID — Airtable accepts either as a
        cell key.

        typecast (round-2 #4): when True, sends `typecast:true` so a string written
        into a select field matches/creates an option (a no-op coercion for text
        fields, which are already strings). Gated per run by the caller behind the
        default-OFF allow_select_typecast opt-in — never set here by default.

        Attempts EVERY batch (a mid-run failure no longer aborts the later ones)
        and returns (written, failed_ids, msg): failed_ids are the record ids in
        any batch that did not write, so callers can surface/retry just those."""
        rows = [{"id": rid, "fields": flds} for rid, flds in updates.items() if flds]
        if not rows:
            return 0, [], "Nothing to write."
        written = 0
        failed_ids: list[str] = []
        errors: list[str] = []
        url = self._url(base, table)
        with self._client(headers=self._headers()) as c:
            for i in range(0, len(rows), 10):
                batch = rows[i : i + 10]
                body = {"records": batch}
                if typecast:
                    body["typecast"] = True
                try:
                    r = self._request(c, "PATCH", url, json=body)
                except Exception as e:
                    failed_ids.extend(row["id"] for row in batch)
                    errors.append(f"record {i}: {type(e).__name__}: {e}")
                    continue
                if r.status_code != 200:
                    failed_ids.extend(row["id"] for row in batch)
                    errors.append(f"record {i} ({r.status_code}): {vendor_excerpt(r.text)}")
                    continue
                written += len(r.json().get("records", batch))
        if failed_ids:
            msg = f"Updated {written} record(s); {len(failed_ids)} failed. " + "; ".join(errors[:3])
        else:
            msg = f"Updated {written} record(s)."
        return written, failed_ids, msg

    # --- record create / delete (round-2 #1; bb-io AddRecord / DeleteRecord) ------
    # A narrowly-scoped, manually/API-triggered capability — NOT wired into the
    # auto-translate/webhook loop, and untouched by the PATCH write-back above. Both
    # batch at Airtable's 10-record ceiling and attempt every batch (a dead batch
    # never aborts the rest), returning per-record success/failure so a caller can
    # surface exactly what landed. The store service layers a WriteBudget on top.
    def create_records(
        self, base: str, table: str, rows: list[dict], typecast: bool = False
    ) -> tuple[list[dict], list[dict], str]:
        """rows = [{field: value}, ...]. POST {base}/{table} in batches of 10.
        Returns (created, failed, msg): `created` are the {id, fields} objects
        Airtable minted; `failed` are the input row dicts in any batch that did not
        write (with an 'error' key). `typecast` mirrors patch_records."""
        clean = [{"fields": r} for r in rows if isinstance(r, dict) and r]
        if not clean:
            return [], [], "Nothing to create."
        created: list[dict] = []
        failed: list[dict] = []
        url = self._url(base, table)
        with self._client(headers=self._headers()) as c:
            for i in range(0, len(clean), 10):
                batch = clean[i : i + 10]
                body = {"records": batch}
                if typecast:
                    body["typecast"] = True
                try:
                    r = self._request(c, "POST", url, idempotent=False, json=body)
                except Exception as e:
                    for row in batch:
                        failed.append({**row["fields"], "error": f"{type(e).__name__}: {e}"})
                    continue
                if r.status_code not in (200, 201):
                    for row in batch:
                        failed.append(
                            {**row["fields"], "error": f"{r.status_code}: {vendor_excerpt(r.text)}"}
                        )
                    continue
                created.extend(r.json().get("records", []))
        if failed:
            msg = f"Created {len(created)} record(s); {len(failed)} failed."
        else:
            msg = f"Created {len(created)} record(s)."
        return created, failed, msg

    def delete_records(
        self, base: str, table: str, record_ids: list[str]
    ) -> tuple[list[str], list[str], str]:
        """DELETE {base}/{table}?records[]=... in batches of 10. Returns
        (deleted_ids, failed_ids, msg). A batch HTTP failure marks all its ids
        failed; the rest still proceed."""
        ids = [rid for rid in (record_ids or []) if rid]
        if not ids:
            return [], [], "Nothing to delete."
        deleted: list[str] = []
        failed: list[str] = []
        url = self._url(base, table)
        with self._client(headers=self._headers()) as c:
            for i in range(0, len(ids), 10):
                batch = ids[i : i + 10]
                try:
                    r = self._request(c, "DELETE", url, params={"records[]": batch})
                except Exception:
                    failed.extend(batch)
                    continue
                if r.status_code != 200:
                    failed.extend(batch)
                    continue
                # Airtable returns {"records":[{"id","deleted":true}, ...]}; count
                # only the ones it confirms deleted, treat the rest as failed.
                confirmed = {
                    rec.get("id") for rec in r.json().get("records", []) if rec.get("deleted")
                }
                for rid in batch:
                    (deleted if rid in confirmed else failed).append(rid)
        if failed:
            msg = f"Deleted {len(deleted)} record(s); {len(failed)} failed."
        else:
            msg = f"Deleted {len(deleted)} record(s)."
        return deleted, failed, msg

    # --- official Webhooks API (Phase 2 LIVE transport) ------------------------
    # Lifecycle: create (returns id + macSecretBase64) -> list-payloads (cursor
    # advanced only after successful processing) -> refresh (+7d keep-alive) ->
    # delete. All server-side (the PAT never leaves our backend). [CONFIRMED —
    # research §2: create/delete/list/refresh/list-payloads endpoints; 5 req/s per
    # base; 7-day PAT/OAuth expiry.] The store owns state + org-scoping; these are
    # thin, defensive HTTP shells (never raise on a keep-alive/list — the caller
    # decides how a failure surfaces).
    def create_webhook(
        self, base: str, notification_url: str, spec: dict
    ) -> tuple[bool, dict, str]:
        """POST /bases/{baseId}/webhooks. Body = {notificationUrl, specification}.
        [CONFIRMED — research §2.] On success Airtable returns
        {id, macSecretBase64, expirationTime}; the caller VAULTS macSecretBase64
        (per-connection webhook secret) + persists id/expiry in config.

        Returns (ok, data, message). `data` carries id/macSecretBase64/
        expirationTime on success (empty on failure). Raises ConnectorError only
        for a missing base/url (a programmer error), never for an HTTP failure —
        those come back as (False, {}, msg) so the registration surface can show a
        clean actionable error."""
        if not base or not notification_url:
            raise ConnectorError("Base id and a notification URL are required.")
        body = {"notificationUrl": notification_url, "specification": spec}
        try:
            url = f"{self._api()}/bases/{base}/webhooks"
            with self._client(headers=self._headers()) as c:
                r = self._request(c, "POST", url, idempotent=False, json=body)
        except Exception as e:
            return False, {}, f"{type(e).__name__}: {e}"
        if r.status_code in (200, 201):
            try:
                data = r.json() or {}
            except Exception:
                data = {}
            if not data.get("id") or not data.get("macSecretBase64"):
                # A 2xx without the two fields we NEED is a hard failure — never
                # register a webhook we can't verify (no mac secret) or refresh (no id).
                return (
                    False,
                    {},
                    (
                        "Airtable accepted the webhook but returned no "
                        f"id/macSecret: {str(data)[:160]}"
                    ),
                )
            return True, data, f"Registered webhook {data.get('id')}"
        return False, {}, f"Create webhook failed ({r.status_code}): {vendor_excerpt(r.text)}"

    def list_webhooks(self, base: str) -> tuple[bool, list, str]:
        """GET /bases/{baseId}/webhooks -> {webhooks:[{id, areNotificationsEnabled,
        expirationTime, isHookEnabled, lastSuccessfulNotificationTime, ...}]}.
        [CONFIRMED — research §2.] Used by the status surface to show live state /
        error state and to reconcile a locally-stored id against Airtable's view."""
        try:
            url = f"{self._api()}/bases/{base}/webhooks"
            with self._client(headers=self._headers()) as c:
                r = self._request(c, "GET", url)
        except Exception as e:
            return False, [], f"{type(e).__name__}: {e}"
        if r.status_code == 200:
            try:
                return True, list((r.json() or {}).get("webhooks") or []), "ok"
            except Exception:
                return True, [], "ok"
        return False, [], f"List webhooks failed ({r.status_code}): {vendor_excerpt(r.text)}"

    def delete_webhook(self, base: str, webhook_id: str) -> tuple[bool, str]:
        """DELETE /bases/{baseId}/webhooks/{webhookId}. [CONFIRMED — research §2.]
        A 404 (already gone remotely) is treated as SUCCESS — the goal state is
        "no such webhook", which a 404 already satisfies; the caller then clears
        local state cleanly. Never raises."""
        if not base or not webhook_id:
            return False, "base id and webhook id are required."
        try:
            url = f"{self._api()}/bases/{base}/webhooks/{webhook_id}"
            with self._client(headers=self._headers()) as c:
                r = self._request(c, "DELETE", url)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        if r.status_code in (200, 204, 404):
            return True, (
                "Deleted (or already gone)"
                if r.status_code == 404
                else f"Deleted webhook {webhook_id}"
            )
        return False, f"Delete webhook failed ({r.status_code}): {vendor_excerpt(r.text)}"

    def list_payloads(
        self, base: str, webhook_id: str, cursor: int | None = None
    ) -> tuple[bool, dict, str]:
        """GET /bases/{baseId}/webhooks/{webhookId}/payloads?cursor=N.
        [CONFIRMED — research §2: cursor-based payload fetch; listing payloads also
        extends the webhook +7 days.] Returns (ok, data, message) where data is the
        raw response {payloads, cursor, mightHaveMore}. `cursor` is 1-based and
        points at the NEXT payload to read; omit it to read from the start. A 404
        (webhook expired/deleted remotely) surfaces as (False, {"gone": True}, msg)
        so the caller can clear local registration cleanly. Never raises."""
        try:
            url = f"{self._api()}/bases/{base}/webhooks/{webhook_id}/payloads"
            params = {}
            if cursor is not None:
                params["cursor"] = int(cursor)
            with self._client(headers=self._headers()) as c:
                r = self._request(c, "GET", url, params=params)
        except Exception as e:
            return False, {}, f"{type(e).__name__}: {e}"
        if r.status_code == 200:
            try:
                return True, (r.json() or {}), "ok"
            except Exception:
                return True, {}, "ok"
        if r.status_code == 404:
            return False, {"gone": True}, f"Webhook {webhook_id} not found (404)."
        return False, {}, f"List payloads failed ({r.status_code}): {vendor_excerpt(r.text)}"

    def refresh_webhook(self, base: str, webhook_id: str) -> tuple[bool, bool, str, str]:
        """Extend a registered webhook's life (+7 days) via the refresh endpoint.
        [CONFIRMED — research §2: PAT/OAuth webhooks expire after 7 days; `refresh
        webhook` or `list payloads` while active extends +7 days.]

        Returns (ok, gone, expiration_time, message):
          * ok      — the refresh landed (2xx)
          * gone    — the webhook was 404 (deleted/expired remotely); the caller
                      should CLEAR the stored registration cleanly rather than treat
                      it as an error (the keep-alive's job is done — the webhook it
                      was keeping alive no longer exists).
          * expiration_time — Airtable's new expirationTime (persisted by the caller)
          * message — human string for the sync log / connection warning.
        Never raises (a keep-alive failure must not abort a scheduled sync)."""
        try:
            url = f"{self._api()}/bases/{base}/webhooks/{webhook_id}/refresh"
            with self._client(headers=self._headers()) as c:
                r = self._request(c, "POST", url)
            if r.status_code in (200, 201):
                exp = ""
                try:  # noqa: SIM105
                    exp = (r.json() or {}).get("expirationTime") or ""
                except Exception:  # noqa: S110 -- refresh succeeded; expiry stays "" if unparsable
                    pass
                return (
                    True,
                    False,
                    exp,
                    (f"Refreshed webhook {webhook_id}" + (f" (expires {exp})" if exp else "")),
                )
            if r.status_code == 404:
                return (
                    False,
                    True,
                    "",
                    f"Webhook {webhook_id} not found (404) — clearing registration.",
                )
            return False, False, "", f"Refresh failed ({r.status_code}): {vendor_excerpt(r.text)}"
        except Exception as e:
            return False, False, "", f"{type(e).__name__}: {e}"
