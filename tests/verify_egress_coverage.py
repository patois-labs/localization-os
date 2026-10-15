"""Egress guard COVERAGE (#354): every outbound client in app/ is guarded or knowingly exempt.

Documented in: docs/architecture/secrets-and-crypto.md (Outbound client inventory)

Two halves, both offline (stub resolvers + one local listener, no real network):

  1. RATCHET. An AST scan of app/ finds every outbound client construction (httpx, urllib,
     requests, aiohttp, smtplib, raw sockets, boto3, the vendor SDK constructors, the OTLP
     exporter). A site is GUARDED when the function that builds it also builds the #327 guarded
     transport through the egress seam; every other site must be named in EXEMPT with a written
     reason. It fails when a new unguarded client appears, AND when EXEMPT names a client that no
     longer exists (fails-on-absence), AND when the docs inventory omits a scanned file.
  2. PER-CLIENT BEHAVIOUR for each operator- or user-influenced client that #354 routed through
     the guard (OIDC, qe_jev, the local-engine door and its probe/warm-up/listing
     clients): a rebinding name (public answer, then internal) is refused with NO dial, an
     internal literal is refused, an allowlisted private target (or, for local engines, the
     deliberate LAN/loopback allowance) still works and dials only the validated address, and an
     unresolvable name is a plain connection failure, never a dial.
"""

# ruff: noqa: E501  (the synthetic one-line sources in MUST_CATCH / MUST_PASS are long on purpose)
import ast
import contextlib
import os
import pathlib
import socket
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_egress_cov_")

import httpx
from httpcore._backends.sync import SyncStream

from app.db import init_db
from app.services import egress

init_db()  # the guard reads egress_private_allowlist from the settings store on every request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = "LOC_TMS_EGRESS_PRIVATE_ALLOWLIST"

# --------------------------------------------------------------------------------------------
# 1. RATCHET
# --------------------------------------------------------------------------------------------
#: Fully qualified call names (import aliases are resolved first) that open an outbound
#: connection or build the client that will.
CLIENT_CALLS = frozenset(
    {
        "httpx.Client",
        "httpx.AsyncClient",
        "httpx.HTTPTransport",
        "httpx.AsyncHTTPTransport",
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.patch",
        "httpx.delete",
        "httpx.head",
        "httpx.options",
        "httpx.request",
        "httpx.stream",
        # httpx2 (anthropic 1.x, mcp 2.x) is the same client family: scan it too.
        "httpx2.Client",
        "httpx2.AsyncClient",
        "httpx2.HTTPTransport",
        "httpx2.AsyncHTTPTransport",
        "httpx2.get",
        "httpx2.post",
        "httpx2.put",
        "httpx2.patch",
        "httpx2.delete",
        "httpx2.head",
        "httpx2.options",
        "httpx2.request",
        "httpx2.stream",
        "urllib.request.urlopen",
        "urllib.request.build_opener",
        "http.client.HTTPConnection",
        "http.client.HTTPSConnection",
        "urllib3.PoolManager",
        "urllib3.ProxyManager",
        "urllib3.HTTPConnectionPool",
        "urllib3.HTTPSConnectionPool",
        "urllib3.connection_from_url",
        "urllib3.request",
        "aiohttp.ClientSession",
        "aiohttp.request",
        "ldap3.Connection",
        "websockets.connect",
        "websockets.sync.client.connect",
        "websockets.asyncio.client.connect",
        "websockets.legacy.client.connect",
        "smtplib.SMTP",
        "smtplib.SMTP_SSL",
        "socket.create_connection",
        "socket.socket",
        "asyncio.open_connection",
        "boto3.client",
        "boto3.resource",
        "openai.OpenAI",
        "openai.AsyncOpenAI",
        "openai.AzureOpenAI",
        "anthropic.Anthropic",
        "anthropic.AsyncAnthropic",
        "google.genai.Client",
        "OTLPSpanExporter",
    }
)
#: Every call on the ``requests`` package counts, except these non-connecting submodules.
REQUESTS_SKIP = ("requests.exceptions.", "requests.adapters.", "requests.utils.")
#: Vendor SDK constructors: guarded only when handed the door's client via ``http_client``.
SDK_CALLS = frozenset(
    {
        "openai.OpenAI",
        "openai.AsyncOpenAI",
        "openai.AzureOpenAI",
        "anthropic.Anthropic",
        "anthropic.AsyncAnthropic",
        "google.genai.Client",
    }
)
#: Last name component of every call that builds the guarded transport (the egress seam).
GUARD_TAILS = frozenset(
    {
        "guarded_transport_kwargs",
        "GuardedTransport",
        "local_engine_client_kwargs",
        "local_engine_route_kwargs",
    }
)

#: Unguarded clients that are DELIBERATELY exempt: (file, call) -> (count, reason). The count is
#: exact, so adding a second unguarded client to an exempt file fails the ratchet too.
_VENDOR = (
    "fixed vendor endpoint: the SDK dials its own hard-coded vendor host with a host-env API key; "
    "no operator, org or user input chooses the target"
)
EXEMPT = {
    ("app/cli/_remote.py", "httpx.Client"): (
        3,
        "runs in the operator's own CLI process and dials the server URL that person typed on "
        "their own machine (and a signed storage URL that server returned); it is not a "
        "server-side fetch an org user can steer",
    ),
    ("app/cli/backup_common.py", "socket.create_connection"): (
        1,
        "CLI liveness probe of this machine's own configured HOST:PORT (a 0.5s TCP connect, no "
        "payload) to decide whether the server is running before a backup",
    ),
    ("app/services/connectors/oauth.py", "httpx.Client"): (
        1,
        "connector OAuth token/discovery calls go only to the fixed code-registered provider "
        "registry (Airtable, Atlassian); no setting or user input names the URL",
    ),
    ("app/services/egress.py", "httpx.HTTPTransport"): (
        4,
        "the guard seam itself: these are the inner transports that GuardedTransport and "
        "guarded_transport_kwargs wrap (the pin and peer check are installed on them)",
    ),
    ("app/services/enterprise_enablement.py", "urllib.request.urlopen"): (
        1,
        "admin-click OTLP reachability preflight of the host-env LOC_TMS_OTEL_ENDPOINT, the same "
        "endpoint the exporter itself dials; collectors are routinely loopback/private, which the "
        "guard would refuse. Residual: urllib follows redirects (see the inventory note)",
    ),
    ("app/services/image_localization.py", "anthropic.Anthropic"): (1, _VENDOR),
    ("app/services/image_localization.py", "google.genai.Client"): (1, _VENDOR),
    ("app/services/image_localization.py", "openai.OpenAI"): (1, _VENDOR),
    ("app/services/ldap_auth.py", "ldap3.Connection"): (
        1,
        "LDAP, not HTTP: the admin-set ldap_host is normally an internal directory (AD/OpenLDAP "
        "on the LAN), so the public policy cannot apply; ldap3 also dials and verifies the TLS "
        "name itself, so a pin to a pre-resolved address would break certificate checking. "
        "Hardening in place: TLS/StartTLS before any bind, no referral chasing",
    ),
    ("app/services/mailer.py", "smtplib.SMTP"): (
        1,
        "SMTP, not HTTP: an operator-chosen mail relay (smtp_host, admin-only) which is almost "
        "always a private/internal relay, so the public-policy guard cannot apply",
    ),
    ("app/services/ocr.py", "openai.OpenAI"): (1, _VENDOR),
    ("app/services/ocr.py", "anthropic.Anthropic"): (1, _VENDOR),
    ("app/services/ocr.py", "google.genai.Client"): (1, _VENDOR),
    ("app/services/oidc.py", "httpx.Client"): (
        1,
        "the documented plain-http LOOPBACK dev IdP (localhost/127.0.0.1/::1 literal only), which "
        "the guard refuses by design and only a local process can answer; every other OIDC target "
        "takes the guarded client in the same function",
    ),
    ("app/services/providers_common.py", "anthropic.Anthropic"): (1, _VENDOR),
    ("app/services/providers_common.py", "httpx2.Client"): (
        1,
        "test-only transport injection for the Anthropic model-list helper (anthropic 1.x speaks "
        "httpx2); production uses the SDK's own client to the fixed api.anthropic.com host",
    ),
    ("app/services/providers_hub.py", "anthropic.Anthropic"): (1, _VENDOR),
    ("app/services/providers_hub.py", "google.genai.Client"): (1, _VENDOR),
    ("app/services/providers_hub.py", "openai.OpenAI"): (1, _VENDOR),
    ("app/services/providers_part2.py", "openai.OpenAI"): (1, _VENDOR + " (model list)"),
    ("app/services/providers_part2.py", "httpx.Client"): (
        2,
        "fixed vendor endpoints: the OpenAI model-list helper (test transport only) and the "
        "Gemini model-list call to generativelanguage.googleapis.com with the host-env key",
    ),
}


def _tail(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _resolve(node, aliases):
    """The fully qualified dotted name of `node` through the file's import aliases, or None.

    Follows ``import httpx as hx``, ``from httpx import Client as C``, a plain re-binding
    (``C = httpx.Client``) and ``getattr(httpx, "get")``."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        base = _resolve(node.args[0], aliases)
        return f"{base}.{node.args[1].value}" if base else None
    return None


def _aliases(tree):
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    for _ in range(2):  # a re-binding of an alias: ``C = httpx.Client`` / ``C = hx.Client``
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                resolved = _resolve(node.value, aliases)
                if resolved:
                    aliases[node.targets[0].id] = resolved
    return aliases


def _client_name(call, aliases):
    """The canonical CLIENT_CALLS name this call resolves to, or None."""
    name = _resolve(call.func, aliases)
    if not name:
        return None
    if name in CLIENT_CALLS:
        return name
    if name.endswith(".OTLPSpanExporter"):
        return "OTLPSpanExporter"
    if name.startswith("requests.") and not name.startswith(REQUESTS_SKIP):
        return name
    return None


def _is_guard_call(node):
    return isinstance(node, ast.Call) and _tail(node.func) in GUARD_TAILS


def _own_nodes(scope):
    """Every node of `scope`, not descending into nested functions."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _defs_of(name, chain):
    """[(kind, value_expr)] for every statement in the scope chain that sets `name`: 'plain'
    for ``name = v``, 'mut' for ``name[k] = v`` / ``name.attr = v`` / ``name.update(v)``."""
    found = []

    def base_is(target):
        while isinstance(target, (ast.Subscript, ast.Attribute)):
            target = target.value
        return isinstance(target, ast.Name) and target.id == name

    for scope in chain:
        for node in _own_nodes(scope):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        found.append(("plain", node.value))
                    elif base_is(t):
                        found.append(("mut", node.value))
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    found.append(("plain", node.value))
                elif base_is(node.target):
                    found.append(("mut", node.value))
            elif isinstance(node, ast.AugAssign) and base_is(node.target):
                found.append(("mut", node.value))
            elif isinstance(node, ast.NamedExpr) and node.target.id == name:
                found.append(("plain", node.value))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    v = item.optional_vars
                    if isinstance(v, ast.Name) and v.id == name:
                        found.append(("plain", item.context_expr))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("update", "setdefault")
                and base_is(node.func.value)
            ):
                found.append(("mut", node))
    return found


def _guarded_expr(expr, chain, seen=frozenset()):
    """True when `expr` carries the guard: it calls a guard builder, or reads a name that was
    set from one (so ``kw = guarded_transport_kwargs(..); httpx.Client(**kw)`` is guarded and
    ``kw = guarded_transport_kwargs(..); httpx.Client(base_url=b)`` is not)."""
    for node in ast.walk(expr):
        if _is_guard_call(node):
            return True
    for node in ast.walk(expr):
        if isinstance(node, ast.Name) and node.id not in seen:
            defs = _defs_of(node.id, chain)
            nxt = seen | {node.id}
            plain = [v for k, v in defs if k == "plain"]
            mut = [v for k, v in defs if k == "mut"]
            if (plain and all(_guarded_expr(v, chain, nxt) for v in plain)) or any(
                _guarded_expr(v, chain, nxt) for v in mut
            ):
                return True
    return False


def _scope_chain(parents, tree, node):
    chain = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chain.append(node)
    chain.append(tree)
    return chain


def scan(sources):
    """{path: source_text} -> ({(path, call): unguarded_count}, {path: guarded_site_count}).

    Import aliases are resolved first, so ``import httpx as hx``, ``from httpx import Client``,
    ``getattr(httpx, "get")`` and ``openai.OpenAI`` are all seen. A client site is GUARDED only
    when the guard flows into THAT call: a guard builder is called inside its own arguments, or
    one of its arguments (a ``**kwargs`` spread, ``transport=``, ``http_client=``, a positional)
    is a name set from a guard builder in the same function. A function that builds a guard but
    creates its client without it is therefore unguarded. A vendor SDK constructor needs the
    guard through its ``http_client`` (or a ``**kwargs`` spread), nothing else counts."""
    unguarded, guarded = {}, {}
    for path, text in sources.items():
        tree = ast.parse(text)
        aliases = _aliases(tree)
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = _client_name(call, aliases)
            if name is None:
                continue
            chain = _scope_chain(parents, tree, call)
            if name in SDK_CALLS:
                exprs = [k.value for k in call.keywords if k.arg in ("http_client", None)]
            else:
                exprs = list(call.args) + [k.value for k in call.keywords]
            if any(_guarded_expr(e, chain) for e in exprs):
                guarded[path] = guarded.get(path, 0) + 1
            else:
                unguarded[(path, name)] = unguarded.get((path, name), 0) + 1
    return unguarded, guarded


def _app_sources():
    return {
        p.relative_to(ROOT).as_posix(): p.read_bytes().decode("utf-8")
        for p in sorted((ROOT / "app").rglob("*.py"))
    }


def ratchet_problems(sources, exempt, docs_text):
    unguarded, guarded = scan(sources)
    problems = []
    for key, count in sorted(unguarded.items()):
        want = exempt.get(key)
        if want is None:
            problems.append(
                f"UNGUARDED outbound client with no exemption: {key[0]} {key[1]} x{count}"
            )
        elif want[0] != count:
            problems.append(
                f"{key[0]} {key[1]}: {count} unguarded client(s), EXEMPT says {want[0]}"
            )
    for key, (_count, reason) in sorted(exempt.items()):
        if key not in unguarded:
            problems.append(f"STALE exemption (client no longer exists or is now guarded): {key}")
        if len(reason.strip()) < 40:
            problems.append(f"exemption {key} has no real written reason")
    for path in sorted(set(guarded) | {k[0] for k in unguarded}):
        if path not in docs_text:
            problems.append(f"{path} is missing from the docs outbound client inventory")
    return problems


def test_ratchet_real_tree():
    docs = (ROOT / "docs/architecture/secrets-and-crypto.md").read_text(encoding="utf-8")
    assert "## Outbound client inventory" in docs
    section = docs.split("## Outbound client inventory", 1)[1]
    problems = ratchet_problems(_app_sources(), EXEMPT, section)
    assert not problems, "\n".join(problems)


#: Synthetic sources the scanner MUST report as unguarded (gate round 1 blind spots and more):
#: each is (label, source). They are the self-test of the ratchet itself.
_SEAM = "from app.services import egress\nfrom app.services.providers_common import local_engine_client_kwargs\n"
MUST_CATCH = [
    ("module-level client", "import httpx\nc = httpx.Client()\n"),
    (
        "guard built, client created without it (kwargs name unused)",
        _SEAM
        + "import httpx\ndef f(b):\n    kw = local_engine_client_kwargs(b)\n    return httpx.Client(base_url=b)\n",
    ),
    (
        "guard result discarded",
        _SEAM
        + "import httpx\ndef f():\n    _ = egress.guarded_transport_kwargs('x')\n    return httpx.Client()\n",
    ),
    (
        "guard built for a different name",
        _SEAM
        + "import httpx\ndef f():\n    kw = egress.guarded_transport_kwargs('x')\n    other = {}\n    return httpx.Client(**other)\n",
    ),
    (
        "name rebound to an unguarded value",
        _SEAM
        + "import httpx\ndef f():\n    kw = egress.guarded_transport_kwargs('x')\n    kw = {}\n    return httpx.Client(**kw)\n",
    ),
    ("from httpx import Client", "from httpx import Client\ndef f():\n    return Client()\n"),
    ("from httpx import AsyncClient", "from httpx import AsyncClient\nc = AsyncClient()\n"),
    ("import httpx as hx", "import httpx as hx\ndef f():\n    return hx.Client()\n"),
    ("client class re-bound", "import httpx\nC = httpx.Client\ndef f():\n    return C()\n"),
    (
        "getattr on the module",
        "import httpx\ndef f():\n    return getattr(httpx, 'get')('http://a')\n",
    ),
    ("httpx.HTTPTransport", "import httpx\nt = httpx.HTTPTransport()\n"),
    ("from requests import Session", "from requests import Session\ns = Session()\n"),
    (
        "from requests import get",
        "from requests import get\ndef f():\n    return get('http://a')\n",
    ),
    ("requests module call", "import requests\ndef f():\n    return requests.post('http://a')\n"),
    ("import requests as rq", "import requests as rq\ndef f():\n    return rq.get('http://a')\n"),
    ("from openai import OpenAI", "from openai import OpenAI\nc = OpenAI()\n"),
    (
        "openai.OpenAI with the module prefix",
        "import openai\ndef f():\n    return openai.OpenAI()\n",
    ),
    ("from anthropic import Anthropic", "from anthropic import Anthropic\nc = Anthropic()\n"),
    (
        "anthropic via getattr",
        "import anthropic\ndef f():\n    return getattr(anthropic, 'Anthropic')()\n",
    ),
    ("google genai client", "from google import genai\ndef f():\n    return genai.Client()\n"),
    (
        "SDK handed an unguarded http_client",
        "import httpx\nfrom openai import OpenAI\ndef f():\n    return OpenAI(http_client=httpx.Client())\n",
    ),
    (
        "SDK with a guard built but not passed",
        _SEAM
        + "from openai import OpenAI\ndef f(b):\n    kw = local_engine_client_kwargs(b)\n    return OpenAI(base_url=b)\n",
    ),
    ("urllib3 pool manager", "import urllib3\np = urllib3.PoolManager()\n"),
    ("urllib3 aliased", "import urllib3 as u3\np = u3.PoolManager()\n"),
    (
        "urllib3 from-import",
        "from urllib3 import HTTPSConnectionPool\np = HTTPSConnectionPool('h')\n",
    ),
    ("ldap3 connection", "import ldap3\ndef f(s):\n    return ldap3.Connection(s)\n"),
    ("ldap3 from-import alias", "from ldap3 import Connection as C\ndef f(s):\n    return C(s)\n"),
    ("aiohttp session", "import aiohttp\ndef f():\n    return aiohttp.ClientSession()\n"),
    ("aiohttp from-import", "from aiohttp import ClientSession\ns = ClientSession()\n"),
    (
        "websockets connect",
        "import websockets\nasync def f():\n    return await websockets.connect('ws://a')\n",
    ),
    (
        "websockets sync client",
        "from websockets.sync.client import connect\ndef f():\n    return connect('ws://a')\n",
    ),
    ("smtplib", "import smtplib\ndef f():\n    return smtplib.SMTP('h')\n"),
    ("smtplib from-import", "from smtplib import SMTP_SSL\ndef f():\n    return SMTP_SSL('h')\n"),
    (
        "raw socket create_connection",
        "import socket\ndef f():\n    return socket.create_connection(('h', 1))\n",
    ),
    (
        "raw socket connect",
        "import socket\ndef f():\n    s = socket.socket()\n    s.connect(('h', 1))\n    return s\n",
    ),
    (
        "socket.socket from-import",
        "from socket import socket as S\ndef f():\n    s = S()\n    s.connect(('h', 1))\n",
    ),
    (
        "asyncio.open_connection",
        "import asyncio\nasync def f():\n    return await asyncio.open_connection('h', 1)\n",
    ),
    (
        "urllib urlopen",
        "import urllib.request\ndef f():\n    return urllib.request.urlopen('http://a')\n",
    ),
    (
        "urlopen from-import",
        "from urllib.request import urlopen\ndef f():\n    return urlopen('http://a')\n",
    ),
    ("http.client", "import http.client\ndef f():\n    return http.client.HTTPSConnection('h')\n"),
    ("boto3", "import boto3\nc = boto3.client('s3')\n"),
]
#: Guarded forms the scanner must NOT report (so the self-test is not simply "flag everything").
MUST_PASS = [
    (
        "inline guard",
        _SEAM
        + "import httpx\ndef f():\n    return httpx.Client(**egress.guarded_transport_kwargs('x'))\n",
    ),
    (
        "guard through a name",
        _SEAM
        + "import httpx\ndef f(b):\n    kw = local_engine_client_kwargs(b)\n    return httpx.Client(**kw)\n",
    ),
    (
        "guard through an aliased client and a mutated kwargs dict",
        _SEAM
        + "import httpx as hx\ndef f(b):\n    kw = {'timeout': 3}\n    kw.update(egress.guarded_transport_kwargs('x'))\n    return hx.Client(**kw)\n",
    ),
    (
        "transport= argument",
        "import httpx\nfrom app.services.egress import GuardedTransport\n"
        "def f():\n    return httpx.Client(transport=GuardedTransport('x'))\n",
    ),
    (
        "SDK through the door's http_client",
        _SEAM
        + "from openai import OpenAI\nimport httpx\ndef f(b):\n    kw = local_engine_client_kwargs(b)\n"
        "    return OpenAI(http_client=httpx.Client(**kw))\n",
    ),
    (
        "SDK kwargs dict carrying the guarded http_client",
        _SEAM
        + "from openai import OpenAI\nimport httpx\ndef f(b):\n    ck = local_engine_client_kwargs(b)\n"
        "    kwargs = {'base_url': b}\n    kwargs['http_client'] = httpx.Client(**ck)\n    return OpenAI(**kwargs)\n",
    ),
]


def test_ratchet_scanner_self_test():
    """The scanner has to catch every synthetic bypass and accept the real guarded forms."""
    docs = "app/x.py"
    missed = []
    for label, src in MUST_CATCH:
        problems = ratchet_problems({"app/x.py": src}, {}, docs)
        if not any("UNGUARDED" in p for p in problems):
            missed.append(label)
    assert not missed, "ratchet missed: " + "; ".join(missed)
    wrongly = []
    for label, src in MUST_PASS:
        problems = ratchet_problems({"app/x.py": src}, {}, docs)
        if problems:
            wrongly.append(f"{label}: {problems}")
    assert not wrongly, "ratchet flagged guarded code: " + "; ".join(wrongly)


def test_ratchet_mutations():
    """The ratchet itself must bite: each mutation below has to be reported."""
    base = {
        "app/x.py": "import httpx\n"
        "from app.services import egress\n"
        "def ok():\n"
        "    return httpx.Client(**egress.guarded_transport_kwargs('x'))\n"
    }
    docs = "app/x.py app/y.py"
    assert ratchet_problems(base, {}, docs) == []
    # 1. a brand-new unguarded client
    new = dict(base, **{"app/y.py": "import httpx\ndef bad():\n    return httpx.get('http://a')\n"})
    assert any("UNGUARDED" in p for p in ratchet_problems(new, {}, docs)), "new client missed"
    # 2. a second, unguarded client hiding next to a guarded one in the same function
    two = {
        "app/x.py": "import httpx\n"
        "from app.services import egress\n"
        "def both():\n"
        "    a = httpx.Client(**egress.guarded_transport_kwargs('x'))\n"
        "    b = httpx.Client()\n"
        "    return a, b\n"
    }
    assert any("UNGUARDED" in p for p in ratchet_problems(two, {}, docs)), "laundered client missed"
    # 3. an exemption naming a client that no longer exists (fails on absence)
    stale = {("app/gone.py", "httpx.Client"): (1, "x" * 50)}
    assert any("STALE" in p for p in ratchet_problems(base, stale, docs)), "stale exemption missed"
    # 4. an exemption with a wrong count, and one with no reason
    y = {"app/y.py": "import httpx\ndef bad():\n    return httpx.get('http://a')\n"}
    assert any(
        "EXEMPT says" in p
        for p in ratchet_problems(y, {("app/y.py", "httpx.get"): (2, "r" * 50)}, docs)
    )
    assert any(
        "no real written reason" in p
        for p in ratchet_problems(y, {("app/y.py", "httpx.get"): (1, "short")}, docs)
    )
    # 5. a scanned file the docs inventory omits
    assert any("missing from the docs" in p for p in ratchet_problems(base, {}, "nothing"))
    # 6. an SDK constructor is only guarded when handed the door's http_client
    sdk = {
        "app/x.py": "from openai import OpenAI\n"
        "from app.services.providers_common import local_engine_client_kwargs\n"
        "def f(b):\n"
        "    kw = local_engine_client_kwargs(b, timeout=1)\n"
        "    return OpenAI(base_url=b)\n"
    }
    assert any("UNGUARDED" in p for p in ratchet_problems(sdk, {}, docs)), "bare SDK ctor passed"
    # 7. the real tree must not mask a bypass: a laundered guard dropped into a real file
    real = _app_sources()
    victim = "app/services/oidc.py"
    real[victim] += (
        "\n\ndef _laundered():\n"
        "    kw = egress.guarded_transport_kwargs('oidc')\n"
        "    return httpx.Client(timeout=1.0)\n"
    )
    docs_text = (ROOT / "docs/architecture/secrets-and-crypto.md").read_text(encoding="utf-8")
    assert any(
        "UNGUARDED" in p or "EXEMPT says" in p for p in ratchet_problems(real, EXEMPT, docs_text)
    ), "a laundered client added to a real file passed"


# --------------------------------------------------------------------------------------------
# 2. PER-CLIENT BEHAVIOUR
# --------------------------------------------------------------------------------------------
class _Net:
    """One local listener plus the patches that make every dial observable and offline."""

    def __init__(self):
        self.dials = []  # (address, port) every socket.create_connection was asked for
        self.os_dns = []  # names that reached the OS resolver (must stay empty)
        self.lookups = []  # names the stub resolver was asked about


@contextmanager
def sim(answers, *, allowlist=""):
    """`answers` is a list of per-lookup answers (the last repeats); an Exception instance in it
    is raised by the resolver. Every dial is redirected to the local listener and recorded, and
    the connected peer is reported as the address that was dialed."""
    net = _Net()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            body = (
                b'{"data":[{"id":"model-1"}],"token_endpoint":"http://x/t","issuer":"i",'
                b'"jwks_uri":"http://x/j","choices":[{"message":{"content":"ok"}}]}'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = _reply

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_connect = socket.create_connection
    seen = {"n": 0}

    def resolver(host):
        net.lookups.append(host)
        answer = answers[min(seen["n"], len(answers) - 1)]
        seen["n"] += 1
        if isinstance(answer, Exception):
            raise answer
        return [answer] if isinstance(answer, str) else list(answer)

    def create_connection(address, *args, **kwargs):
        net.dials.append(address)
        net.last = address[0]
        return real_connect(("127.0.0.1", port), *args, **kwargs)

    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host == "127.0.0.1":  # the redirect to our own listener inside create_connection
            return real_getaddrinfo(host, *args, **kwargs)
        net.os_dns.append(host)
        raise socket.gaierror("no real DNS in this suite")

    def info(stream, key):
        if key == "server_addr":
            return (net.last, port)
        return None

    net.port = port
    try:
        with (
            patch.object(egress, "_RESOLVER", resolver),
            patch.dict(os.environ, {ENV: allowlist}),
            patch.object(socket, "create_connection", create_connection),
            patch.object(socket, "getaddrinfo", getaddrinfo),
            patch.object(SyncStream, "get_extra_info", info),
            patch.object(
                SyncStream,
                "start_tls",
                lambda s, ssl_context, server_hostname=None, timeout=None: s,
            ),
        ):
            yield net
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def _chain(exc):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def outcome(call):
    """Run `call` and classify: 'ok', 'blocked' (a policy refusal) or 'connect' (transport)."""
    try:
        result = call()
    except Exception as exc:
        chain = _chain(exc)
        if any(isinstance(e, egress.EgressBlocked) or "Blocked target" in str(e) for e in chain):
            return "blocked"
        if any(isinstance(e, httpx.ConnectError) for e in chain):
            return "connect"
        raise
    if isinstance(result, dict) and "blocked" in result:  # providers.local_list_models_result
        if result["blocked"]:
            return "blocked"
        return "ok" if result.get("models") else "connect"
    return "ok"


# --- adapters: one callable per client, taking the base URL -----------------------------------
def _oidc(url):
    from app.services import oidc

    oidc.clear_cache()
    return lambda: oidc.discover(url)


def _qe_jev_hosted(url):
    from app.services import qe_jev

    judge = qe_jev.JevTarget(
        kind="jev",
        base_url=url,
        key="k",
        model="m",
        confidence_floor=None,
        meaning_floor=None,
        timeout_s=3.0,
        priced=False,
        provider_ref="engine:1",
        profile_id=1,
    )
    return lambda: qe_jev._post({"source": "a", "target": "b"}, judge=judge)


def _qe_jev_legacy(url):
    from app import config
    from app.services import qe_jev

    def call():
        with (
            patch.object(config, "TYPESAFE_BASE_URL", url),
            patch.object(config, "TYPESAFE_API_KEY", "k"),
        ):
            return qe_jev._post({"source": "a", "target": "b"})

    return call


def _local_models_result(url):
    from app.services import providers

    return lambda: providers.local_list_models_result(url, "key")


def _local_sdk(url):
    from app.services import providers

    return lambda: providers._local_client(url, "key", max_retries=0).models.list()


def _engines_probe_client(url):
    from app.services import engines

    def call():
        with engines._client(url, local_engine=True) as c:
            return c.get("/models")

    return call


def _engines_cloud_probe_client(url):
    from app.services import engines

    def call():
        with engines._client(url) as c:
            return c.get("/v1/models")

    return call


def _warmup_client(url):
    from app.services import model_warmup

    def call():
        with model_warmup._client(None, url) as c:
            return c.get("/models")

    return call


PUBLIC_POLICY = {
    "oidc": _oidc,
    "qe_jev hosted": _qe_jev_hosted,
}
LOCAL_POLICY = {
    "qe_jev legacy env (loopback research clone)": _qe_jev_legacy,
    "local list models": _local_models_result,
    "local openai sdk client": _local_sdk,
    "engine probe client (local)": _engines_probe_client,
    "engine probe client (cloud kind)": _engines_cloud_probe_client,
    "model warm-up client": _warmup_client,
}

PUBLIC = "93.184.216.34"
LAN = "10.20.30.40"
LOOPBACK = "127.0.0.1"
METADATA = "169.254.169.254"


def _url(host, net, scheme="http"):
    return f"{scheme}://{host}:{net.port}"


def test_public_policy_clients():
    failures = []
    for label, make in PUBLIC_POLICY.items():
        # a rebinding name: the preflight sees a public answer, the pin sees loopback
        with sim([PUBLIC, LOOPBACK]) as net:
            got = outcome(make(_url("rebind.test", net)))
            if got != "blocked" or net.dials or net.os_dns or len(net.lookups) < 2:
                failures.append(
                    f"{label}: rebind -> {got}, dials={net.dials}, os_dns={net.os_dns}, "
                    f"lookups={net.lookups}"
                )
        # an internal literal
        with sim([PUBLIC]) as net:
            got = outcome(make(_url(LAN, net)))
            if got != "blocked" or net.dials:
                failures.append(f"{label}: internal literal -> {got}, dials={net.dials}")
        # an allowlisted private name works and dials ONLY the validated address
        with sim([LAN], allowlist="idp.private.test") as net:
            got = outcome(make(_url("idp.private.test", net)))
            if got != "ok" or [d[0] for d in net.dials] != [LAN]:
                failures.append(f"{label}: allowlisted private -> {got}, dials={net.dials}")
        # the allowlist never admits a rebind to the fixed deny set
        with sim([LAN, METADATA], allowlist="idp.private.test") as net:
            got = outcome(make(_url("idp.private.test", net)))
            if got != "blocked" or net.dials:
                failures.append(
                    f"{label}: allowlisted rebind to metadata -> {got}, dials={net.dials}"
                )
        # an unresolvable name is a connection failure, with no dial
        with sim([socket.gaierror("no such host")]) as net:
            got = outcome(make(_url("typo.test", net)))
            if got != "connect" or net.dials or net.os_dns:
                failures.append(
                    f"{label}: unresolvable -> {got}, dials={net.dials}, os_dns={net.os_dns}"
                )
    assert not failures, "\n".join(failures)


def test_local_engine_clients():
    failures = []
    for label, make in LOCAL_POLICY.items():
        # a rebinding engine hostname: public answer, then the metadata address
        with sim([PUBLIC, METADATA]) as net:
            got = outcome(make(_url("rebind.test", net)))
            if got != "blocked" or net.dials or net.os_dns or len(net.lookups) < 2:
                failures.append(
                    f"{label}: rebind -> {got}, dials={net.dials}, os_dns={net.os_dns}, "
                    f"lookups={net.lookups}"
                )
        # a denied literal
        with sim([PUBLIC]) as net:
            got = outcome(make(_url(METADATA, net)))
            if got != "blocked" or net.dials:
                failures.append(f"{label}: metadata literal -> {got}, dials={net.dials}")
        # the deliberate allowance stays: a LAN name works and only the validated address is dialed
        with sim([LAN]) as net:
            got = outcome(make(_url("box.lan.test", net)))
            if got != "ok" or [d[0] for d in net.dials] != [LAN]:
                failures.append(f"{label}: LAN name -> {got}, dials={net.dials}")
        # ... and so does loopback (a literal)
        with sim([PUBLIC]) as net:
            got = outcome(make(_url(LOOPBACK, net)))
            if got != "ok":
                failures.append(f"{label}: loopback literal -> {got}, dials={net.dials}")
        # an unresolvable name is a connection failure with no dial
        with sim([socket.gaierror("no such host")]) as net:
            got = outcome(make(_url("typo.test", net)))
            if got != "connect" or net.dials or net.os_dns:
                failures.append(
                    f"{label}: unresolvable -> {got}, dials={net.dials}, os_dns={net.os_dns}"
                )
    assert not failures, "\n".join(failures)


def test_oidc_loopback_dev_idp_exception():
    """The documented dev IdP (plain http, loopback literal) works; https loopback does not."""
    with sim([PUBLIC]) as net:
        assert outcome(_oidc(_url("127.0.0.1", net))) == "ok"
        assert outcome(_oidc(_url("localhost", net))) == "ok"
    with sim([PUBLIC]) as net:
        assert outcome(_oidc(_url("127.0.0.1", net, "https"))) == "blocked"
        assert not net.dials


def test_oidc_user_facing_message():
    """A refused issuer shows the generic allowlist hint, never the internal address."""
    from app.services import oidc

    with sim([PUBLIC, LAN]) as net:
        oidc.clear_cache()
        try:
            oidc.discover(_url("rebind.test", net))
        except ValueError as exc:
            text = str(exc)
        else:
            raise AssertionError("discovery was not refused")
    assert "egress_private_allowlist" in text and LAN not in text and "rebind.test" not in text, (
        text
    )


def _real_shaped_jwt():
    """An unsigned but well-formed RS256 JWT with a kid, so jwt.get_unverified_header passes and
    _signing_key goes on to build its client and fetch the JWKS."""
    import base64
    import json

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'RS256', 'kid': 'k'})}.{seg({'sub': 'x'})}.c2ln"


def test_oidc_token_and_jwks_calls_are_guarded():
    from app.services import oidc

    with sim([PUBLIC, LAN]) as net:
        try:
            with oidc._client(None, "https://rebind.test/token") as c:
                c.post("https://rebind.test/token", data={"a": "b"})
        except egress.EgressBlocked:
            pass
        else:
            raise AssertionError("token POST to a rebinding name was not refused")
        assert not net.dials
    # the JWKS fetch: a real-shaped token reaches the client, the guard REFUSES (raised, not
    # swallowed) and nothing is dialed
    token = _real_shaped_jwt()
    with sim([PUBLIC, LAN]) as net:
        try:
            oidc._signing_key(token, {"jwks_uri": "https://rebind.test/jwks"})
        except egress.EgressBlocked:
            pass
        else:
            raise AssertionError("JWKS fetch from a rebinding name was not refused")
        assert not net.dials, net.dials
    # NON-VACUOUS: with the guard removed the very same call DOES dial, so a regression that
    # leaves the JWKS fetch unguarded turns this suite red
    with (
        sim([PUBLIC, LAN]) as net,
        patch.object(oidc.egress, "guarded_transport_kwargs", lambda *a, **k: {}),
    ):
        with contextlib.suppress(ValueError):  # the stub answer has no keys
            oidc._signing_key(token, {"jwks_uri": "https://rebind.test/jwks"})
        assert net.dials, "the JWKS probe never reached the client: the test is vacuous"


def test_oidc_loopback_exception_needs_a_loopback_issuer():
    """The plain-http loopback allowance belongs to a loopback dev IdP only. A public IdP whose
    discovery document names a loopback token_endpoint / jwks_uri is refused, and the
    client_secret is never posted there."""
    import types

    from app.services import oidc

    public_iss = "https://idp.example.com"
    # the refusal is in the https check ...
    for key in ("token_endpoint", "jwks_uri", "authorization_endpoint"):
        meta = {key: "http://127.0.0.1:9/x"}
        try:
            oidc._require_https(public_iss, meta)
        except ValueError:
            pass
        else:
            raise AssertionError(f"public issuer with a loopback {key} was accepted")
    # ... and a loopback dev IdP with loopback endpoints still passes
    oidc._require_https("http://127.0.0.1:8080", {"token_endpoint": "http://127.0.0.1:8080/t"})
    # belt and braces: even past that check the client for a loopback URL under a public issuer
    # is the GUARDED one (refused, no dial); under a loopback issuer it is the plain client
    with sim([PUBLIC]) as net:
        try:
            with oidc._client(None, "http://127.0.0.1:9/t", public_iss) as c:
                c.post("http://127.0.0.1:9/t", data={"client_secret": "S3CRET"})
        except egress.EgressBlocked:
            pass
        else:
            raise AssertionError("loopback token endpoint under a public issuer was dialed")
        assert not net.dials, net.dials
    with sim([PUBLIC]) as net:
        loop = _url("127.0.0.1", net)
        with oidc._client(None, loop + "/t", loop) as c:
            assert c.get(loop + "/t").status_code == 200
        assert net.dials
    # end to end: a hostile discovery document must stop the code exchange before any dial
    meta = {
        "issuer": public_iss,
        "authorization_endpoint": public_iss + "/a",
        "token_endpoint": "http://127.0.0.1:9/token",
        "jwks_uri": public_iss + "/jwks",
    }
    stub = types.SimpleNamespace(
        get=lambda k, d=None: {"sso_client_id": "cid"}.get(k, d),
        read_secret_setting=lambda k: "S3CRET",
    )
    request = types.SimpleNamespace(
        session={"oidc_state": "st", "oidc_nonce": "n", "oidc_verifier": "v"}
    )
    with (
        sim([PUBLIC]) as net,
        patch.object(oidc, "issuer", lambda: public_iss),
        patch.object(oidc, "settings", stub),
        patch.object(oidc, "redirect_uri", lambda r: "https://app.example.com/cb"),
        patch.dict(oidc._discovery_cache, {public_iss: meta}),
    ):
        claims, err = oidc.complete_login(request, "code", "st")
    assert claims is None and err == oidc._INSECURE_MSG, (claims, err)
    assert not net.dials, net.dials


def main():
    invoked = set()
    failures = []
    for test in (
        test_ratchet_scanner_self_test,
        test_ratchet_mutations,
        test_ratchet_real_tree,
        test_public_policy_clients,
        test_local_engine_clients,
        test_oidc_loopback_dev_idp_exception,
        test_oidc_user_facing_message,
        test_oidc_token_and_jwks_calls_are_guarded,
        test_oidc_loopback_exception_needs_a_loopback_issuer,
    ):
        invoked.add(test.__name__)
        try:
            test()
            print(f"  ok {test.__name__}")
        except Exception as exc:
            import traceback

            traceback.print_exc()
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
    assert invoked == {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert not failures, "\n".join(failures)
    print("verify_egress_coverage OK")


if __name__ == "__main__":
    main()
