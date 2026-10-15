"""Shared SSRF policy for connectors, vendor HTTP requests and web search.

Canonicalizes numeric hosts, checks every DNS answer, and permits host-managed
private exceptions. The fixed ALWAYS_BLOCKED ranges never admit exceptions.
A guarded transport is the single validation seam: it enforces the same policy on
every sent request and redirect hop while preserving httpx's environment-proxy support.
Local engines use a separate fixed deny set; the MockEgressBlocked MT guard is unchanged.

Documented in: docs/architecture/secrets-and-crypto.md
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpcore
import httpx
import idna
from httpx._utils import get_environment_proxies

log = logging.getLogger("loc_tms.egress")


class EgressBlocked(Exception):
    """A target host this server must never egress to (bad scheme, private/loopback/
    metadata literal, internal name, or a name that resolves to a non-public address).
    ``connectors/base.py`` re-raises this as ``ConnectorError`` at its wrapper so every
    existing connector/vendor call site is byte-identical."""


#: The CGNAT / carrier-grade-NAT shared range. `ipaddress.is_private` does NOT flag
#: 100.64.0.0/10 (is_private=False, is_global=False), so it is checked explicitly — a box
#: behind a carrier NAT can reach neighbours in this range that are not "public".
_CGNAT4 = ipaddress.ip_network("100.64.0.0/10")

#: Hostname conventions that never name a public host (RFC 6762 `.local` mDNS, the cloud
#: `.internal` convention — `metadata.google.internal` is the concrete GCP metadata attack).
#: Matched as a case-insensitive suffix. `localhost` is matched exactly (also caught by the
#: IP path once resolved, but blocked by name so no resolver is even needed).
_INTERNAL_SUFFIXES = (".internal", ".local", ".localhost")

#: Injectable DNS resolver seam for the resolve-and-check SSRF step. `None` (the DEFAULT,
#: and what the fully-offline verify suites run with) means resolution is SKIPPED — the
#: numeric-canonicalization + name-denylist checks below still run unconditionally, so the
#: dangerous literal/localhost bypasses are closed even with no resolver. PRODUCTION installs
#: the real getaddrinfo-backed resolver at app startup (main._startup -> install_default_resolver)
#: so a hostname that RESOLVES to a private/metadata IP is refused before any byte egresses.
#: A test installs a fake `host -> iterable[str]` via set_test_resolver to exercise the
#: resolve-and-check path hermetically. Mirrors vendor_return.set_test_resolver.
_RESOLVER = None


def set_test_resolver(fn) -> None:
    """Install (or clear, with None) the SSRF-guard DNS resolver. `fn` is `host -> iterable[str]`
    of resolved IP strings. Named for the test seam it primarily serves; production uses
    install_default_resolver()."""
    global _RESOLVER
    _RESOLVER = fn


def _default_resolver(host: str) -> list[str]:
    """Every distinct A/AAAA address `host` resolves to, via the OS resolver. Production only —
    the offline suites keep _RESOLVER=None (resolution skipped) or inject a fake."""
    # Order-preserving de-dup: keep getaddrinfo's RFC 6724 ordering (a set would randomise
    # IPv4/IPv6 order, and the pinned dial walks the answers in this order).
    return list(dict.fromkeys(ai[4][0] for ai in socket.getaddrinfo(host, None)))


#: How long the engine CREATE/UPDATE door waits for DNS before giving up on a name.
#: `socket.getaddrinfo` takes no timeout and can sit for the OS resolver's own budget
#: (tens of seconds against a dead or hostile nameserver), and that door is reached from
#: `async def` /api routes, where a synchronous stall blocks the whole single-process
#: event loop rather than just the caller.
#:
#: OPT-IN (`validate_local_engine_target(..., deadline=...)`) and passed by that ONE
#: caller, `engines._WRITE_DEADLINE_S`. Giving up reads as "unresolvable", which this
#: door ALLOWS, so a bounded check FAILS OPEN and can be talked past by a resolver that
#: simply answers slowly. That is acceptable there and nowhere else: the worst case is a
#: bad target being SAVED -- exactly the pre-#144 state -- while
#: `providers_common.local_engine_client_kwargs` still runs this validator with no
#: deadline before a single byte leaves, so it can never be USED. Every other caller,
#: including the settings registry and the engine test probe, passes nothing and gets
#: the strict, unbounded, fail-closed check.
LOCAL_ENGINE_RESOLVE_DEADLINE_S = 2.0


def _resolve_within(resolve, host: str, seconds: float) -> list[str]:
    """`resolve(host)`, abandoned after `seconds`, reported as UNRESOLVABLE on timeout.

    Timing out lands where a resolver that RAISES has always landed (`addresses = []`),
    which this door allows -- so the bound FAILS OPEN and a resolver that merely answers
    slowly can be used to talk a name past a bounded check. That is why exactly one
    caller opts in; see LOCAL_ENGINE_RESOLVE_DEADLINE_S. Everywhere else the caller passes
    no deadline and this helper is not reached at all, so those checks are exactly as
    strict as they were.

    The abandoned thread is a daemon and is left to finish on its own: there is no way to
    cancel a `getaddrinfo` in flight, and holding the request to wait for one is the very
    stall this exists to bound. With one opt-in caller the thread is created only when an
    operator saves an engine, never on a translation path.
    """
    answers: list[str] = []
    done = threading.Event()

    def run() -> None:
        try:
            answers.extend(str(a) for a in resolve(host))
        except Exception:
            log.debug("egress: resolver raised for %r; treating as unresolvable", host)
        finally:
            done.set()

    threading.Thread(target=run, name="egress-resolve", daemon=True).start()
    if not done.wait(seconds):
        log.warning(
            "egress: DNS for %r exceeded %.1fs; treating it as unresolvable for this check",
            host,
            seconds,
        )
        return []
    return list(answers)


def install_default_resolver() -> None:
    """Wire the real getaddrinfo-backed resolver so the SSRF guard resolves-and-checks in
    production. Called once at app startup (main._startup). Idempotent."""
    set_test_resolver(_default_resolver)


def _parse_inet_int(part: str) -> int | None:
    """One dotted component parsed with C strtoul(base=0) semantics (what glibc's inet_aton
    uses): `0x`/`0X` prefix -> hex, a leading `0` -> octal, otherwise decimal. Returns None
    for anything that is not a clean whole-number in its base (so a real hostname label like
    'com' or a malformed '08' falls through to the DNS-name path, exactly as inet_aton would
    fail on it)."""
    if not part:
        return None
    try:
        low = part.lower()
        if low.startswith("0x"):
            return int(part[2:], 16) if len(part) > 2 else None
        if part[0] == "0":
            return int(part, 8)  # '0' alone -> 0; '08' raises -> None (matches inet_aton)
        return int(part, 10)
    except ValueError:
        return None


def _inet_aton_ipv4(host: str):
    """Return the ipaddress.IPv4Address that glibc's inet_aton/getaddrinfo would resolve `host`
    to for the NUMERIC encodings a plain dotted-quad parse rejects — the real Linux attack
    surface — else None. Covers: a single integer (decimal `2130706433`, hex `0x7f000001`,
    octal `017700000001`); and 2-, 3-, or 4-part forms where the LAST part absorbs the remaining
    low bytes (`127.1`->127.0.0.1, `10.1`->10.0.0.1, `1.2.3`->1.2.0.3), each part itself
    decimal/hex/octal. A form glibc would turn into an IP is thus range-checked as that IP
    instead of being treated as an allowed hostname."""
    # Fast reject: only digits / hex letters / '.' / 'x' can be an inet_aton numeric form.
    if not host or any(c not in "0123456789abcdefABCDEFxX." for c in host):
        return None
    parts = host.split(".")
    if not (1 <= len(parts) <= 4):
        return None
    vals = [_parse_inet_int(p) for p in parts]
    if any(v is None for v in vals):
        return None
    n = len(vals)
    if n == 1:
        addr = vals[0]
        if addr > 0xFFFFFFFF:
            return None
    elif n == 2:
        a, b = vals
        if a > 0xFF or b > 0xFFFFFF:
            return None
        addr = (a << 24) | b
    elif n == 3:
        a, b, c = vals
        if a > 0xFF or b > 0xFF or c > 0xFFFF:
            return None
        addr = (a << 24) | (b << 16) | c
    else:  # n == 4
        a, b, c, d = vals
        if any(x > 0xFF for x in vals):
            return None
        addr = (a << 24) | (b << 16) | (c << 8) | d
    return ipaddress.IPv4Address(addr)


def _as_ip_literal(host: str):
    """The IP address `host` denotes as a LITERAL (no DNS): a normal IPv4/IPv6 literal, OR one
    of the alternate numeric IPv4 encodings glibc accepts (_inet_aton_ipv4). None -> not a
    literal, i.e. a DNS name."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return _inet_aton_ipv4(host)


def _is_blocked_ip(ip) -> bool:
    """True if `ip` requires refusal or a private exception: loopback /
    link-local (incl. 169.254.169.254 metadata) / private / multicast / reserved / unspecified,
    plus the CGNAT range is_private misses. An IPv4-mapped IPv6 (`::ffff:127.0.0.1`) is ALSO
    checked as its embedded IPv4 — belt-and-suspenders in case a runtime's is_* flags don't
    reflect the mapped address."""
    candidates = [ip]
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)
    for a in candidates:
        if (
            a.is_loopback
            or a.is_link_local
            or a.is_private
            or a.is_multicast
            or a.is_reserved
            or a.is_unspecified
            or (isinstance(a, ipaddress.IPv4Address) and a in _CGNAT4)
        ):
            return True
    return False


# Fixed, non-overridable destinations. IPv6 reserved space mirrors the guard's
# existing is_reserved policy; private ULA (fc00::/7) remains eligible.
ALWAYS_BLOCKED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/8",
        "100::/8",
        "200::/7",
        "400::/6",
        "800::/5",
        "1000::/4",
        "4000::/3",
        "6000::/3",
        "8000::/3",
        "a000::/3",
        "c000::/3",
        "e000::/4",
        "f000::/5",
        "f800::/6",
        "fe00::/9",
        "fe80::/10",
        "ff00::/8",
    )
)
LOCAL_ENGINE_BLOCKED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "fe80::/10",
        "ff00::/8",
        "fd00:ec2::254/128",
    )
)
LOCAL_ENGINE_V6_EMBEDS = tuple(ipaddress.ip_network(n) for n in ("::/8", "64:ff9b::/96"))


CALLER_IP = ContextVar("egress_caller_ip", default="")
CALLER_ACTOR = ContextVar("egress_caller_actor", default="system")
_ATTEMPTS = ContextVar("egress_attempts", default=None)


def _hostname(host):
    host = host.lower().rstrip(".")
    if host.isascii():
        # httpx never calls idna for an ASCII host (httpx._urlparse.encode_host quotes and
        # lowercases it), so an underscore, a leading '_' or a trailing '-' label is a legal
        # target here too. IDNA (with its STD3 label rules) runs only for a genuinely non-ASCII
        # host below, exactly as httpx does; do not impose a well-formedness gate the transport
        # itself would not. IP literals are ASCII and pass straight through unchanged.
        return host
    try:
        return idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError as exc:
        # Round 4 LOW-3: a distinct signal so each caller can phrase its own refusal. A malformed
        # non-ASCII TARGET must get the target-refusal wording (validate_target), not the allowlist
        # ENTRY grammar; parse_allowlist re-raises this as its own grammar sentence below.
        raise ValueError("hostname is not a valid IDN") from exc


def _always_name(host):
    return host in ("localhost", "metadata.google.internal") or host.endswith(".localhost")


def _always_ip(ip):
    return any(ip.version == n.version and ip in n for n in ALWAYS_BLOCKED)


def parse_allowlist(value):
    """Parse an entire host policy atomically; never accept a partially valid list."""
    entries = []
    if not isinstance(value, str):
        raise ValueError("expected comma-separated hostnames and CIDRs")
    if not value.strip():
        return entries
    for raw in value.split(","):
        host = raw.strip().lower().rstrip(".")
        ip = _as_ip_literal(host)
        if "/" in host or ip is not None:
            try:
                net = ipaddress.ip_network(host if "/" in host else str(ip), strict=False)
            except ValueError as exc:
                raise ValueError(
                    "expected exact hostnames or IPv4/IPv6 CIDRs; no URLs or ports"
                ) from exc
            if net.prefixlen < (8 if net.version == 4 else 32):
                raise ValueError("minimum prefix is /8 IPv4 or /32 IPv6")
            if any(net.version == n.version and net.overlaps(n) for n in ALWAYS_BLOCKED):
                raise ValueError("entry covers an always-blocked address")
            entries.append((raw.strip(), net))
        else:
            try:
                host = _hostname(host)
            except ValueError as exc:
                # Round 4 LOW-3: keep the ENTRY-validator grammar for a malformed non-ASCII entry
                # (the target side gets its own IDN wording in validate_target instead).
                raise ValueError(
                    "expected exact hostnames or IPv4/IPv6 CIDRs; no URLs, ports or wildcards"
                ) from exc
            if (
                _always_name(host)
                or len(host) > 253
                or not all(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in host.split(".")
                )
            ):
                # Names the accepted forms (LOW-2): a host:port, an empty entry or a wildcard/
                # blocked name now reaches this branch rather than idna (which only runs for a
                # non-ASCII host after MED-1), so the message must still guide the operator.
                raise ValueError(
                    "expected exact hostnames or IPv4/IPv6 CIDRs; "
                    "no URLs, ports, wildcards or blocked names"
                )
            entries.append((raw.strip(), host))
    return entries


#: Warn once per outage episode; a successful store read re-arms the warning.
#: Suppress flapping for 60 seconds between warnings, using monotonic time.
#: A successful read since the last warning is also required to start a new episode.
#: This lock protects bookkeeping only, never a settings session or write.
_ALLOWLIST_STORE_WARNING_COOLDOWN = 60.0
_ALLOWLIST_STORE_LOCK = threading.Lock()
_ALLOWLIST_STORE_WARNED = False
_ALLOWLIST_STORE_LAST_WARNING = float("-inf")


def _allowlist_entries():
    """Return (entries, store_ok), distinguishing an outage from an empty policy.

    With the store down, nothing is approved that the configured install would
    refuse. The pre-P4 posture is the floor, and P4's exact-name refusal is kept:
    an unknown allowlist cannot approve a name without resolved addresses.
    A successful read re-arms the warning subject to the monotonic cooldown.
    Parse errors on a present value still surface; only the settings read is guarded.
    """
    from app.services import settings

    global _ALLOWLIST_STORE_WARNED, _ALLOWLIST_STORE_LAST_WARNING
    try:
        raw = settings.get("egress_private_allowlist") or ""
    except Exception:
        emit_warning = False
        with _ALLOWLIST_STORE_LOCK:
            now = time.monotonic()
            if (
                not _ALLOWLIST_STORE_WARNED
                and now - _ALLOWLIST_STORE_LAST_WARNING > _ALLOWLIST_STORE_WARNING_COOLDOWN
            ):
                _ALLOWLIST_STORE_WARNED = True
                _ALLOWLIST_STORE_LAST_WARNING = now
                emit_warning = True
        if emit_warning:
            log.warning(
                "egress_private_allowlist unavailable (settings store not reachable); "
                "private exceptions and unresolved names refused (fail closed)",
                exc_info=True,
            )
        return [], False
    with _ALLOWLIST_STORE_LOCK:
        _ALLOWLIST_STORE_WARNED = False
    return parse_allowlist(raw), True


def validate_target(
    base_url: str, *, subsystem: str = "egress", resolver=None, _exceptions=None
) -> str:
    """Check every address and each HTTP hop. Private exceptions never override the
    fixed deny set. GuardedTransport pins a name to its validated answers before the
    connect and re-checks the connected peer before sending bytes.
    The offline resolver seam retains public-name preflight behavior.
    """
    raw = (base_url or "").strip()
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise EgressBlocked(
            f"Blocked target: scheme {scheme or '(none)'!r} not allowed (use http/https)."
        )
    try:
        host = _hostname(parts.hostname or "")
    except ValueError as exc:
        # Round 4 LOW-3: a malformed non-ASCII target names the target, not the allowlist grammar.
        raise EgressBlocked(f"Blocked target: {exc}.") from exc
    if not host:
        raise EgressBlocked("Blocked target: no host in URL.")
    internal = host.endswith(_INTERNAL_SUFFIXES)
    if _always_name(host):
        raise EgressBlocked(f"Blocked target: {host!r} is a private/internal name.")
    entries, store_ok = _allowlist_entries()
    exact = next((raw for raw, entry in entries if isinstance(entry, str) and entry == host), None)
    ip = _as_ip_literal(host)
    resolve = resolver or _RESOLVER
    if ip is not None:
        addresses = [str(ip)]
    elif resolve is not None:
        try:
            addresses = list(resolve(host))
        except Exception:
            addresses = []
    else:
        addresses = []
    if not addresses:
        if not store_ok:
            raise EgressBlocked(
                "Blocked target: settings store unavailable; resolution required during outage."
            )
        if internal or exact:
            raise EgressBlocked(
                f"Blocked target: {host!r} is a private/internal name. "
                "not on egress_private_allowlist (resolution required)."
            )
        return raw
    exceptions = []
    for address in addresses:
        try:
            rip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise EgressBlocked(
                f"Blocked target: {host} resolved to an unparseable address {address!r}."
            ) from exc
        if ip is not None:
            message = f"Blocked target: {host} is not a public address."
        elif internal:
            message = f"Blocked target: {host!r} is a private/internal name."
        else:
            message = f"Blocked target: {host} resolves to non-public address {address}."
        if _always_ip(rip):
            mapped = getattr(rip, "ipv4_mapped", None)
            if mapped is not None and not _always_ip(mapped):
                # LOW-4: an IPv4-mapped IPv6 answer lands in the reserved ::/8 always-blocked set,
                # so it can never be allowlisted even when its embedded IPv4 is in an allowlisted
                # range. Say so, and point the operator at the fix rather than a bare block.
                # Round 4 LOW-2: only when the embedded IPv4 could ITSELF be allowlisted. If the
                # embedded address is always-blocked (127.0.0.1, 169.254.169.254, 0.0.0.0), the
                # hint's advice ("use a native IPv4 DNS answer") is impossible to follow, so refuse
                # plainly instead.
                raise EgressBlocked(
                    message + " IPv4-mapped IPv6 addresses cannot be allowlisted; use a native "
                    "IPv4 DNS answer for an egress_private_allowlist exception."
                )
            raise EgressBlocked(message)
        if _is_blocked_ip(rip) or internal:
            match = exact or next(
                (
                    raw
                    for raw, entry in entries
                    if not isinstance(entry, str) and entry.version == rip.version and rip in entry
                ),
                None,
            )
            if not match:
                raise EgressBlocked(message + " not on egress_private_allowlist")
            exceptions.append((str(rip), match))
    if _exceptions is not None:
        _exceptions.extend((host, address, match, subsystem) for address, match in exceptions)
    return raw


def validate_local_engine_target(
    base_url: str,
    *,
    subsystem: str = "local_engine",
    resolver=None,
    deadline: float | None = None,
    _exceptions=None,
) -> str:
    """Fixed deny set for self-hosted engines, including their tailnet SOCKS path.

    Loopback, localhost/.local, RFC1918, ULA fc00::/7 and CGNAT 100.64.0.0/10
    are allowed because these are intended local model destinations. No private
    allowlist applies. Accepted overlap: Alibaba metadata 100.100.100.200 lies
    inside CGNAT; the tailnet sidecar wins (documented accepted risk).
    Unresolvable names are allowed so offline installs with LAN hostnames work;
    numeric and metadata-name checks still run. This is a preflight policy for the
    separate local-engine clients, not the GuardedTransport connection check.
    Refusals use the existing best-effort audit machinery, including preflights.

    `deadline` (seconds) bounds the DNS wait and has exactly ONE caller, the engine
    create/update door: giving up reads as unresolvable, which this door allows, so a
    bounded call can be talked past by a slow resolver. Every check that gates real
    egress passes none.
    """
    raw = (base_url or "").strip()
    if not raw:
        raise EgressBlocked("No base URL configured (set the server URL in Setup AI)")
    host = ""
    address = ""
    try:
        try:
            parts = urlsplit(raw)
            scheme = (parts.scheme or "").lower()
            if scheme not in ("http", "https"):
                raise EgressBlocked(
                    f"Blocked target: scheme {scheme or '(none)'!r} not allowed (use http/https)."
                )
            host = _hostname(parts.hostname or "")
        except ValueError as exc:
            raise EgressBlocked(f"Blocked target: {exc}.") from exc
        if not host:
            raise EgressBlocked("Blocked target: no host in URL.")
        if host in ("metadata", "metadata.goog", "metadata.google.internal"):
            raise EgressBlocked(f"Blocked target: {host!r} is a metadata name.")
        ip = _as_ip_literal(host)
        addresses = [ip] if ip is not None else []
        resolve = resolver or _RESOLVER
        if ip is None and resolve is not None:
            if deadline is None:
                # THE DEFAULT, and every path that decides whether bytes may leave: wait
                # as long as the OS resolver takes, then check what it answered.
                # Unbounded on purpose -- a bound here lets a slow resolver talk a denied
                # target through (gate MED-1).
                try:
                    addresses = list(resolve(host))
                except Exception:
                    addresses = []
            else:
                # The one opt-in caller, the engine create/update door (review round 1
                # HIGH): it runs from an async route, where an unbounded getaddrinfo
                # stalls the event loop. Giving up reads as unresolvable, so at worst a
                # bad target is SAVED and then refused at use by the unbounded check.
                addresses = _resolve_within(resolve, host, deadline)
        for answer in addresses:
            address = str(answer)
            try:
                rip = ipaddress.ip_address(answer)
            except ValueError as exc:
                raise EgressBlocked(
                    f"Blocked target: {host} resolved to an invalid address."
                ) from exc
            candidates = [rip]
            mapped = getattr(rip, "ipv4_mapped", None)
            if mapped is not None:
                candidates.append(mapped)
            if (
                rip.version == 6
                and not rip.is_loopback
                and any(rip in network for network in LOCAL_ENGINE_V6_EMBEDS)
            ):
                candidates.append(ipaddress.IPv4Address(int(rip) & 0xFFFFFFFF))
            for candidate in candidates:
                for network in LOCAL_ENGINE_BLOCKED:
                    if candidate.version == network.version and candidate in network:
                        raise EgressBlocked(
                            f"Blocked target: {host} address {address} "
                            f"is in local-engine deny set {network}."
                        )
    except EgressBlocked:
        with audit_attempts():
            row = _ATTEMPTS.get().setdefault(
                (host, subsystem), {"attempt_count": 0, "approvals": set()}
            )
            row["attempt_count"] += 1
            row["approvals"].add((address, "blocked:local_engine"))
        raise
    return raw


@contextmanager
def audit_attempts():
    """One row per approved target per logical request, including retry count."""
    if _ATTEMPTS.get() is not None:
        yield
        return
    rows = {}
    token = _ATTEMPTS.set(rows)
    try:
        yield
    finally:
        # Round 5 MED-1 (corrects round 4 LOW-5): flush on BOTH outcomes (request completed,
        # request raised), exactly once per logical attempt, so a private egress whose transport
        # raises still records its egress.private_target row (the burst of refused internal hops an
        # operator most needs). The contextvar reset must run first. The flush uses a total guard
        # (LOW-1): a best-effort audit write can never replace or mask the caller's own result or
        # exception, and a BaseException in audit.log cannot lose a successful response. Only the
        # caller's own exception, if any, propagates out of the finally, never the audit's.
        _ATTEMPTS.reset(token)
        try:
            _flush_attempts(rows)
        except BaseException:
            log.warning("egress audit flush failed; egress unaffected", exc_info=True)


def _resolve_actor():
    """Resolve the audit actor best-effort. The middleware installs a callable that reads the
    request identity (a DB read that opens a session); if it raises, the row is still written
    under ``system`` and a warning is logged, rather than taking down the egress it describes."""
    actor = CALLER_ACTOR.get()
    if not callable(actor):
        return actor
    try:
        return actor()
    except Exception:
        log.warning("egress audit actor resolution failed; recording as system", exc_info=True)
        return "system"


def _flush_attempts(rows):
    """Write the accumulated ``egress.private_target`` rows. This is a best-effort description of
    an egress that already happened (MED-2). It is called from ``audit_attempts`` on BOTH outcomes
    (Round 5 MED-1) inside a total guard there, so a failure can never replace or mask the caller's
    real result or exception. Each write is also guarded here: a failure logs a WARNING and skips.
    Actor resolution is likewise guarded (see _resolve_actor). The guard covers the whole body,
    including the audit import, so no crumb outside the per-row try can take down the egress it
    merely describes; the per-row guard is kept so one bad row does not skip the rest."""
    try:
        from app.services import audit
    except Exception:
        log.warning("egress audit module unavailable; egress unaffected", exc_info=True)
        return

    for (host, subsystem), row in rows.items():
        try:
            approvals = sorted(row["approvals"])
            address, match = approvals[0]
            detail = {
                "hostname": host,
                "resolved_address": address,
                "matched_entry": match,
                "subsystem": subsystem,
                "attempt_count": row["attempt_count"],
            }
            if len(approvals) > 1:
                detail["resolved_targets"] = [
                    {"resolved_address": address, "matched_entry": match}
                    for address, match in approvals
                ]
            audit.log(
                actor=_resolve_actor(),
                action="egress.private_target",
                target=host,
                ip=CALLER_IP.get(),
                detail=json.dumps(detail),
            )
        except Exception:
            log.warning("egress audit write failed for %r; egress unaffected", host, exc_info=True)


#: Which validator a guarded transport / pin runs. "public" is the connector/vendor/search
#: policy (validate_target: private ranges refused unless allowlisted); "local_engine" is the
#: deliberately wider self-hosted-engine policy (validate_local_engine_target: loopback, LAN
#: and tailnet allowed, link-local/metadata refused). Both get the SAME resolve-once pin and
#: connected-peer check (#327/#354); only the per-address verdict differs.
POLICY_PUBLIC = "public"
POLICY_LOCAL_ENGINE = "local_engine"


def _check_target(policy, url, *, subsystem, resolver, _exceptions=None):
    """Run the validator for `policy` against `url`, resolving through `resolver`."""
    if policy == POLICY_LOCAL_ENGINE:
        validate_local_engine_target(url, subsystem=subsystem, resolver=resolver)
    elif policy == POLICY_PUBLIC:
        validate_target(url, subsystem=subsystem, resolver=resolver, _exceptions=_exceptions)
    else:
        raise ValueError(f"unknown egress policy {policy!r}")
    return url


class _PeerCheckedBackend:
    """Close the DNS check/connect race before httpcore can send TLS or HTTP bytes.

    A name is resolved ONCE before the dial, every answer is validated, and only the
    pinned validated addresses are dialed (#327): a rebinding answer at connect time can
    neither redirect the connection nor let an open internal port answer differently
    from a closed one. A name the resolver cannot answer is REFUSED before any dial,
    never dialed: a name dial would let a per-query rebind complete a handshake with an
    internal port and leak open/closed state. SNI and the certificate hostname come
    from the request, not the dial address, so TLS verification on the name is
    unchanged. The connected peer is still re-checked from getpeername() as defense
    in depth.
    Leave the origin untouched so Host, SNI and certificate name checks stay intact.
    Explicit proxies own destination resolution/policy; their socket is not the origin.
    """

    def __init__(self, inner, subsystem, error_type, resolver=None, policy=POLICY_PUBLIC):
        self.inner = inner
        self.subsystem = subsystem
        self.error_type = error_type
        self.resolver = resolver
        self.policy = policy

    def _pinned_connect(self, host, port, **kwargs):
        """Resolve-and-pin for a DNS name: validate every answer, then dial those
        validated addresses only. Same resolution order as the transport preflight
        (per-transport seam, then the global seam, then the OS resolver). A name the
        resolver raises on or answers with nothing raises httpcore.ConnectError here,
        before any dial: nothing could connect to it anyway, dialing the name would
        re-open the #327 rebind window, and it is a transport failure (retryable, not
        a policy block). Answers that resolve to blocked addresses keep the policy
        error."""
        resolve = self.resolver or _RESOLVER or _default_resolver
        try:
            answers = [str(a) for a in resolve(host)]
        except Exception as exc:
            # A failed lookup is a CONNECTION failure, not a policy block: still no dial
            # (nothing to connect to), but callers' retry/messaging treats it as a
            # transport error (httpcore.ConnectError -> httpx.ConnectError).
            raise httpcore.ConnectError(
                f"could not resolve {host} for the pre-connect check: {exc}"
            ) from exc
        if not answers:
            raise httpcore.ConnectError(f"{host} resolved to no address (pre-connect check)")
        try:
            _check_target(
                self.policy,
                str(httpx.URL(scheme="http", host=host, port=port)),
                subsystem=self.subsystem,
                resolver=lambda _: answers,
            )
        except EgressBlocked as exc:
            raise self.error_type(str(exc)) from exc
        last_error = None
        for answer in answers:
            try:
                return self.inner.connect_tcp(answer, port, **kwargs)
            except Exception as exc:
                last_error = exc
        raise last_error

    def connect_tcp(self, host, port, **kwargs):
        # IP literals were already validated at transport entry; only a NAME can
        # rebind between the check and the dial, so only names are pinned.
        if _as_ip_literal(_hostname(host)) is None:
            stream = self._pinned_connect(host, port, **kwargs)
        else:
            stream = self.inner.connect_tcp(host, port, **kwargs)
        try:
            peer = stream.get_extra_info("server_addr")
            if not peer:
                raise EgressBlocked("Blocked target: connected peer address unavailable.")
            try:
                address = ipaddress.ip_address(peer[0])
            except ValueError as exc:
                raise EgressBlocked("Blocked target: invalid connected peer address.") from exc
            literal = _as_ip_literal(_hostname(host))
            if literal is not None and literal != address:
                raise EgressBlocked("Blocked target: connected peer differs from target IP.")
            _check_target(
                self.policy,
                str(httpx.URL(scheme="http", host=host, port=port)),
                subsystem=self.subsystem,
                resolver=lambda _: [str(address)],
            )
        except BaseException as exc:
            stream.close()
            if isinstance(exc, EgressBlocked):
                raise self.error_type(str(exc)) from exc
            raise
        return stream

    def connect_unix_socket(self, *args, **kwargs):
        # An explicitly configured local socket has no DNS rebinding surface.
        return self.inner.connect_unix_socket(*args, **kwargs)

    def sleep(self, seconds):
        self.inner.sleep(seconds)


def _install_peer_backend(pool, subsystem, error_type, resolver, policy=POLICY_PUBLIC) -> None:
    """Install _PeerCheckedBackend on an httpcore pool, FAILING CLOSED if the private seam moved.

    httpx has no public backend injection API, so the guard relies on httpcore reading
    `pool._network_backend` when it creates connections. If a future httpcore renames
    that attribute the assignment would silently create a dead attribute and drop the
    guard to preflight-only (re-opening #233 and #327). So verify the seam exists before
    and that a connection the pool creates is actually handed OUR backend after; if not,
    refuse to build the client (RuntimeError).
    """
    if not hasattr(pool, "_network_backend"):
        raise RuntimeError(
            "egress guard cannot attach: the httpcore pool has no _network_backend seam "
            "(unsupported httpcore version); refusing to build an unguarded client"
        )
    backend = _PeerCheckedBackend(pool._network_backend, subsystem, error_type, resolver, policy)
    pool._network_backend = backend
    try:
        conn = pool.create_connection(httpcore.Origin(b"http", b"egress-seam-check.invalid", 80))
        seam_ok = getattr(conn, "_network_backend", None) is backend
    except Exception:
        seam_ok = False
    if not seam_ok:
        raise RuntimeError(
            "egress guard cannot attach: httpcore no longer hands the pool's "
            "_network_backend to new connections (unsupported httpcore version); "
            "refusing to build an unguarded client"
        )


class GuardedTransport(httpx.BaseTransport):
    """Validate at transport entry; preflights and rejected hops never record use.

    A transport invocation counts as an attempt even if it raises a connection
    error: the remote outcome may be unknown. No successful response is required.
    """

    def __init__(
        self,
        subsystem,
        transport=None,
        error_type=EgressBlocked,
        resolver=None,
        policy=POLICY_PUBLIC,
    ):
        self.inner = transport if transport is not None else httpx.HTTPTransport()
        self.subsystem = subsystem
        self.error_type = error_type
        self.resolver = resolver
        self.policy = policy
        if isinstance(self.inner, httpx.HTTPTransport):
            # httpx has no public backend injection API. As with _DeadlineBackend,
            # adapt the pool before any connection is created. Proxy pools must keep
            # their operator-selected route, including private corporate proxies.
            pool = self.inner._pool
            if not hasattr(pool, "_proxy_url"):
                _install_peer_backend(pool, subsystem, error_type, self.resolver, policy)

    def handle_request(self, request):
        exceptions = []
        try:
            _check_target(
                self.policy,
                str(request.url),
                subsystem=self.subsystem,
                resolver=self.resolver,
                _exceptions=exceptions,
            )
        except EgressBlocked as exc:
            raise self.error_type(str(exc)) from exc
        with audit_attempts():
            if exceptions:
                host = exceptions[0][0]
                row = _ATTEMPTS.get().setdefault(
                    (host, self.subsystem), {"attempt_count": 0, "approvals": set()}
                )
                row["attempt_count"] += 1
                row["approvals"].update((address, match) for _, address, match, _ in exceptions)
            return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


class _DeadlineStream:
    """Apply the remaining request budget to every socket operation, not just each body chunk."""

    def __init__(self, inner, remaining):
        self.inner = inner
        self.remaining = remaining

    def _timeout(self, timeout):
        budget = self.remaining()
        return min(timeout, budget) if timeout is not None else budget

    def read(self, max_bytes, timeout=None):
        return self.inner.read(max_bytes, timeout=self._timeout(timeout))

    def write(self, buffer, timeout=None):
        return self.inner.write(buffer, timeout=self._timeout(timeout))

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _DeadlineStream(
            self.inner.start_tls(ssl_context, server_hostname, self._timeout(timeout)),
            self.remaining,
        )

    def close(self):
        self.inner.close()

    def get_extra_info(self, info):
        return self.inner.get_extra_info(info)


class _DeadlineBackend:
    """httpcore backend adapter; the callback is context-local for pooled connections."""

    def __init__(self, inner, remaining):
        self.inner = inner
        self.remaining = remaining

    def _connect(self, method, *args, **kwargs):
        budget = self.remaining()
        timeout = kwargs.get("timeout")
        kwargs["timeout"] = min(timeout, budget) if timeout is not None else budget
        return _DeadlineStream(method(*args, **kwargs), self.remaining)

    def connect_tcp(self, *args, **kwargs):
        return self._connect(self.inner.connect_tcp, *args, **kwargs)

    def connect_unix_socket(self, *args, **kwargs):
        return self._connect(self.inner.connect_unix_socket, *args, **kwargs)

    def sleep(self, seconds):
        self.inner.sleep(min(seconds, self.remaining()))


def guarded_transport_kwargs(
    subsystem,
    *,
    transport=None,
    error_type=EgressBlocked,
    resolver=None,
    trust_env=True,
    remaining=None,
    policy=POLICY_PUBLIC,
    **transport_kwargs,
):
    """httpx.Client kwargs that run the SSRF guard on every hop as the SINGLE validation seam
    (LOW-1: no separate event hook, so a target is resolved and checked once, not twice) WHILE
    preserving httpx's environment-proxy support (HIGH-1) with byte-for-byte the mount map httpx
    would have built itself (MED-1).

    httpx.Client only reads HTTPS_PROXY / HTTP_PROXY / NO_PROXY when ``transport is None``
    (``allow_env_proxies = trust_env and transport is None``), so passing a bare GuardedTransport
    silently disabled a corporate proxy on every connector, vendor and web.search client. Here the
    guard wraps ONE base direct transport plus the same mounts httpx computes for a None transport
    (via ``get_environment_proxies``): a proxy pattern maps to a guarded proxy ``HTTPTransport``
    and a ``NO_PROXY`` pattern maps to ``None`` exactly as httpx does. That ``None`` is not an
    unguarded hole: httpx ``_transport_for_url`` returns the client's base transport for a ``None``
    mount, and the base here is the guarded one, so a ``NO_PROXY`` host is still validated on every
    hop. Mapping it to ``None`` (rather than a fresh guarded ``HTTPTransport``) is what keeps the
    map equal to a plain ``httpx.Client(trust_env=True)`` and avoids building one extra TLS context
    and connection pool per ``NO_PROXY`` entry on every client a proxy install constructs.

    Transport-affecting client kwargs (``verify``, ``cert``, ``limits``, ``http1``, ``http2`` and
    any future one) are passed as ``**transport_kwargs`` and built into the base AND every proxy
    transport, the way ``httpx.Client`` builds them into the transport it makes itself (LOW-4);
    passing them to ``httpx.Client`` alongside our explicit transport would silently drop them.
    ``trust_env`` both selects env proxies and, like httpx, feeds the transport (SSL cert env vars,
    netrc).

    When an explicit inner ``transport`` is passed (a test double / MockTransport), no env-proxy
    mounts are built, so tests keep their fixed route. If transport-construction kwargs accompany it
    they build a fresh transport the same way the implicit path does (LOW-2), rather than being
    silently dropped on the early return; an explicit transport with no such kwargs is guarded
    verbatim.
    """

    def guard(inner):
        if remaining is not None and isinstance(inner, httpx.HTTPTransport):
            # httpx exposes no public network-backend injection. Keep this adapter
            # at the same seam that builds its direct and environment-proxy pools.
            pool = inner._pool
            pool._network_backend = _DeadlineBackend(pool._network_backend, remaining)
        return GuardedTransport(
            subsystem, transport=inner, error_type=error_type, resolver=resolver, policy=policy
        )

    if transport is not None:
        # LOW-2: an explicit transport carrying transport-construction kwargs (verify, cert,
        # limits, http1, http2 and any future one) builds them into the wrapped transport the same
        # way the implicit path builds them into the transport it makes itself, rather than silently
        # dropping them. An explicit transport with no such kwargs (a test double) is guarded as-is.
        if transport_kwargs:
            base_kwargs = {"trust_env": trust_env, **transport_kwargs}
            return {"transport": guard(httpx.HTTPTransport(**base_kwargs))}
        return {"transport": guard(transport)}
    base_kwargs = {"trust_env": trust_env, **transport_kwargs}
    mounts = {}
    if trust_env:
        for pattern, proxy_url in get_environment_proxies().items():
            mounts[pattern] = (
                None
                if proxy_url is None
                else guard(httpx.HTTPTransport(proxy=proxy_url, **base_kwargs))
            )
    return {"transport": guard(httpx.HTTPTransport(**base_kwargs)), "mounts": mounts}
