"""Shared doors for the Bilingua suites.

verify_bilingua_old_projects, verify_bilingua_e2e and verify_bilingua_failures drive the
same four doors (web form, /api, CLI in-process, MCP through the real server module routed
at the in-process app) for upload and export. One copy here so the three suites cannot
drift. Every helper returns what the door answered (status code, body, exit code, tool
error text) and never raises for a refusal, so a suite can assert "4xx, never 500".

FakeBilingua is a stand-in `bilingua` module with the T1 API (extract, merge, locos_units,
locos_set_targets, profiles, version, detect, BilinguaError) over a trivial paragraph
reader, so the REAL adapter, ingest, export and every door run without the wheel; only the
engine is fake. The failure suite injects each BilinguaError code through it (or through the
real wheel's functions when the wheel is installed).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = "Bili-Doors-Pass-2026-x"  # pregate: allow-secret throwaway fixture login


def real_engine():
    """The installed bilingua wheel with the T1 API, or None."""
    from app.filters import bilingua_adapter as ba

    return ba._module()


# --------------------------------------------------------------------------- fake engine
class _FakeError(Exception):
    def __init__(self, message: str, code: str = "parse"):
        super().__init__(message)
        self.code = code


class PanicException(BaseException):  # same name and base as pyo3's
    pass


def make_fake_bilingua() -> types.ModuleType:
    """A `bilingua` module with the T1 API over a toy reader: units are the blank-line
    separated paragraphs of a UTF-8 file, merge writes the targets back in their place.
    Enough for every door to reach the engine; never used to judge a format."""
    m = types.ModuleType("bilingua")
    m.BilinguaError = _FakeError
    m.PanicException = PanicException

    def version():
        return "0.0.0-fake"

    def profiles():
        return {"locos": 1}

    def detect(name, data):
        return "srt" if str(name).endswith(".srt") else None

    def extract(name, data, **kw):
        text = bytes(data).decode("utf-8")
        paras = text.split("\n\n")
        units = []
        for i, p in enumerate(paras):
            if p.strip():
                units.append(
                    {
                        "id": f"u{i}",
                        "segments": [{"id": f"u{i}", "source": [{"text": p.strip()}]}],
                        "context": {},
                    }
                )
        return {
            "format": "markdown",
            "units": units,
            "skeleton": {"text": text},
            "warnings": [],
            "parser": {
                "bilingua": version(),
                "format": "markdown",
                "crate": "fake",
                "crate_version": "0",
                "schema": 1,
                "profile": kw.get("profile"),
                "profile_version": 1,
            },
            "_paras": paras,
        }

    def locos_units(doc):
        out = []
        for n, u in enumerate(doc["units"]):
            out.append(
                {
                    "p_index": n,
                    "segment_id": u["id"],
                    "text": u["segments"][0]["source"][0]["text"],
                    "map": {},
                    "style": "",
                }
            )
        return out

    def locos_set_targets(doc, targets):
        doc = json.loads(json.dumps(doc))
        for u in doc["units"]:
            if u["id"] in targets:
                u["segments"][0]["target"] = [{"text": targets[u["id"]]}]
        return doc, {}

    def merge(doc, **kw):
        paras = list(doc["_paras"])
        for u in doc["units"]:
            tgt = u["segments"][0].get("target")
            if tgt is not None:
                i = int(u["id"][1:])
                lead = paras[i][: len(paras[i]) - len(paras[i].lstrip())]
                trail = paras[i][len(paras[i].rstrip()) :]
                paras[i] = lead + tgt[0]["text"] + trail
        return "\n\n".join(paras).encode("utf-8")

    def extract_handle(name, data, **kw):
        return PyHandle(m, m.extract(name, data, **kw))  # m.extract: a suite may wrap it

    for f in (
        version,
        profiles,
        detect,
        extract,
        extract_handle,
        locos_units,
        locos_set_targets,
        merge,
    ):
        setattr(m, f.__name__, f)
    m.DocumentHandle = PyHandle
    return m


class PyHandle:
    """A DocumentHandle (SPEED2) over a Document dict and a module's dict functions, looked up
    at call time, so a suite that wraps `module.locos_set_targets` or `module.merge` (to raise
    an injected error, or to spy) reaches the call the adapter makes through the handle."""

    def __init__(self, module, doc: dict):
        self._m = module
        self._d = doc

    @staticmethod
    def from_dict(document):
        raise NotImplementedError("PyHandle.from_dict: use PyHandle(module, doc)")

    def to_dict(self) -> dict:
        return json.loads(json.dumps(self._d))

    def locos_units(self):
        return self._m.locos_units(self._d)

    def locos_set_targets(self, targets):
        doc, problems = self._m.locos_set_targets(self._d, targets)
        return PyHandle(self._m, doc), problems

    def merge(self, **kw):
        return self._m.merge(self._d, **kw)

    @property
    def format(self):
        return self._d.get("format")

    @property
    def parser(self):
        return self._d.get("parser")

    @property
    def warnings(self):
        return list(self._d.get("warnings") or [])

    @property
    def source_encoding(self):
        return self._d.get("source_encoding")

    def __len__(self) -> int:
        return len(self._d.get("units") or [])

    def unit_extras(self, names=None):
        out = []
        for unit in self._d.get("units") or []:
            extra = (unit.get("context") or {}).get("extra") or {}
            if names is not None:
                extra = {k: v for k, v in extra.items() if k in names}
            if extra:
                out.append(
                    {
                        "unit": unit.get("id"),
                        "segments": [s.get("id") for s in unit.get("segments") or []],
                        "translate": unit.get("translate", True),
                        "extra": dict(extra),
                    }
                )
        return out


class InjectableHandle:
    """A real wheel DocumentHandle whose locos_units / locos_set_targets / merge call the
    module function of the same name instead when a suite replaced it (`replaced` names the
    replaced functions): the wheel's handle methods cannot be patched, so an error injected
    into `bilingua.merge` still reaches the adapter's `handle.merge`. Untouched calls go to
    the real handle."""

    def __init__(self, module, replaced, handle):
        self._m, self._r, self._h = module, replaced, handle

    def locos_units(self):
        if "locos_units" in self._r:
            return self._m.locos_units(self._h.to_dict())
        return self._h.locos_units()

    def locos_set_targets(self, targets):
        if "locos_set_targets" in self._r:
            doc, problems = self._m.locos_set_targets(self._h.to_dict(), targets)
            return InjectableHandle(self._m, self._r, self._m.DocumentHandle.from_dict(doc)), (
                problems
            )
        h, problems = self._h.locos_set_targets(targets)
        return InjectableHandle(self._m, self._r, h), problems

    def merge(self, **kw):
        if "merge" in self._r:
            return self._m.merge(self._h.to_dict(), **kw)
        return self._h.merge(**kw)

    def __len__(self) -> int:
        return len(self._h)

    def __getattr__(self, name):
        return getattr(self._h, name)


def route_handles(module, replaced) -> None:
    """Make the real wheel's `extract_handle` honour functions a suite replaces on the module:
    `extract` replaced -> extract_handle calls it (and wraps the dict); the handle it returns
    is an InjectableHandle. `replaced` is a live collection the suite keeps up to date."""
    real = getattr(module, "_real_extract_handle", None) or module.extract_handle
    module._real_extract_handle = real

    def extract_handle(name, data, **kw):
        if "extract" in replaced or "extract_handle" in replaced:
            return module.DocumentHandle.from_dict(module.extract(name, data, **kw))
        return InjectableHandle(module, replaced, real(name, data, **kw))

    module.extract_handle = extract_handle


class engine_swap:
    """Context manager: put `module` in sys.modules["bilingua"] (None = the module missing)."""

    def __init__(self, module):
        self.module = module
        self.had = "bilingua" in sys.modules
        self.old = sys.modules.get("bilingua")

    def __enter__(self):
        sys.modules["bilingua"] = self.module
        return self.module

    def __exit__(self, *exc):
        if self.had:
            sys.modules["bilingua"] = self.old
        else:
            sys.modules.pop("bilingua", None)
        return False


# --------------------------------------------------------------------------- fixtures
def api_token(email: str) -> dict:
    from app.models import Role
    from app.services import apikeys, security

    user = security.get_by_email(email)
    if user is None:
        user = security.create_user(email, email.split("@")[0], Role.ADMIN, PASSWORD, org_id=1)
    _row, raw = apikeys.create_key(user.id, email.split("@")[0], scopes="api docs")
    return {"X-Loc-Token": raw}


def new_project(name: str, targets=("de-DE",), source="en-US") -> int:
    from sqlmodel import Session

    from app.db import engine
    from app.models import Project, TargetLocale

    with Session(engine) as s:
        p = Project(name=name, org_id=1, source_locale=source, default_provider="mock")
        s.add(p)
        s.commit()
        s.refresh(p)
        for loc in targets:
            s.add(TargetLocale(project_id=p.id, locale=loc))
        s.commit()
        return p.id


def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def doc_ids(pid: int) -> list[int]:
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Document

    with Session(engine) as s:
        return [d.id for d in s.exec(select(Document).where(Document.project_id == pid)).all()]


def skeleton(doc_id: int) -> dict:
    from sqlmodel import Session

    from app.db import engine
    from app.models import Document

    with Session(engine) as s:
        return dict(s.get(Document, doc_id).skeleton or {})


def write_tmp(name: str, data: bytes) -> str:
    d = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "door-files")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# --------------------------------------------------------------------------- upload doors
def upload_web(c, pid: int, name: str, data: bytes):
    """The project page form. Returns (status, location, new doc id or None)."""
    from tests._csrf import csrf_value

    before = set(doc_ids(pid))
    token = csrf_value(c)
    r = c.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented", "csrf_token": token},
        files={"file": (name, data, "application/octet-stream")},
        follow_redirects=False,
    )
    new = sorted(set(doc_ids(pid)) - before)
    return r.status_code, r.headers.get("location", ""), (new[0] if new else None)


def upload_api(c, tok: dict, pid: int, name: str, data: bytes):
    r = c.post(
        "/api/documents",
        headers=tok,
        data={"project_id": str(pid)},
        files={"file": (name, data, "application/octet-stream")},
    )
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:300]}
    return r.status_code, body


def run_cli(argv):
    """The CLI in-process. Logging is silenced for the call so (code, stdout, stderr) is what
    the CLI itself printed to its user, not the app's operator log lines."""
    import logging

    from tests._cli_runner import run_cli as _run

    logging.disable(logging.CRITICAL)
    try:
        return _run(argv)
    finally:
        logging.disable(logging.NOTSET)


def upload_cli(pid: int, name: str, data: bytes):
    """`loc doc upload` in-process (--json). Returns (exit code, parsed json or None, text)."""
    path = write_tmp(name, data)
    code, out, err = run_cli(["--json", "doc", "upload", str(pid), path])
    try:
        body = json.loads(out)
    except ValueError:
        body = None
    return code, body, out + err


_MCP = None


def mcp_server(c, tok: dict):
    """The real MCP server module (integrations/mcp-server/server.py), its httpx client
    routed at the in-process app with this token, local file reads allowed under the data dir."""
    global _MCP
    import httpx

    from app.main import app

    d = os.path.join(ROOT, "integrations", "mcp-server")
    if d not in sys.path:
        sys.path.insert(0, d)
    import server

    server.BASE_URL = "http://testserver"
    server.TOKEN = tok["X-Loc-Token"]
    statuses: list = []

    async def hook(resp):
        statuses.append(resp.status_code)

    def _patched():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Loc-Token": server.TOKEN},
            event_hooks={"response": [hook]},
            timeout=120,
        )

    server._client = _patched
    server._allow_local_file_reads(True, roots=[os.environ["LOC_TMS_DATA_DIR"]])
    server.OUTPUT_DIR = __import__("pathlib").Path(os.environ["LOC_TMS_DATA_DIR"]) / "mcp-out"
    server._t5_statuses = statuses
    _MCP = server
    return server


def upload_mcp(server, pid: int, name: str, data: bytes):
    """MCP submit_translation with no targets (upload only). Returns (ok, json or error text,
    HTTP statuses the server saw)."""
    path = write_tmp(name, data)
    server._t5_statuses.clear()
    try:
        out = asyncio.run(server.submit_translation(path, [], project_id=pid, confirm=True))
        return True, json.loads(out), list(server._t5_statuses)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", list(server._t5_statuses)


# --------------------------------------------------------------------------- export doors
def export_web(c, doc_id: int, locale: str, allow_held: bool = False):
    q = f"/documents/{doc_id}/export?locale={locale}" + ("&allow_held=1" if allow_held else "")
    r = c.get(q, headers={"Accept": "text/html"})
    return r.status_code, r.content


def export_api(c, tok: dict, doc_id: int, locale: str, allow_held: bool = False):
    params = {"locale": locale}
    if allow_held:
        params["allow_held"] = "1"
    r = c.get(f"/api/documents/{doc_id}/download", headers=tok, params=params)
    return r.status_code, r.content


def export_cli(doc_id: int, locale: str, allow_held: bool = False):
    out = os.path.join(os.environ["LOC_TMS_DATA_DIR"], "door-files", f"cli-{doc_id}-{locale}")
    if os.path.exists(out):
        os.remove(out)
    argv = ["doc", "export", str(doc_id), "--locale", locale, "-o", out, "--force"]
    if allow_held:
        argv.append("--allow-held")
    code, so, se = run_cli(argv)
    data = None
    if code == 0 and os.path.exists(out):
        with open(out, "rb") as fh:
            data = fh.read()
    return code, data, so + se


def export_mcp(server, doc_id: int, locale: str):
    server._t5_statuses.clear()
    try:
        out = json.loads(asyncio.run(server.get_result(doc_id, locale)))
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", list(server._t5_statuses)
    if out.get("path"):
        return True, open(out["path"], "rb").read(), list(server._t5_statuses)
    return True, out, list(server._t5_statuses)


# --------------------------------------------------------------------------- translate
def mock_translate(doc_id: int, locale: str) -> None:
    """Pre-translate with the project's mock engine (in-process, offline)."""
    from sqlmodel import Session

    from app.db import engine
    from app.services import translate

    with Session(engine) as s:
        for _ in translate.pretranslate_stream(s, doc_id, locale):
            pass


def segments(doc_id: int, locale: str) -> list:
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Segment

    with Session(engine) as s:
        rows = s.exec(
            select(Segment)
            .where(Segment.document_id == doc_id, Segment.target_locale == locale)
            .order_by(Segment.order_index)
        ).all()
        return [(r.id, r.order_index, r.source_text, r.target_text, r.status) for r in rows]


LEAK_MARKERS = ("Traceback", "panicked at", "PanicException", "RUST_BACKTRACE", "src/lib.rs")


def no_leak(text) -> bool:
    t = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
    return not any(m in t for m in LEAK_MARKERS)
