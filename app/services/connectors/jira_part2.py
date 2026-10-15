"""jira: part2

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import re

import httpx

from app.filters import adf
from app.services.connectors import oauth
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    PulledItem,
    _validate_target,
    reject_unwrappable_query,
    to_int,
)
from app.services.connectors.jira_part1 import (
    _ADF_FIELDS,
    _BUILTIN_TEXT_FIELDS,
    _ISSUE_KEY_RE,
    _LABEL_PREFIX,
    _META_FIELDS,
    JQL_GET_MAXLEN,
    SPEC,
    _adf_doc,
    _adf_text,
    _cf_schema_kind,
    _issue_meta,
    _safe_attachment_path,
)
from app.services.connectors.vendor_text import vendor_excerpt

_SCOPED_CRED_HINT = (
    "If the email and token are correct, the token may be a scoped API token (created with "
    "'Create API token with scopes'): those only work through api.atlassian.com, not your "
    "site URL. Create a regular API token ('Create API token', no scopes) or connect with "
    "OAuth instead."
)

_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)


def _split_order_by(jql: str) -> tuple[str, str]:
    """Split a JQL string into (conditions, ORDER BY clause). Only an ORDER BY outside
    quotes counts; the clause keeps its own `order by` words. (#642)"""
    jql = (jql or "").strip()
    for m in _ORDER_BY_RE.finditer(jql):
        head = jql[: m.start()]
        if head.count("'") % 2 == 0 and head.count('"') % 2 == 0:
            return head.strip(), jql[m.start() :].strip()
    return jql, ""


def _join_order(conditions: str, order: str) -> str:
    return f"{conditions} {order}" if conditions and order else (conditions or order)


class JiraConnector(Connector):
    spec = SPEC

    def _is_oauth(self) -> bool:
        return self.secret.get("auth_mode") == "oauth"

    def _base(self) -> str:
        """The REST API root, mode-aware.

        TOKEN MODE (legacy, UNCHANGED — the existing jira_transport tests are the
        regression proof): the user's own site, https://{site}.atlassian.net.
        OAUTH (3LO) MODE: the Atlassian gateway, https://api.atlassian.com/ex/jira/
        {cloudid}. The gateway host is a hard constant (oauth.ATLASSIAN_API_HOST)
        so it can't be retargeted, and the cloudid is format-checked
        (oauth.valid_cloudid) so it can't inject a path segment / traversal —
        that IS the SSRF guard for oauth mode (the site base_url is never contacted
        once a 3LO token exists)."""
        if self._is_oauth():
            cloudid = ((self.secret.get("oauth") or {}).get("cloudid") or "").strip()
            if not oauth.valid_cloudid(cloudid):
                raise ConnectorError(
                    "Missing or invalid Atlassian cloudid — reconnect this connection."
                )
            return f"{oauth.ATLASSIAN_API_HOST}/ex/jira/{cloudid}"
        b = (self.config.get("base_url") or "").strip().rstrip("/")
        if not b:
            raise ConnectorError("Jira site URL is required.")
        _validate_target(b)  # SSRF guard on every request path (see base._validate_target)
        return b

    def _auth(self):
        email = (self.secret.get("email") or "").strip()
        token = (self.secret.get("api_token") or "").strip()
        if not email or not token:
            raise ConnectorError("Jira email and API token are required.")
        return (email, token)

    def _auth_kwargs(self) -> dict:
        """The per-request auth kwargs, mode-aware, merged into _request().

        TOKEN MODE: httpx basic auth (email, api_token) — byte-identical to the
        pre-OAuth `auth=self._auth()` every call site used.
        OAUTH MODE: an Authorization: Bearer header (the fresh access_token). Both
        return a dict so call sites can `**self._auth_kwargs()` uniformly."""
        if self._is_oauth():
            tok = ((self.secret.get("oauth") or {}).get("access_token") or "").strip()
            if not tok:
                raise ConnectorError(
                    "Missing Atlassian OAuth access token — reconnect this connection."
                )
            return {"headers": {"Authorization": f"Bearer {tok}"}}
        return {"auth": self._auth()}

    def _get(self, c, path, **kw):
        akw = self._auth_kwargs()
        headers = {"Accept": "application/json", **akw.pop("headers", {})}
        return self._request(c, "GET", self._base() + path, headers=headers, **akw, **kw)

    def _write_kwargs(self) -> dict:
        """Auth kwargs + JSON Content-Type for a write, mode-aware. Token mode
        yields {auth, headers}; oauth mode yields {headers: {Authorization, ...}}
        — identical to the old `auth=self._auth(), headers={Content-Type: json}`
        for token connections, so existing write tests are unchanged."""
        akw = self._auth_kwargs()
        akw["headers"] = {"Content-Type": "application/json", **akw.pop("headers", {})}
        return akw

    # --- operations ---
    def test(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                r = self._get(c, "/rest/api/3/myself")
            if r.status_code == 200:
                who = r.json().get("displayName") or r.json().get("emailAddress") or "user"
                return True, f"Connected to Jira as {who}."
            msg = f"Jira returned {r.status_code}: {vendor_excerpt(r.text)}"
            if r.status_code == 401 and not self._is_oauth():
                # #645: a scoped Atlassian API token (created with "Create API token with
                # scopes") only works through api.atlassian.com/ex/jira/{cloudId}; the site
                # URL answers 401 even though the email and token are right.
                msg += " " + _SCOPED_CRED_HINT
            return False, msg
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        out: list[dict] = []
        try:
            with self._client() as c:
                r = self._get(c, "/rest/api/3/project/search", params={"maxResults": 50})
                if r.status_code != 200:
                    return []
                for p in r.json().get("values", []):
                    out.append(
                        {
                            "id": p.get("key"),
                            "name": f"{p.get('name')} ({p.get('key')})",
                            "tables": [],
                        }
                    )
        except Exception:
            return out
        return out

    def _search(self, c, jql: str, fields: str, cap: int) -> list[dict]:
        """Paginate the enhanced search endpoint (/rest/api/3/search/jql). The
        legacy /search was removed by Atlassian; the new one uses nextPageToken
        (no startAt/total).

        G7 (POST fallback for long JQL): scope-ANDing can push a query past a safe
        GET URL length (a 414/431). When the built clause exceeds JQL_GET_MAXLEN we
        switch to POST /rest/api/3/search/jql, carrying the SAME clause in the JSON
        body. This is NOT a scope bypass: _search receives an ALREADY-scoped clause
        from _scoped_clause (the single choke point) and transmits it verbatim on
        BOTH transports — the POST body cannot re-widen the query. The POST search is
        a read (no side effect), so the default idempotent retry policy is correct."""
        # #576: never page past one issue more than the document cap, whatever the
        # connection's max_results says, so a huge JQL cannot grow memory without bound
        # (the run's cap check then refuses it with a clear message).
        from app import config as _app_config

        cap = min(cap, _app_config.MAX_DOCUMENT_SEGMENTS + 1)
        issues: list[dict] = []
        token = None
        use_post = len(jql or "") > JQL_GET_MAXLEN
        field_list = [f for f in (fields or "").split(",") if f]
        while len(issues) < cap:
            want = min(100, cap - len(issues))
            if use_post:
                body_req = {"jql": jql, "maxResults": want, "fields": field_list}
                if token:
                    body_req["nextPageToken"] = token
                r = self._request(
                    c,
                    "POST",
                    self._base() + "/rest/api/3/search/jql",
                    json=body_req,
                    **self._write_kwargs(),
                )
            else:
                params = {"jql": jql, "maxResults": want, "fields": fields}
                if token:
                    params["nextPageToken"] = token
                r = self._get(c, "/rest/api/3/search/jql", params=params)
            if r.status_code != 200:
                raise ConnectorError(
                    f"Jira search failed ({r.status_code}): {vendor_excerpt(r.text)}"
                )
            body = r.json()
            page = self._refuse_oversize(body.get("issues", []))
            issues.extend(page)
            token = body.get("nextPageToken")
            if body.get("isLast") or not token or not page:
                break
        return issues[:cap]

    def pull(self) -> list[PulledItem]:
        jql = (self.config.get("jql") or "").strip()
        if not jql:
            pk = (self.config.get("project_key") or "").strip()
            jql = f"project = {pk}" if pk else "order by created DESC"
        src = (self.config.get("source_field") or "summary").split(",")[0].strip() or "summary"
        cap = to_int(self.config.get("max_results"), 50)

        items: list[PulledItem] = []
        with self._client() as c:
            for iss in self._search(c, jql, f"{src},summary", cap):
                fields = iss.get("fields", {}) or {}
                raw = fields.get(src)
                text = _adf_text(raw).strip() if not isinstance(raw, str) else raw.strip()
                if text:
                    items.append(
                        PulledItem(
                            external_id=iss["key"],
                            field=src,
                            text=text,
                            title=f"{iss['key']}: {(fields.get('summary') or '')[:50]}",
                        )
                    )
        return items

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        # A6 (adversarial audit): this generic push path used to (1) ABORT the whole
        # batch on the first non-2xx — every remaining item silently unwritten and no
        # record of which — and (2) have NO per-run write ceiling (jira_translate, its
        # sibling, has both). Now it mirrors jira_translate: a per-item try/except so a
        # single failing (or exception-raising) item lands as a STRUCTURED failure and
        # the batch continues, plus the shared WriteBudget cap so a large batch stops
        # cleanly at config.MAX_WRITES_PER_RUN. WriteBudget is imported lazily to avoid
        # a store<->jira import cycle (store already imports jira lazily).
        from app.services.connectors.store import WriteBudget

        if not items:
            return 0, "Nothing to push."
        mode = (self.config.get("target_mode") or "comment").strip().lower()
        cf = (self.config.get("target_field") or "").strip()
        wkw = self._write_kwargs()
        base = self._base()
        budget = WriteBudget()
        written = 0
        failures: list[dict] = []  # structured, for retry: {external_id, error}
        with self._client() as c:
            for it in items:
                if not budget.allow():
                    break  # ceiling reached — stop cleanly, remainder picked up next run
                # Per-item try/except: a transport error (base._request re-raises a
                # persistent one even for an idempotent PUT) must land as a per-item
                # failure, never crash the batch (parity w/ jira_translate's write loop).
                try:
                    if mode == "field" and cf:
                        r = self._request(
                            c,
                            "PUT",
                            f"{base}/rest/api/3/issue/{it.external_id}",
                            json={"fields": {cf: it.text}},
                            **wkw,
                        )
                        ok = r.status_code in (200, 204)
                    else:
                        # POST comment is non-idempotent (a retry double-posts) —
                        # retry only on 429, never on 5xx/timeout. See base._request.
                        r = self._request(
                            c,
                            "POST",
                            f"{base}/rest/api/3/issue/{it.external_id}/comment",
                            json={"body": _adf_doc(it.text)},
                            idempotent=False,
                            **wkw,
                        )
                        ok = r.status_code in (200, 201)
                except Exception as e:
                    failures.append(
                        {"external_id": it.external_id, "error": f"{type(e).__name__}: {e}"}
                    )
                    continue
                if not ok:
                    # One non-2xx is now a per-item failure, NOT a batch abort — the
                    # remaining items still get their shot (A6).
                    failures.append(
                        {
                            "external_id": it.external_id,
                            "error": f"{r.status_code}: {vendor_excerpt(r.text)}",
                        }
                    )
                    continue
                budget.record()
                written += 1
        how = "into the custom field" if (mode == "field" and cf) else "as comments"
        msg = f"Wrote {written} translation(s) back {how}."
        if failures:
            # Summarize like jira_translate (first few, then a count) so the caller sees
            # WHICH items failed instead of a single opaque abort.
            shown = "; ".join(f"{f['external_id']} ({f['error']})" for f in failures[:3])
            msg += f" {len(failures)} item(s) failed: {shown}"
            if len(failures) > 3:
                msg += f" (+{len(failures) - 3} more)"
        if budget.ceiling_hit:
            msg += (
                f" ⚠ Hit the per-run write ceiling ({budget.ceiling}) — stopped early; "
                "remaining items will be picked up on the next run."
            )
        return written, msg

    # --- per-field-type validation (editmeta / field metadata) ---
    def field_metadata(self) -> dict:
        """Map field id -> its /rest/api/3/field `schema` block ({type, custom,
        items, ...}) — the site-wide field catalogue. Built-in `summary`/
        `description` are fixed types and don't need this; custom fields
        (customfield_*) are classified against it (see _cf_schema_kind) so a rich-
        text/paragraph cf is ADMITTED and routed through the ADF walker while a
        non-text cf is skipped-and-reported (guarded, not silently corrupted).

        G1: the catalogue now carries the FULL schema (not just `schema.type`), so
        the classifier can see `schema.custom` — the discriminator between a plain
        single-line text field (`...:textfield`) and a rich-text/paragraph field
        (`...:textarea`, ADF-valued). editmeta (/issue/{key}/editmeta) is the
        per-issue alternative; the site catalogue is cheaper and sufficient here."""
        try:
            with self._client() as c:
                r = self._get(c, "/rest/api/3/field")
            if r.status_code != 200:
                return {}
            out: dict = {}
            for f in r.json() or []:
                fid = f.get("id") or f.get("key")
                if fid:
                    out[fid] = f.get("schema") or {}
            return out
        except Exception:
            return {}

    def validate_fields(self, fields) -> tuple[list, list]:
        """Split configured field ids into (usable, skipped). Built-in text fields
        pass unconditionally; a custom field must classify (via _cf_schema_kind) as
        a translatable text field — plain (`string`) OR rich-text/paragraph (ADF,
        the G1 widening). A non-text or unresolvable cf is skipped with a TYPED
        reason (reported, never written blind)."""
        meta = None
        usable, skipped = [], []
        for fld in fields:
            if fld in _BUILTIN_TEXT_FIELDS:
                usable.append(fld)
                continue
            if not fld.startswith("customfield_"):
                # An unknown built-in name (labels/status/priority/...) — not a text
                # field we support; skip by type.
                skipped.append((fld, "not a supported translatable field"))
                continue
            if meta is None:
                meta = self.field_metadata()
            kind, reason = _cf_schema_kind(meta.get(fld))
            if kind:  # "adf" (rich text) or "plain" (single-line)
                usable.append(fld)
            else:
                skipped.append((fld, reason))
        return usable, skipped

    # --- helpers for multi-locale on-the-fly translation (store.jira_translate) ---
    def _scoped_clause(self, jql, issue_keys, scope_jql) -> str:
        """Build the ONE scoped JQL clause every read (fetch_issues + approximate_count)
        funnels through — the single scope choke point. The connection's configured
        scope_jql is ANDed onto whatever the caller produced, on BOTH branches:

          * issue_keys branch — keys are allowlisted upstream (_ISSUE_KEY_RE fullmatch),
            so the key clause is injection-free; without the AND, a signed push naming
            an out-of-scope key would translate ANY readable issue (proven).
          * free-query branch — a caller jql that isn't safely wrappable is REFUSED
            (reject_unwrappable_query) BEFORE it is concatenated, because JQL binds AND
            tighter than OR: `project = SECRET) OR (project = LOC` would otherwise run
            SECRET unscoped. Only the (paren-free) caller jql is then wrapped + ANDed.

        The stored scope_jql is trusted operator config (MAY contain parens); it is the
        wrapping target, never the untrusted side. Blank scope => the token's own read
        visibility IS the declared scope and no rejection applies. Extracted so the
        count preview shares this EXACT perimeter — a caller cannot get an unscoped
        count of issues it could not translate."""
        scope = (scope_jql or "").strip()
        if issue_keys:
            # Enforced HERE as well as at the webhook intake: the /api, CLI and retry paths
            # pass caller keys straight through, and one smuggled "AB-1) OR (key is not
            # EMPTY" or "AB-1,AB-2,..." element would turn a single-issue call into a batch
            # (#517 gate). Any malformed key refuses the whole read (fail closed).
            bad = [k for k in issue_keys if not (isinstance(k, str) and _ISSUE_KEY_RE.fullmatch(k))]
            if bad:
                raise ValueError(f"malformed Jira issue key {str(bad[0])[:40]!r}")
            keys_clause = "key in (" + ",".join(issue_keys) + ")"
            # #642: the scope may end in ORDER BY; JQL allows it only once, at the very
            # end, never inside the parentheses that group the conditions.
            scope_cond, scope_order = _split_order_by(scope)
            if scope_cond:
                return _join_order(f"({keys_clause}) AND ({scope_cond})", scope_order)
            return _join_order(keys_clause, scope_order)
        caller_jql = (jql or "").strip()
        if not caller_jql:
            # No caller query: the clause IS the scope (it may carry its own `order by`),
            # or the default ordering when no scope is configured. Wrapping a bare
            # `order by created DESC` in parens is ILLEGAL JQL, so never parenthesize it.
            return scope or "order by created DESC"
        if scope:
            safe_jql = reject_unwrappable_query(caller_jql, lang="JQL")
            caller_cond, caller_order = _split_order_by(safe_jql)
            scope_cond, scope_order = _split_order_by(scope)
            order = caller_order or scope_order  # the caller's sort wins, one ORDER BY only
            if not scope_cond:
                return _join_order(caller_cond, order)
            if not caller_cond:
                return _join_order(scope_cond, order)
            return _join_order(f"({caller_cond}) AND ({scope_cond})", order)
        return caller_jql

    def approximate_count(
        self, jql: str | None = None, issue_keys: list | None = None, scope_jql: str | None = None
    ) -> int:
        """Issue-count PREVIEW via POST /rest/api/3/search/approximate-count (G8).

        The enhanced search API dropped `total` from its responses; Atlassian ships
        this sibling endpoint for counts. Pure UX: 'N issues match your JQL' before an
        operator spends a batch run. Read-only — it launches nothing. The SAME scope
        perimeter applies: the JQL is built through _scoped_clause, so a scoped
        connection's count reflects only in-scope issues and a hostile un-wrappable
        caller jql is refused here too. approximate-count is POST-only by design."""
        clause = self._scoped_clause(jql, issue_keys, scope_jql)
        with self._client() as c:
            r = self._request(
                c,
                "POST",
                self._base() + "/rest/api/3/search/approximate-count",
                json={"jql": clause},
                **self._write_kwargs(),
            )
        if r.status_code != 200:
            raise ConnectorError(f"Jira count failed ({r.status_code}): {vendor_excerpt(r.text)}")
        return to_int((r.json() or {}).get("count"), 0)

    def fetch_issues(
        self,
        jql: str | None = None,
        issue_keys: list | None = None,
        fields: tuple = ("summary", "description"),
        cap: int = 50,
        scope_jql: str | None = None,
    ) -> list[dict]:
        """Fetch issues, carrying BOTH a flat `texts` map (plain string per field —
        back-compat for summary/plain cfs) AND `raw` (the untouched field value) so
        ADF fields can be walked leaf-by-leaf and reinserted into the source tree.
        `adf_fields` lists which returned fields are ADF (rich text).

        scope_jql (F1(b) + B1, adversarial review — scope escape): the CONNECTION'S
        configured jql/project scope, passed by the caller (store.jira_translate).
        It is ANDed onto WHATEVER clause a request produces, on BOTH branches:

          * issue_keys branch (the push/targeted-translate path): a signed webhook or
            an authenticated caller names specific keys directly -- without this,
            `key in (...)` was the WHOLE clause, so ANY issue the token can read would
            translate, regardless of the connection's declared jql/project (proven: a
            signed push naming an out-of-scope key still translated).
          * free-query branch (B1, this fix): a caller-supplied `jql` (an /api caller's
            `jql`, the CLI `--jql`, the full-pull fallback) previously BECAME the whole
            clause and REPLACED the scope entirely -- an authorized-but-scoped caller
            (e.g. a linguist key) could search/translate/comment on ANY issue the
            credential can read, bypassing `project = LOC` outright (proven). We now AND
            the connection's scope_jql onto the caller jql the same way.

        The intersection is `(caller_jql) AND (scope_jql)`, both parenthesized. Cluster-B
        FOLLOW-UP (adversarial review — paren-breaking injection): parenthesis-balancing
        alone is NOT a safe defense, because the CALLER's jql can contain its OWN parens.
        JQL binds AND tighter than OR, so `project = SECRET) OR (project = LOC` would make
        `(caller) AND (scope)` == `project = SECRET OR (scoped)` -- SECRET runs UNSCOPED
        (proven end-to-end). So when a scope is configured we FIRST reject any caller jql
        containing parens or an unbalanced quote (reject_unwrappable_query); a legitimate
        scope-narrowing query never needs its own grouping. Only then is the (now
        paren-free) caller jql wrapped and ANDed. The stored scope_jql is trusted operator
        config and MAY contain parens -- it is the wrapping target, never the untrusted
        side. When scope_jql is NOT configured (blank), the clause is left as-is on EITHER
        branch and NO rejection applies: the operator declared no scope, so the token's own
        read visibility IS the declared scope -- documented in SPEC.config_fields['jql']'s
        help text. This is the single choke point: every caller (web/api/CLI/webhook/store)
        funnels its query through here, so the guard cannot be bypassed at a call site."""
        clause = self._scoped_clause(jql, issue_keys, scope_jql)
        # Context-harvest metadata (SPEC-context-metadata Phase 3B): widen the API
        # `fields=` request so Jira also returns project/issuetype/labels/summary
        # ALONGSIDE the configured content fields. These are METADATA ONLY — harvested
        # into `meta` below, NEVER added to `texts`/`raw` (the unit-extraction loop
        # still iterates ONLY the configured `fields`, so no metadata field ever
        # becomes a translatable unit). summary is included so it is available as
        # source.title even when it is not itself a configured content field.
        api_fields = list(fields) + [m for m in _META_FIELDS if m not in fields]
        out: list[dict] = []
        with self._client() as c:
            for iss in self._search(c, clause, ",".join(api_fields), cap):
                f = iss.get("fields", {}) or {}
                texts, raw, adf_fields = {}, {}, []
                for fld in fields:
                    val = f.get(fld)
                    if isinstance(val, dict) and val.get("type") == "doc":
                        # ADF field: carry the raw tree; texts holds the flattened
                        # preview (used only for empty-check / plain fallbacks).
                        if adf.extract(val).units:
                            raw[fld] = val
                            adf_fields.append(fld)
                            texts[fld] = _adf_text(val).strip()
                    else:
                        t = val if isinstance(val, str) else _adf_text(val)
                        if (t or "").strip():
                            texts[fld] = t.strip()
                            raw[fld] = t.strip()
                out.append(
                    {
                        "key": iss["key"],
                        "summary": f.get("summary", ""),
                        "texts": texts,
                        "raw": raw,
                        "adf_fields": adf_fields,
                        "meta": _issue_meta(f),
                    }
                )
        return out[:cap]

    def _self_account_id(self, c) -> str:
        """Our OWN authenticated accountId (from /rest/api/3/myself), memoised for
        this connector instance. Used by fetch_comments to tell OUR labelled
        replies from human/integration comments by AUTHORSHIP rather than a
        spoofable body marker (D6). A fetch failure yields "" — the caller then
        falls back to the marker check, never to a false identity match.

        D6-A (review, transient-failure caching): ONLY a successful (non-empty)
        accountId is cached. A transient /myself failure (503/timeout) at the
        start of a run must NOT poison this connector instance for its whole
        lifetime — store.py's comment loop reuses one connector across every
        issue, so caching "" here would silently degrade the ENTIRE run to the
        spoofable marker fallback (dropping legitimate human marker-prefixed
        comments) even after Jira recovers. Leaving failures uncached means the
        next call simply retries /myself."""
        cached = getattr(self, "_self_aid", None)
        if cached:  # only a non-empty (successful) id is cached
            return cached
        aid = ""
        try:
            r = self._get(c, "/rest/api/3/myself")
            if r.status_code == 200:
                aid = (r.json().get("accountId") or "").strip()
        except Exception:
            aid = ""
        if aid:
            self._self_aid = aid  # cache only on success; failures retry next call
        return aid

    def fetch_comments(self, issue_key: str, cap: int = 20) -> list[dict]:
        """Existing comments on an issue as [{id, body(ADF)}] — the source for the
        opt-in translate-existing-comments path. ADF-aware (body is a doc).

        F1(b) / D6 (review + audit, self-feedback loop): comments WE posted are
        excluded here so they can never be picked up and re-translated. The
        DECISIVE check is authorship — a comment is treated as ours iff its
        author.accountId matches our own (self_aid, from /rest/api/3/myself). The
        _LABEL_PREFIX marker is only a fallback when self_aid is unavailable. This
        keeps a legitimate human comment that happens to start with 🌐 (or a
        spoofed one from another account) as a real translation source — see the
        module docstring for the two defects this closes.

        D6-B (doc note): the exclusion is by ACCOUNT, not by content — any comment
        posted by the connection's own authenticated account is treated as ours and
        excluded from translation, by design. If a human uses their own personal
        API token/PAT as this connection's credential (rather than a dedicated
        integration/bot account), that human's OWN comments on the issue will be
        silently excluded too. Recommend configuring a dedicated integration
        account for this connection to avoid that overlap."""
        out: list[dict] = []
        with self._client() as c:
            self_aid = self._self_account_id(c)
            r = self._get(
                c, f"/rest/api/3/issue/{issue_key}/comment", params={"maxResults": min(100, cap)}
            )
            if r.status_code != 200:
                return []
            for cm in (r.json().get("comments") or [])[:cap]:
                body = cm.get("body")
                author_id = ((cm.get("author") or {}).get("accountId") or "").strip()
                if self_aid:
                    # Authorship is decisive: skip iff WE authored it. A 🌐-prefixed
                    # comment from any OTHER account is a genuine translation source.
                    if author_id and author_id == self_aid:
                        continue
                elif _adf_text(body).lstrip().startswith(_LABEL_PREFIX):
                    # Degraded fallback (couldn't resolve our own accountId): the
                    # marker is the only anti-repost signal left.
                    continue
                out.append({"id": str(cm.get("id")), "body": body})
        return out

    def post_comment(
        self, issue_key: str, text: str, label: str = "", blocks: list | None = None
    ) -> tuple[bool, str]:
        """Post a comment. `blocks` (Issue #643): ADF block nodes (paragraphs, lists, ...)
        rebuilt from the translated source tree; when given they become the comment body
        (after the label paragraph) instead of the flat `text`, so lists and paragraph
        breaks survive."""
        if blocks is not None:
            head = _adf_doc(f"{_LABEL_PREFIX} {label}")["content"] if label else []
            doc = {"type": "doc", "version": 1, "content": head + list(blocks)}
        else:
            doc = _adf_doc(f"{_LABEL_PREFIX} {label}\n\n{text}" if label else text)
        try:
            with self._client() as c:
                # Non-idempotent: a resend after a timeout/5xx could double-post
                # the comment, so retry only on 429 (see base._request). A
                # transport error is re-raised there rather than retried; catch
                # it here so a timed-out write is recorded as a single failure.
                r = self._request(
                    c,
                    "POST",
                    f"{self._base()}/rest/api/3/issue/{issue_key}/comment",
                    json={"body": doc},
                    idempotent=False,
                    **self._write_kwargs(),
                )
        except httpx.TransportError as e:
            return False, f"{type(e).__name__}: {e}"
        ok = r.status_code in (200, 201)
        return ok, "ok" if ok else f"{r.status_code}: {vendor_excerpt(r.text)}"

    def set_field(
        self, issue_key: str, field: str, text: str, expected_current: str | None = None
    ) -> tuple[bool, str]:
        """Plain write-back: PUT a string field, or (legacy path) a plain-text ADF
        field built from a flat string. Prefer set_field_adf for rich fields so
        structure survives — this remains for plain cfs and the summary field.

        expected_current (F2, concurrent-edit guard): the plain-text value we
        fetched at the start of the run, if the caller has it. See _put_field."""
        value = _adf_doc(text) if field in _ADF_FIELDS else text  # rich fields are ADF
        return self._put_field(issue_key, field, value, expected_current=expected_current)

    def set_field_adf(
        self, issue_key: str, field: str, adf_doc: dict, expected_current: dict | None = None
    ) -> tuple[bool, str]:
        """Write a full ADF value (already reinserted by the walker) into a rich
        field. The caller has run adf.reinsert + adf.structure_ok, so this is a
        straight PUT — no flattening.

        expected_current (F2, concurrent-edit guard): the source ADF tree we
        fetched at the start of the run, if the caller has it. See _put_field."""
        return self._put_field(issue_key, field, adf_doc, expected_current=expected_current)

    def _current_field_text(self, c, issue_key: str, field: str):
        """Re-GET a single field's CURRENT value (F2 concurrent-edit guard). Plain
        fields return a string (or None if cleared); ADF fields return the raw
        dict tree so it can be compared structurally against what we fetched.

        LIVE-QA ASSUMPTION (needs live-tenant QA, review LOW): this compares the tree from the
        /search/jql fetch against the tree from this per-issue GET. If a live Jira
        NORMALIZES ADF differently between those two endpoints, the equality check would
        false-trip and silently skip the rich-text custom field (G1) every run. This is
        the SAME exposure `description` already carries and which works in practice, and
        the hermetic fake assumes byte-identical trees — so it must be confirmed against a
        real tenant before the G1 cf path is relied on (one live rich-text-cf write with
        no concurrent edit must actually write, not skip)."""
        r = self._get(c, f"/rest/api/3/issue/{issue_key}", params={"fields": field})
        if r.status_code != 200:
            return None, False  # can't confirm -> caller treats as "unknown, proceed"
        val = (r.json().get("fields") or {}).get(field)
        # ADF fields compare as the raw tree; plain fields as text. Shape-detect an
        # ADF-VALUED CUSTOM field (G1) so a rich-text cf isn't flattened here and then
        # mis-compared against the source tree the write path passes as
        # expected_current — which would falsely trip the concurrent-edit guard on
        # every run (dict != flattened string), silently refusing to write it.
        if field in _ADF_FIELDS or (isinstance(val, dict) and val.get("type") == "doc"):
            return val, True
        return (val if isinstance(val, str) else _adf_text(val)), True

    def _put_field(
        self, issue_key: str, field: str, value, expected_current=None
    ) -> tuple[bool, str]:
        """PUT one field. F2 (review, last-write-wins clobber): when the caller
        passes `expected_current` (the value we fetched BEFORE translating), we
        re-GET the field immediately before writing and compare. If it no longer
        matches what we fetched, a human (or another process) edited it in the
        window between our fetch and our write — we SKIP the write and report a
        "concurrent edit detected" reason instead of clobbering it. This narrows
        the clobber window to the (short) re-GET-to-PUT gap; see the module
        docstring for the full threat-model note. expected_current=None (the
        caller doesn't have a fetched value to compare) skips the check entirely
        — unchanged behavior for any call site that predates this guard."""
        # A PUT to a fixed issue id IS idempotent, so base._request retries a
        # transient transport error — but a PERSISTENT one is re-raised (base._request
        # docstring). Catch it here and return (False, msg) so a timed-out field write
        # is recorded as a single per-pair failure, never escaping to crash the whole
        # run — parity with post_comment above (which caught it; _put_field previously
        # did not — the same create-vs-update asymmetry fixed in freshdesk's client).
        try:
            with self._client() as c:
                if expected_current is not None:
                    current, confirmed = self._current_field_text(c, issue_key, field)
                    if confirmed and current != expected_current:
                        return (
                            False,
                            "concurrent edit detected — field changed since fetch, skipped",
                        )
                r = self._request(
                    c,
                    "PUT",
                    f"{self._base()}/rest/api/3/issue/{issue_key}",
                    json={"fields": {field: value}},
                    **self._write_kwargs(),
                )
        except httpx.TransportError as e:
            return False, f"{type(e).__name__}: {e}"
        ok = r.status_code in (200, 204)
        return ok, "ok" if ok else f"{r.status_code}: {vendor_excerpt(r.text)}"

    # --- Outbound attachments (SPEC-attachments Phase A, tunnel-free) ---------
    def list_attachments(self, issue_key: str) -> list[dict]:
        """The attachment metadata array on an issue as
        [{id, filename, mimeType, size, content, author_id}]. `content` is the
        authed download URL Jira returns for the raw bytes. author_id
        (author.accountId) powers the loop-guard: an attachment WE uploaded is
        skipped so a translated file is never re-detected as source work (the
        same authorship seam as fetch_comments' D6 check)."""
        out: list[dict] = []
        with self._client() as c:
            r = self._get(c, f"/rest/api/3/issue/{issue_key}", params={"fields": "attachment"})
            if r.status_code != 200:
                return []
            atts = ((r.json().get("fields") or {}).get("attachment")) or []
        for a in atts:
            if not isinstance(a, dict):
                continue
            out.append(
                {
                    "id": str(a.get("id") or ""),
                    "filename": a.get("filename") or "",
                    "mimeType": a.get("mimeType") or "application/octet-stream",
                    "size": to_int(a.get("size"), 0),
                    "content": a.get("content") or "",
                    "author_id": ((a.get("author") or {}).get("accountId") or "").strip(),
                }
            )
        return out

    @staticmethod
    def _same_origin(url: str, base: str) -> bool:
        from urllib.parse import urlsplit

        u, b = urlsplit(url), urlsplit(base)
        return (u.scheme, u.hostname, u.port) == (b.scheme, b.hostname, b.port)

    def _resolve_attachment_url(self, url: str, base: str) -> str:
        """Resolve a Jira attachment `content` URL to an in-perimeter, fetchable URL.

        TOKEN mode (unchanged): a relative URL is joined to the site base; an absolute
        same-origin URL passes; anything else is refused (SSRF defense in depth even
        though Jira produced it).

        OAUTH mode (G3 fix): Jira returns attachment `content` URLs on the SITE host
        (*.atlassian.net), but a 3LO token only authenticates against the gateway
        (api.atlassian.com/ex/jira/{cloudid}) — so a same-origin check against the
        gateway base refused EVERY attachment (the fail-closed bug the digest flagged).
        A content URL on the connection's OWN stored site host (captured at OAuth-connect
        time via accessible-resources -> oauth.site_url) whose path is a gateway-
        addressable /rest/... attachment endpoint is REWRITTEN onto the gateway base and
        fetched with the bearer token. The request therefore still only ever leaves for
        api.atlassian.com — the SSRF perimeter is preserved, and the site host is
        Atlassian-issued config, never caller input. A legacy /secure/... content URL is
        NOT gateway-addressable, so it is refused with a CLEAR reason (not a silent
        fail): the operator learns the one attachment shape OAuth mode cannot serve, and
        that a token-mode connection downloads it fine."""
        u = (url or "").strip()
        if u.startswith("/"):
            return base + _safe_attachment_path(u)
        if self._same_origin(u, base):
            return u
        if self._is_oauth():
            from urllib.parse import urlsplit

            site = ((self.secret.get("oauth") or {}).get("site_url") or "").strip()
            site_host = (urlsplit(site).hostname or "").lower() if site else ""
            parts = urlsplit(u)
            if site_host and (parts.hostname or "").lower() == site_host:
                path = parts.path or ""
                if path.startswith("/rest/"):
                    return (
                        base
                        + _safe_attachment_path(path)
                        + (f"?{parts.query}" if parts.query else "")
                    )
                raise ConnectorError(
                    "OAuth mode: this attachment is a legacy /secure link Jira serves "
                    "only on the site host, which a 3LO token cannot reach through the "
                    "Atlassian gateway — skipped. (A token-mode connection downloads it "
                    "fine.)"
                )
        raise ConnectorError(
            "attachment content URL is outside the connection's Jira site — refused"
        )

    def download_attachment(self, content_url: str, max_bytes: int | None = None) -> bytes | None:
        """Fetch an attachment's raw bytes from the authed `content` URL Jira
        returned. SSRF-guarded: the URL MUST live under this connection's own API
        base (scheme + host + port) — a `content` value pointing anywhere else is
        refused (defense in depth even though the URL came from Jira's own
        response, mirroring base._validate_target's posture). Returns None on a
        non-200 or an over-`max_bytes` body (a second, byte-length cap after the
        metadata-size pre-check the caller already did).

        OAUTH mode (G3): the API base is the Atlassian gateway
        (api.atlassian.com/ex/jira/{cloudid}) but Jira returns attachment `content`
        URLs on the USER SITE host (…atlassian.net). _resolve_attachment_url rewrites a
        site-host /rest/... content URL onto the gateway so the bearer token can fetch
        it in-perimeter; a legacy /secure/... URL raises a clear ConnectorError (the
        caller records it as a per-attachment skip) rather than failing silently."""
        url = (content_url or "").strip()
        if not url:
            return None
        base = self._base()
        url = self._resolve_attachment_url(url, base)
        akw = self._auth_kwargs()
        headers = {**akw.pop("headers", {})}
        with self._client() as c:  # noqa: SIM117
            # STREAM the body with a cumulative byte cap (hardening): a fully-buffered
            # download would trust the metadata size the caller pre-checked and could be
            # defeated by a body that declares a small size but sends a large one. We
            # accumulate chunks and ABORT the moment the running total exceeds max_bytes,
            # so peak memory is bounded to ~max_bytes + one chunk regardless of the
            # declared/Content-Length value. httpx.MockTransport supports .stream(), so
            # the hermetic suite exercises this path too. Idempotent GET, so a plain
            # stream (no _request retry wrapper) is fine — a failed read simply returns
            # None and the caller skips the attachment.
            with c.stream("GET", url, headers=headers, **akw) as r:
                if r.status_code != 200:
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        return None  # streamed past the cap — abort, never buffer whole
                    chunks.append(chunk)
        return b"".join(chunks)

    # Jira REQUIRES this XSRF opt-out header on the attachments endpoint — a
    # documented hard requirement; the multipart POST 403s without it.
    _NO_CHECK_HEADER = {"X-Atlassian-Token": "no-check"}  # noqa: RUF012

    def upload_attachment(
        self, issue_key: str, filename: str, data: bytes, mime: str = "application/octet-stream"
    ) -> tuple[bool, str, str]:
        """POST a file to an issue as a NEW attachment (Jira attachments are
        ADDITIVE — never an overwrite; the translated file appears alongside the
        source). The `X-Atlassian-Token: no-check` header is REQUIRED and the
        multipart field name MUST be `file`. Content-Type is left to httpx so the
        multipart boundary is set correctly (we deliberately do NOT use
        _write_kwargs, which would force application/json). Non-idempotent (a
        resend after a timeout/5xx would double-attach), so base._request retries
        only on 429 and re-raises a transport error, which we catch as a single
        failure. Returns (ok, message, new_attachment_id)."""
        akw = self._auth_kwargs()
        headers = {**self._NO_CHECK_HEADER, **akw.pop("headers", {})}
        files = {"file": (filename, data, mime)}
        try:
            with self._client() as c:
                r = self._request(
                    c,
                    "POST",
                    f"{self._base()}/rest/api/3/issue/{issue_key}/attachments",
                    files=files,
                    headers=headers,
                    idempotent=False,
                    **akw,
                )
        except httpx.TransportError as e:
            return False, f"{type(e).__name__}: {e}", ""
        if r.status_code not in (200, 201):
            return False, f"{r.status_code}: {vendor_excerpt(r.text)}", ""
        new_id = ""
        try:
            body = r.json()
            if isinstance(body, list) and body and isinstance(body[0], dict):
                new_id = str(body[0].get("id") or "")
        except Exception:
            new_id = ""
        return True, "ok", new_id
