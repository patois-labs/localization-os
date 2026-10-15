"""shopify: part2

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from typing import ClassVar

from app.services.connectors import base
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    PulledItem,
    fetch_cap,
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
from app.services.connectors.shopify_part1 import (
    _DIGEST_RACE_CODES,
    _GID_RE,
    _GID_TYPE_TO_ENUM,
    _MAX_RUN_THROTTLE_WAIT,
    _MAX_THROTTLE_RETRIES,
    _MAX_TOTAL_THROTTLE_WAIT,
    _NAME_SKIP_EXACT,
    _SUPPORTED_RESOURCE_TYPES,
    _SUPPORTED_SET,
    _TRANSLATABLE_CONTENT_TYPES,
    API_VERSION,
    SPEC,
    ShopifyTranslationStrategy,
    _parse_names,
    _RegisterOutcome,
    _Unit,
    _validate_store_handle,
    log,
)
from app.services.connectors.strategy import (
    ABSENT,
)
from app.services.connectors.vendor_text import vendor_excerpt, vendor_quote

# --------------------------------------------------------------------------
# Client-credentials token cache (#653). A Dev Dashboard app has no permanent token: it
# trades its client id + secret for an access token that lives 24 hours
# (https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant).
# Connectors are rebuilt for every web action and run, so the minted token is cached in this
# process, in memory only, keyed by (store, client id, hash of the secret) so a rotated
# secret or another store never reuses it. It is never written to the database or a log.
# --------------------------------------------------------------------------
_TOKEN_REFRESH_MARGIN = 60.0  # re-mint this many seconds before Shopify's expiry
_TOKEN_CACHE: dict = {}
_TOKEN_CACHE_LOCK = threading.Lock()
_now = time.time  # injectable clock for tests
_SAFE_ERROR_RE = re.compile(r"^[A-Za-z0-9_ .:-]{1,80}$")


def _reset_token_cache() -> None:
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE.clear()


class ShopifyConnector(Connector, ConnectorPlugin):
    spec = SPEC

    capability = Capability(
        key="shopify",
        topology=Topology.T2,
        auth=(AuthKind.API_KEY,),  # Admin API access token, or client id + secret minted into one
        api_version=API_VERSION,
        rich_text=RichText.NONE,  # v1 translates field values whole (HTML deferred)
        regions="store_name",
        change_signals=ChangeSignals(
            webhook_sig=None,  # webhooks OUT of scope v1 -> poll-first
            poll_field="updatedAt",  # metadata-tainted -> NO watermark; hash is the gate
            stale_flag="outdated",  # server flag exists; hash-diff drives v1
            thin_payload=True,
        ),
        anti_loop_label=False,
    )

    def __init__(self, config, secret, *, transport=None):
        super().__init__(config, secret, transport=transport)
        self.strategy = ShopifyTranslationStrategy()
        self._locales_cache: list[dict] | None = None
        # Field-selection visibility (taxonomy D): accumulated across extract() calls,
        # surfaced by build_message / build_result and logged loud for machine-type skips.
        self._sel_selected: set = set()
        self._sel_skipped: dict = {}
        self._sel_warned: set = set()
        # Per-run INCOMPLETE markers (partial write): {(resource_id, locale): {key: reason}}.
        # Surfaced by build_message / build_result. A held/poison key on a partial write was
        # never registered, so the full-coverage probe (registered-set) treats the pair as
        # COVERED and it is SUPPRESSED — NOT auto-re-attempted (no re-spend loop). Recovery
        # is operator-only: retry_failed (which passes the pair as only_pairs, and the flow
        # bypasses suppression for it) OR a source edit (changes the content hash). A DRIFTED
        # key differs: its source change alters the hash, so it auto-returns next run.
        self._incomplete: dict = {}
        # Per-run registered-key delta for FULL-coverage suppression: {"<rid>:<loc>":
        # {keys}} of keys Shopify ACCEPTED this run. Persisted (unioned) into config
        # `shopify_registered` PER PAIR by _shopify_on_write_ok — BEFORE the end-of-run hash
        # merge (H1) — so a crash can't leave a hash without its tracking. fully_covered
        # checks these against the live overlay so a deleted key surfaces for restore
        # (MEDIUM-3).
        self._registered_delta: dict = {}
        # Run-level cumulative throttle-wait budget (LOW-1). Per-CALL wait is already capped
        # (_MAX_TOTAL_THROTTLE_WAIT); this bounds the SUM across a run's many GraphQL calls
        # so a persistently-throttled run fails cleanly (retryable) instead of stalling.
        self._throttle_total: float = 0.0

    def _record_registered(self, resource_id, target_locale, keys) -> None:
        if not keys:
            return
        k = f"{resource_id}:{str(target_locale).lower()}"
        self._registered_delta.setdefault(k, set()).update(keys)

    def _record_incomplete(self, resource_id, locale, held: dict) -> None:
        if not held:
            return
        self._incomplete.setdefault((str(resource_id), str(locale)), {}).update(held)

    # ------------------------------------------------------------ auth / base
    def _store(self) -> str:
        return _validate_store_handle(self.config.get("store_name"))

    def _endpoint(self) -> str:
        """The single Admin GraphQL endpoint. The store handle is allowlisted (gotcha #1)
        BEFORE interpolation; _validate_target is a defence-in-depth SSRF check on the
        fully-built https URL."""
        url = f"https://{self._store()}.myshopify.com/admin/api/{API_VERSION}/graphql.json"
        base._validate_target(url)
        return url

    def _client_credentials(self) -> tuple[str, str]:
        return (
            (self.secret.get("client_id") or "").strip(),
            (self.secret.get("client_secret") or "").strip(),
        )

    def _uses_client_credentials(self) -> bool:
        cid, csec = self._client_credentials()
        return bool(cid and csec)

    def _token_cache_key(self) -> tuple:
        cid, csec = self._client_credentials()
        return (self._store(), cid, hashlib.sha256(csec.encode("utf-8")).hexdigest())

    def _invalidate_token(self) -> None:
        with _TOKEN_CACHE_LOCK:
            _TOKEN_CACHE.pop(self._token_cache_key(), None)

    def _mint_token(self) -> tuple[str, float]:
        """Client-credentials grant: POST client id + secret (form encoded) to the store's
        /admin/oauth/access_token and return (access_token, seconds_valid). The request
        carries no scopes (Shopify grants those selected on the app version). Neither the
        secret nor the token is logged, and no upstream body is echoed: only Shopify's short
        `error` code is surfaced, so a secret echoed by a proxy cannot leak into an error."""
        cid, csec = self._client_credentials()
        url = f"https://{self._store()}.myshopify.com/admin/oauth/access_token"
        base._validate_target(url)
        with self._client() as c:
            r = self._request(
                c,
                "POST",
                url,
                data={"grant_type": "client_credentials", "client_id": cid, "client_secret": csec},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                idempotent=False,
            )
        body: dict = {}
        try:
            parsed = r.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}
        if r.status_code != 200:
            code = str(body.get("error") or "").strip()
            # Fail closed: a short code that still carries the client id or secret (an
            # upstream or proxy echoing the form) is dropped, not surfaced.
            if not _SAFE_ERROR_RE.match(code) or any(v and v in code for v in (cid, csec)):
                code = ""
            hint = ""
            if code == "shop_not_permitted":
                hint = (
                    " The app and the store must belong to the same Shopify organization "
                    "(create the store from the Dev Dashboard's Dev stores page)."
                )
            elif r.status_code in (400, 401, 403):
                hint = (
                    " Check the client id and client secret, and that the app is "
                    "installed on the store."
                )
            raise ConnectorError(
                f"Shopify refused the client id / secret (HTTP {r.status_code}"
                + (f", {code}" if code else "")
                + ")."
                + hint
            )
        token = str(body.get("access_token") or "").strip()
        if not token:
            raise ConnectorError("Shopify token response had no access_token.")
        try:
            ttl = float(body.get("expires_in") or 0)
        except (TypeError, ValueError):
            ttl = 0.0
        return token, (ttl if ttl > 0 else 3600.0)

    def _token(self) -> str:
        cid, csec = self._client_credentials()
        if cid or csec:
            if not (cid and csec):
                missing = "client secret" if cid else "client id"
                raise ConnectorError(
                    f"Shopify {missing} is missing: enter both the client id and the "
                    "client secret (or clear both and use an Admin API access token)."
                )
            key = self._token_cache_key()
            with _TOKEN_CACHE_LOCK:
                hit = _TOKEN_CACHE.get(key)
                if hit and hit[1] - _TOKEN_REFRESH_MARGIN > _now():
                    return hit[0]
            token, ttl = self._mint_token()
            with _TOKEN_CACHE_LOCK:
                _TOKEN_CACHE[key] = (token, _now() + ttl)
            return token
        token = (self.secret.get("access_token") or "").strip()
        if not token:
            raise ConnectorError(
                "Shopify credentials are required: a client id + client secret "
                "(Dev Dashboard app) or an Admin API access token."
            )
        return token

    def _headers(self) -> dict:
        return {
            "X-Shopify-Access-Token": self._token(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # --------------------------------------------------------------- GraphQL
    @staticmethod
    def _is_throttled(errors) -> bool:
        for e in errors or []:
            if (
                isinstance(e, dict)
                and str((e.get("extensions") or {}).get("code") or "").upper() == "THROTTLED"
            ):
                return True
        return False

    @staticmethod
    def _throttle_wait(body: dict, attempt: int) -> float:
        """Backoff for a THROTTLED response: prefer the server's cost accounting
        ((requested - available) / restoreRate), else exponential; clamped at
        base.MAX_SLEEP."""
        cost = (body.get("extensions") or {}).get("cost") or {}
        ts = cost.get("throttleStatus") or {}
        try:
            need = float(cost.get("requestedQueryCost") or 0)
            avail = float(ts.get("currentlyAvailable") or 0)
            rate = float(ts.get("restoreRate") or 0)
            if need > avail and rate > 0:
                return min(max(0.0, (need - avail) / rate), base.MAX_SLEEP)
        except (TypeError, ValueError):
            pass
        return min(base.BACKOFF_BASE * (2**attempt), base.MAX_SLEEP)

    @staticmethod
    def _errmsg(errors) -> str:
        return vendor_quote("; ".join(str((e or {}).get("message") or e) for e in (errors or [])))

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        """Execute one Admin GraphQL operation and return its `data`. Handles cost-based
        THROTTLED backoff (digest §5) — the reference has none. HTTP-level 429/5xx are
        already retried by base._request; a THROTTLED is a 200 with a top-level error, so
        it is handled here. Every operation we run is an idempotent read or an idempotent
        UPSERT (translationsRegister), so idempotent=True is safe (a retried write re-runs
        the same upsert)."""
        url = self._endpoint()
        headers = self._headers()
        total_wait = 0.0
        reminted = False
        for attempt in range(_MAX_THROTTLE_RETRIES):
            with self._client() as c:
                r = self._request(
                    c,
                    "POST",
                    url,
                    json={"query": query, "variables": variables or {}},
                    headers=headers,
                    idempotent=True,
                )
            if r.status_code == 401 and self._uses_client_credentials() and not reminted:
                # The minted token expired or was revoked mid-run: drop it, mint a fresh one
                # and retry ONCE (a second 401 is a real credential problem).
                reminted = True
                self._invalidate_token()
                headers = self._headers()
                continue
            if r.status_code != 200:
                raise ConnectorError(
                    f"Shopify Admin API HTTP {r.status_code}: {vendor_excerpt(r.text)}"
                )
            try:
                body = r.json()
            except Exception as e:
                raise ConnectorError(f"Shopify Admin API returned non-JSON: {e}") from e
            errors = body.get("errors")
            if errors and self._is_throttled(errors):
                if (
                    attempt < _MAX_THROTTLE_RETRIES - 1
                    and total_wait < _MAX_TOTAL_THROTTLE_WAIT
                    and self._throttle_total < _MAX_RUN_THROTTLE_WAIT
                ):
                    wait = min(
                        self._throttle_wait(body, attempt),
                        _MAX_TOTAL_THROTTLE_WAIT - total_wait,
                        _MAX_RUN_THROTTLE_WAIT - self._throttle_total,
                    )
                    total_wait += wait
                    self._throttle_total += wait
                    base._sleep(wait)
                    continue
                raise ConnectorError(
                    "Shopify Admin API throttled (GraphQL cost limit) after "
                    f"{_MAX_THROTTLE_RETRIES} attempts / {_MAX_RUN_THROTTLE_WAIT:.0f}s run "
                    "budget — reduce the run size or retry later."
                )
            if errors:
                raise ConnectorError(f"Shopify GraphQL error: {self._errmsg(errors)}")
            return body.get("data") or {}
        raise ConnectorError("Shopify Admin API throttled (cost limit).")

    # --------------------------------------------------------------- locales
    def _fetch_locales(self) -> list[dict]:
        if self._locales_cache is not None:
            return self._locales_cache
        data = self._graphql("{ shopLocales { locale name primary published } }")
        locs = [
            x for x in (data.get("shopLocales") or []) if isinstance(x, dict) and x.get("locale")
        ]
        self._locales_cache = locs
        return locs

    def enabled_locales(self) -> list[str]:
        return [str(x["locale"]).strip().lower() for x in self._fetch_locales()]

    def _locale_map(self) -> dict[str, str]:
        return {
            str(x["locale"]).strip().lower(): str(x["locale"]).strip()
            for x in self._fetch_locales()
        }

    def resolve_locale(self, loc: str) -> str:
        return self._locale_map().get(str(loc).strip().lower(), str(loc).strip())

    def unpublished_locales(self) -> set[str]:
        """Enabled-but-UNPUBLISHED locale codes (lowercased). A translation written to
        one of these succeeds but does not appear on the storefront until a human
        publishes the language in Shopify admin (digest §6.6) — surfaced as an
        operator-visible note, not a block."""
        return {
            str(x["locale"]).strip().lower()
            for x in self._fetch_locales()
            if not x.get("published") and not x.get("primary")
        }

    def _primary_locale(self) -> str:
        """The shop's PRIMARY locale (from shopLocales) — the one Shopify's
        translatableContent is ALWAYS authored in (digest §3)."""
        for x in self._fetch_locales():
            if x.get("primary"):
                return str(x["locale"]).strip()
        locs = self._fetch_locales()
        return str(locs[0]["locale"]).strip() if locs else "en"

    def _source_locale(self) -> str:
        # HIGH-3: Shopify's translatableContent is ALWAYS the shop's PRIMARY locale, so the
        # source we translate FROM is the primary — NEVER a config override. Accepting an
        # override would (a) mislabel primary-locale text as another language to the engine
        # and (b) turn the actual primary into a writable TARGET receiving MT. The config
        # `source_locale` field is informational and MUST match the primary; preflight
        # fails loudly (naming the primary) if it does not.
        return self._primary_locale()

    # ---------------------------------------------------------- resource types
    def _resource_types(self) -> list[str]:
        raw = (self.config.get("resource_types") or "").strip()
        if not raw:
            return ["PRODUCT"]
        out: list[str] = []
        for x in raw.replace(";", ",").replace("\n", ",").split(","):
            t = x.strip().upper()
            if not t:
                continue
            if t not in _SUPPORTED_SET:
                raise ConnectorError(
                    f"Unsupported Shopify resource type {x.strip()!r}; supported: "
                    f"{', '.join(_SUPPORTED_RESOURCE_TYPES)}."
                )
            if t not in out:
                out.append(t)
        if not out:
            raise ConnectorError("At least one resource type is required.")
        return out

    def _validate_gid(self, rid: str, types: list[str] | None = None) -> str:
        """Fail-closed scope + shape check for a caller-supplied resourceId (taxonomy A).
        Must be a well-formed `gid://shopify/<Type>/<numeric>` whose <Type> maps to a
        resource type this connection is CONFIGURED for — else refuse. The GID is only
        ever passed as a GraphQL VARIABLE (never string-interpolated into a query), but
        this keeps an out-of-scope or malformed id from ever reaching the wire."""
        r = str(rid or "").strip()
        m = _GID_RE.match(r)
        if not m:
            raise ConnectorError(
                f"Unsafe resource id {rid!r}: must be a Shopify GID of the form "
                f"'gid://shopify/<Type>/<number>'."
            )
        enum = _GID_TYPE_TO_ENUM.get(m.group(1))
        types = types if types is not None else self._resource_types()
        if enum is None or enum not in types:
            raise ConnectorError(
                f"Resource id {rid!r} (type {m.group(1)}) is not a resource type this "
                f"connection is configured for ({', '.join(types)}) — refusing to target it."
            )
        return r

    # ----------------------------------------------------------- test / list
    # Scopes the connector cannot work without (checked by test()); a `write_X` grant also
    # satisfies `read_X` (Shopify access scopes: write implies read).
    _REQUIRED_SCOPES = ("read_locales", "read_translations", "write_translations")
    # Per resource type, ANY one of these satisfies the read (a warning, not a failure: the
    # exact scope a content type needs is the part least verified against a live store).
    _RESOURCE_SCOPES: ClassVar[dict] = {
        "PRODUCT": ("read_products",),
        "COLLECTION": ("read_products",),
        "ARTICLE": ("read_content", "read_online_store_pages"),
        "BLOG": ("read_content", "read_online_store_pages"),
        "PAGE": ("read_content", "read_online_store_pages"),
    }

    def _granted_scopes(self) -> set | None:
        """Handles of the access scopes the app was granted (currentAppInstallation), or
        None when Shopify will not say. Read-only, needs no scope of its own."""
        try:
            data = self._graphql("{ currentAppInstallation { accessScopes { handle } } }")
        except ConnectorError:
            return None
        inst = data.get("currentAppInstallation")
        if not isinstance(inst, dict):
            return None
        return {
            str(s.get("handle")).strip()
            for s in (inst.get("accessScopes") or [])
            if isinstance(s, dict) and s.get("handle")
        }

    @staticmethod
    def _has_scope(granted: set, scope: str) -> bool:
        return scope in granted or (
            scope.startswith("read_") and ("write_" + scope[len("read_") :]) in granted
        )

    def test(self) -> tuple[bool, str]:
        try:
            locs = self._fetch_locales()
            pub = sum(1 for x in locs if x.get("published"))
            base_msg = (
                f"Connected to Shopify (store {self._store()}, {len(locs)} "
                f"locale(s), {pub} published)."
            )
            granted = self._granted_scopes()
            if granted is None:
                return True, (
                    base_msg + " Could not read the app's granted access scopes, so write "
                    "access is unchecked."
                )
            missing = [s for s in self._REQUIRED_SCOPES if not self._has_scope(granted, s)]
            if missing:
                return False, (
                    f"Connected to Shopify (store {self._store()}), but the app is missing "
                    f"access scope(s): {', '.join(missing)}. Add them to the app's "
                    "configuration, release a new app version, and approve the update on "
                    "the store."
                )
            warn: list[str] = []
            for t in self._resource_types():
                opts = self._RESOURCE_SCOPES.get(t, ())
                if opts and not any(self._has_scope(granted, s) for s in opts):
                    warn.append(f"{opts[0]} (for {t})")
            if warn:
                return True, (
                    base_msg
                    + " Warning: the app may be missing read scope(s): "
                    + ", ".join(warn)
                    + "."
                )
            return True, base_msg
        except ConnectorError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def containers(self) -> list[dict]:
        try:
            return [
                {"id": t, "name": f"{t} (resource type)", "tables": []}
                for t in self._resource_types()
            ]
        except Exception:
            return []

    # ------------------------------------------------------- GraphQL fragments
    _CONTENT_FIELDS = "translatableContent { key value digest type locale }"

    def _resource_query(self) -> str:
        return (
            "query($id: ID!, $locale: String!) {"
            "  translatableResource(resourceId: $id) {"
            "    resourceId "
            + self._CONTENT_FIELDS
            + "    translations(locale: $locale) { key value locale }"
            "  } }"
        )

    def _list_query(self) -> str:
        return (
            "query($type: TranslatableResourceType!, $first: Int!, $after: String, "
            "$reverse: Boolean) {"
            "  translatableResources(resourceType: $type, first: $first, after: $after, "
            "reverse: $reverse) {"
            "    edges { cursor node { resourceId " + self._CONTENT_FIELDS + " } }"
            "    pageInfo { hasNextPage endCursor }"
            "  } }"
        )

    _REGISTER_MUTATION = (
        "mutation($id: ID!, $translations: [TranslationInput!]!) {"
        "  translationsRegister(resourceId: $id, translations: $translations) {"
        "    userErrors { field message code }"
        "    translations { key value locale }"
        "  } }"
    )

    # ------------------------------------------------------- record normalize
    @staticmethod
    def _content_map(node: dict) -> dict:
        """{key: {"value","digest","type"}} from a resource node's translatableContent."""
        out: dict = {}
        for c in (node or {}).get("translatableContent") or []:
            k = c.get("key")
            if k:
                out[str(k)] = {
                    "value": c.get("value") or "",
                    "digest": c.get("digest"),
                    "type": str(c.get("type") or ""),
                }
        return out

    def _record(self, node: dict, resource_type: str) -> dict:
        content = self._content_map(node)
        title = str(
            (content.get(self._title_key(list(content.keys())) or "") or {}).get("value") or ""
        )
        return {
            "resource_id": str(node.get("resourceId") or ""),
            "_rt": resource_type,
            "content": content,
            "title": title,
        }

    # --------------------------------------------------------------- discovery
    def _paginate(self, resource_type: str, cap: int) -> list[dict]:
        """Walk the cursor up to `cap` resources, NEWEST FIRST (#654). Shopify lists oldest
        first by default, so a capped walk (default 50) in a larger store only ever saw the
        oldest resources and a newly added product was never discovered. `reverse: true`
        (a documented translatableResources argument) puts the newest first; an operator
        reaches older resources by raising max_entries."""
        out: list[dict] = []
        after = None
        page = min(100, max(1, cap))
        while len(out) < cap:
            data = self._graphql(
                self._list_query(),
                {"type": resource_type, "first": page, "after": after, "reverse": True},
            )
            block = data.get("translatableResources") or {}
            for edge in block.get("edges") or []:
                node = edge.get("node") or {}
                if node.get("resourceId") and self._keep_rows([node]):
                    out.append(self._record(node, resource_type))
            info = block.get("pageInfo") or {}
            if not info.get("hasNextPage") or not (block.get("edges") or []):
                break
            after = info.get("endCursor")
        return out[:cap]

    def discover_articles(
        self, cap: int | None = None, article_ids: list | None = None
    ) -> list[dict]:
        """Source resources to translate. Explicit article_ids (Shopify GIDs) win and are
        SCOPE-VALIDATED (well-formed GID whose type is configured; taxonomy A). Else each
        configured resource type is listed and the shared cap is filled ROUND-ROBIN across
        types so the first type can never starve the others (taxonomy E)."""
        cap = fetch_cap(cap or self.config.get("max_entries"), 50)
        types = self._resource_types()
        if article_ids:
            out: list[dict] = []
            src_loc = self._source_locale()
            for raw in list(article_ids)[:cap]:
                gid = self._validate_gid(str(raw), types)
                node = self._fetch_resource(gid, src_loc)
                if node is not None:
                    # The listed resource type is inferred from the GID for reporting.
                    m = _GID_RE.match(gid)
                    rt = _GID_TYPE_TO_ENUM.get(m.group(1)) if m else (types[0] if types else "")
                    out.append(
                        self._charge_record(self._record(node, rt or (types[0] if types else "")))
                    )
            return out
        per_type = [self._paginate(t, cap) for t in types]
        records: list[dict] = []
        i = 0
        while len(records) < cap and any(i < len(lst) for lst in per_type):
            for lst in per_type:
                if i < len(lst):
                    records.append(lst[i])
                    if len(records) >= cap:
                        break
            i += 1
        return records

    def _fetch_resource(self, gid: str, locale: str) -> dict | None:
        data = self._graphql(self._resource_query(), {"id": gid, "locale": locale})
        node = data.get("translatableResource")
        return node if isinstance(node, dict) and node.get("resourceId") else None

    # ------------------------------------------------------------ selection
    def _title_key(self, keys: list[str]) -> str | None:
        cfg = (self.config.get("title_field") or "").strip()
        if cfg and cfg in keys:
            return cfg
        for cand in ("title", "name"):
            if cand in keys:
                return cand
        return None

    def _field_selection(self, content: dict) -> tuple[list[str], dict]:
        """(translatable_keys, {skipped_key: reason}). Server-driven: Shopify's key list
        IS the allowlist. AUTO mode additionally gates on the content TYPE (translate
        STRING/HTML; skip machine types + report) and skips the URL `handle` by name; an
        operator `fields` allowlist overrides the type/name heuristic but still only
        selects keys Shopify marks translatable."""
        allow = _parse_names(self.config.get("fields"))
        exclude = set(_parse_names(self.config.get("exclude_fields")))
        selected: list[str] = []
        skipped: dict = {}
        names = allow if allow else list(content.keys())
        for key in names:
            info = content.get(key)
            if info is None:
                skipped[key] = "not a translatable key on this resource"
                continue
            if not allow and key in exclude:
                skipped[key] = "operator exclude_fields"
                continue
            ctype = str(info.get("type") or "").upper()
            if ctype not in _TRANSLATABLE_CONTENT_TYPES:
                skipped[key] = f"content type {ctype or '(unknown)'} is not translatable text"
                continue
            val = info.get("value")
            if not (isinstance(val, str) and val.strip()):
                skipped[key] = "no non-empty source text on this resource"
                continue
            if not allow and key in _NAME_SKIP_EXACT:
                skipped[key] = f"key looks non-translatable ({key})"
                continue
            selected.append(key)
        return selected, skipped

    def _record_selection(self, selected, skipped) -> None:
        self._sel_selected.update(selected)
        for n, r in skipped.items():
            self._sel_skipped.setdefault(n, r)
            if "not translatable text" in r and n not in self._sel_warned:
                self._sel_warned.add(n)
                log.warning(
                    "shopify: key %r skipped - %s. It is NOT being translated; "
                    "add it to the `fields` allowlist only if it holds plain text.",
                    n,
                    r,
                )

    def _translatable_keys(self, content: dict) -> list[str]:
        selected, skipped = self._field_selection(content)
        self._record_selection(selected, skipped)
        return selected

    def _selection_summary(self) -> str:
        if not self._sel_selected and not self._sel_skipped:
            return ""
        parts = [f"Translatable key(s): {', '.join(sorted(self._sel_selected)) or '(none)'}."]
        if self._sel_skipped:

            def _rank(item):
                _n, r = item
                loud = "not translatable text" in r
                return (0 if loud else 1, _n)

            items = sorted(self._sel_skipped.items(), key=_rank)
            shown = "; ".join(f"{n} ({r})" for n, r in items[:8])
            more = "" if len(items) <= 8 else f"; +{len(items) - 8} more"
            parts.append(f"Skipped: {shown}{more}.")
        return " ".join(parts)

    def extract(self, record: dict):
        content = record.get("content") or {}
        keys = self._translatable_keys(content)
        title_key = self._title_key(keys)
        title_text = (
            str((content.get(title_key or "") or {}).get("value") or "") if title_key else ""
        )
        body_keys = [k for k in keys if k != title_key]
        body_units = [_Unit(str((content.get(k) or {}).get("value") or ""), k) for k in body_keys]
        chash = _content_hash(
            *([title_text] + [str((content.get(k) or {}).get("value") or "") for k in body_keys])
        )
        return title_key, title_text, body_units, chash, body_keys

    # ----------------------------------------- client the T2 strategy drives
    def source_content(self, resource_id: str):
        """{key: {"value","digest","type"}} for the resource's CURRENT source content, or
        strategy.ABSENT when the resource no longer exists. Re-read fresh before every
        write (gotcha #2). Any locale is fine here (the source content + digest are
        locale-independent); we pass the source locale."""
        gid = self._validate_gid(resource_id)
        node = self._fetch_resource(gid, self._source_locale())
        if node is None:
            return ABSENT
        return self._content_map(node)

    def _overlay_keys(self, node: dict) -> set:
        return {
            str(t.get("key"))
            for t in (node.get("translations") or [])
            if t.get("key") and (t.get("value") is not None)
        }

    def existing_translation_keys(self, resource_id: str, exact_locale: str) -> set:
        """The set of keys already REGISTERED for (resource, locale) — read from Shopify's
        server-computed `translations(locale:)` overlay (an explicit existence signal,
        never a content read). Used for the created-vs-updated distinction. Empty = nothing
        registered for that locale yet."""
        gid = self._validate_gid(resource_id)
        data = self._graphql(self._resource_query(), {"id": gid, "locale": exact_locale})
        node = data.get("translatableResource")
        return self._overlay_keys(node) if isinstance(node, dict) else set()

    def fully_covered(self, resource_id: str, exact_locale: str) -> bool:
        """FULL-coverage suppression probe (MEDIUM-3): True iff there IS a persisted
        registered-key set for this (resource, locale) AND every key in it is still present
        in the live overlay. Keyed on the persisted `shopify_registered` set (the keys
        Shopify accepted on a prior run) rather than the SELECTED set, so:
          * a HELD/poison-dropped key we never registered does NOT force a re-attempt (no
            re-spend loop), yet
          * a key we DID register that was later deleted in Shopify surfaces for restore.
        UNTRACKED ⇒ NOT covered (H1): there is deliberately NO any-key fallback. The
        registered-set is persisted per successful pair via flow.on_write_ok BEFORE the
        end-of-run hash merge, so a crash cannot leave hash-without-tracking; and a pair
        whose hash predates this build simply re-registers ONCE (re-establishing tracking)
        instead of being sealed 'covered' forever by a stale-overlay fallback."""
        gid = self._validate_gid(resource_id)
        map_key = f"{resource_id}:{str(exact_locale).lower()}"
        tracked = set((self.config.get("shopify_registered") or {}).get(map_key) or [])
        if not tracked:
            return False  # untracked -> not covered; re-register re-establishes it
        data = self._graphql(self._resource_query(), {"id": gid, "locale": exact_locale})
        node = data.get("translatableResource")
        if not isinstance(node, dict):
            return False
        return tracked <= self._overlay_keys(node)

    @staticmethod
    def _attribute_bad_keys(errs, inputs) -> set:
        """Map each userError to the input KEY it blames — CONSERVATIVELY (M1). Only the
        index token IMMEDIATELY FOLLOWING a 'translations' token in the error's `field`
        LIST counts (e.g. ['translations','2','value'] -> inputs[2]['key']); at most one
        index per error. Anything else — a `field` that isn't a list (a plain string is
        NOT iterated char-wise), a numeric token in a non-`translations` path
        (['input','0','id']), or extra numeric tokens — contributes NOTHING, so the error
        is unattributable and the whole pair fails safe rather than dropping the wrong key.
        A key is dropped ONLY when Shopify explicitly points a translations-index at it."""
        bad: set = set()
        n = len(inputs)
        for e in errs or []:
            field = (e or {}).get("field")
            if not isinstance(field, list):
                continue  # a string/None field is unattributable (never iterate a str)
            idx = None
            for i, tok in enumerate(field):
                if tok == "translations" and i + 1 < len(field):
                    nxt = field[i + 1]
                    if isinstance(nxt, bool):
                        pass
                    elif isinstance(nxt, int):
                        idx = nxt
                    elif isinstance(nxt, str) and nxt.isdigit():
                        idx = int(nxt)
                    break  # only the FIRST 'translations' token; one index per error
            if idx is not None and 0 <= idx < n:
                k = inputs[idx].get("key")
                if k:
                    bad.add(k)
        return bad

    def register_translations(
        self, resource_id: str, exact_locale: str, inputs: list[dict]
    ) -> _RegisterOutcome:
        """translationsRegister the given [{locale,key,value,translatableContentDigest}]
        inputs. Classifies userErrors: a _DIGEST_RACE_CODES code latches invalid_digest (the
        strategy re-reads a fresh digest + retries); any other userError attributes its
        offending key(s) into bad_keys (MEDIUM-1 — the strategy drops just those and
        registers the rest); an unattributable error yields a whole-pair failure."""
        gid = self._validate_gid(resource_id)
        try:
            data = self._graphql(self._REGISTER_MUTATION, {"id": gid, "translations": inputs})
        except ConnectorError as e:
            return _RegisterOutcome(False, message=str(e))
        block = data.get("translationsRegister") or {}
        errs = block.get("userErrors") or []
        if not errs:
            return _RegisterOutcome(True)
        invalid = any(str((e or {}).get("code") or "").upper() in _DIGEST_RACE_CODES for e in errs)
        bad = self._attribute_bad_keys(errs, inputs) if not invalid else set()
        msg = vendor_quote("; ".join(str((e or {}).get("message") or e) for e in errs))
        return _RegisterOutcome(
            False,
            invalid_digest=invalid,
            message=msg or "translationsRegister failed",
            bad_keys=bad,
        )

    # ------------------------------------------------- generic Connector API
    def pull(self) -> list[PulledItem]:
        out: list[PulledItem] = []
        for rec in self.discover_articles():
            content = rec.get("content") or {}
            keys = self._translatable_keys(content)
            rid = rec["resource_id"]
            title_key = self._title_key(keys)
            label = str((content.get(title_key or "") or {}).get("value") or rid)[:60]
            for key in keys:
                out.append(
                    PulledItem(
                        external_id=rid,
                        field=key,
                        text=str((content.get(key) or {}).get("value") or ""),
                        title=label,
                    )
                )
        return out

    def push(self, items: list[PulledItem]) -> tuple[int, str]:
        raise ConnectorError("Shopify registers per-locale translations - use 'Translate now'.")

    def detect_change(self, conn=None):
        return []
