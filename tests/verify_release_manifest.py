"""Release identity across the host CLI and authenticated read surfaces."""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)
from tests._bootstrap import init_env

init_env("loctms_release_")
import json

from tests._cli_runner import run_cli


def test_release_manifest():
    code, out, err = run_cli(["release", "manifest", "--json"])
    assert code == 0, (out, err)
    body = json.loads(out)
    assert body["version"] and body["alembic_head"]
    assert body["dependencies"]["requirements.lock"]
    for key in ("tag", "commit", "image_digest", "enabled_extras"):
        assert key in body, body
    code, out, err = run_cli(["--json", "version"])
    assert code == 0 and json.loads(out)["version"] == body["version"], (out, err)


def test_release_surfaces():
    import asyncio
    from unittest.mock import patch

    import httpx
    from fastapi.testclient import TestClient

    from app.cli._remote import RemoteClient
    from app.db import init_db
    from app.kernel import apikeys, security
    from app.main import app
    from app.release import app_version, coarse_version, release_manifest

    init_db()
    security.seed_initial()
    user = security.get_by_email("admin")
    _, token = apikeys.create_key(user.id, "release docs", scopes="docs")
    _, wrong = apikeys.create_key(user.id, "release api", scopes="api")
    client = TestClient(app)
    expected = release_manifest()
    # HIGH-1 (round 2): /health is anonymous, so it carries only the COARSE major.minor
    # form (audit 2026-09-05 L4) -- never the full/commit-bearing release string. The full
    # string is exposed only on the authenticated /api/release below.
    #
    # HIGH-2 (round 2): pin the SHAPE, not `== coarse_version(app_version())` (that held for
    # ANY implementation of the coarsener, including one that leaked the raw string). The
    # default checkout coarsens to major.minor.
    import re as _re

    health_version = client.get("/health").json()["version"]
    assert _re.fullmatch(r"\d+\.\d+", health_version), health_version
    assert health_version == coarse_version(app_version())
    # A forged commit-bearing RELEASE (the shapes DEPLOY.md's `git describe --tags --always
    # --dirty` yields on a tag-less or two-segment-tag checkout) must show "unknown" on the
    # anonymous /health -- the commit must NOT appear anywhere in the body.
    for forged in ("079a2706", "v0.8-9-gdeadbee"):
        with patch(
            "app.release.release_identity",
            return_value={
                "version": forged,
                "tag": None,
                "commit": forged,
            },
        ):
            body = client.get("/health").json()
            assert body["version"] == "unknown", (forged, body)
            assert forged not in json.dumps(body), (forged, body)
    assert client.get("/api/release").status_code == 401
    assert client.get("/api/release", headers={"X-Loc-Token": wrong}).status_code == 403
    response = client.get("/api/release", headers={"X-Loc-Token": token})
    assert response.status_code == 200 and response.json() == expected, response.text
    # the authenticated surface carries the FULL release string (not the coarse form)
    assert response.json()["version"] == app_version()

    def send(self, method, path, **kwargs):
        return client.request(method, path, headers=self._headers(), **kwargs)

    with patch.object(RemoteClient, "_send", send):
        code, out, err = run_cli(
            ["--remote", "http://127.0.0.1", "--token", token, "release", "manifest", "--json"]
        )
        assert code == 0 and json.loads(out) == expected, (code, out, err)
        code, out, err = run_cli(
            ["--remote", "http://127.0.0.1", "--token", wrong, "release", "manifest", "--json"]
        )
        assert code != 0, out
        code, out, err = run_cli(
            ["--remote", "http://127.0.0.1", "--token", token, "version", "--json"]
        )
        assert code == 0 and json.loads(out)["version"] == expected["version"], (out, err)
    sys.path.insert(0, str(_ROOT / "integrations/mcp-server"))
    import registry
    import server

    spec = next(t for t in registry.TOOLS if t.name == "release_manifest")
    assert spec.tier == "read" and spec.scope == "docs"
    # 2026-09-14: 81 -> 82 for #91's get_advanced_mission (3a685b1d), accounted
    # for in the registry and help/CLI/llms.txt/capability descriptors.
    # 2026-10-07: 82 -> 83 for #595's search_workspace (38f18c772), likewise on help, CLI,
    # llms.txt and the capability card.
    # 2026-10-13: 84 -> 86 for SPEC-project-automation part C's manage_custom_fields and
    # set_project_fields (projects family), on help, CLI, llms.txt and the agent guide;
    # 86 -> 87 for part B's project_notifications.
    # project automation part A: 87 -> 89 (project_automation, project_run).
    # open-core S2: 89 -> 72 (the 17 tools of the paid add-ons left with their code).
    assert len(registry.TOOLS) == 72

    def async_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Loc-Token": token},
        )

    with patch.object(server, "_client", async_client), patch.object(server, "TOKEN", token):
        assert json.loads(asyncio.run(server.HANDLERS["release_manifest"]())) == expected
    for rel in ("app/templates/help.html", "docs/CLI.md", "app/capabilities/llm.md"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        for marker in ("/api/release", "release_manifest", "release manifest", "version"):
            assert marker in text, (rel, marker)


def test_coarse_version_literals():
    """HIGH-2 (round 2): coarse_version coarsens the DEPLOY.md `git describe` shapes to a
    NON-identifying constant, never the raw commit-bearing string. Literal expectations on
    both sides (not coarse_version(x) == coarse_version(x), which pins nothing)."""
    from app.release import coarse_version

    assert coarse_version("079a2706") == "unknown"
    assert coarse_version("v0.8-9-gdeadbee") == "unknown"
    assert coarse_version("v0.8.0") == "0.8"
    assert coarse_version("0.8.0-dirty") == "0.8"
    # LOW-4 (round 3 gate): `str.isdigit()` is true for non-ASCII digits (superscripts,
    # Arabic-Indic), so a bare isdigit() guard would echo them back verbatim instead of
    # coarsening to the constant. The guard is ASCII-only, so these coarsen to 'unknown'
    # and NEVER return the raw non-parsing string.
    assert coarse_version("².³") == "unknown"  # superscript two/three
    assert coarse_version("٢.٣") == "unknown"  # Arabic-Indic two/three
    assert coarse_version("1.⁵") == "unknown"  # ASCII major, superscript minor


def test_health_release_doc_truth():
    """MED-1/MED-2/LOW-1/LOW-2/LOW-3 (rounds 3-5 gates): the operator-facing docs must state
    what /health ACTUALLY serves after HIGH-2 (round 2) coarsened it -- the coarse major.minor
    form, or 'unknown' for a release string that does not parse (a bare commit hash or
    `git describe` output). The round-4 ruling was "same sentence on all FOUR surfaces": the
    two in-app/human surfaces (app/templates/help.html, docs/CLI.md) and the two machine/deploy
    surfaces (app/capabilities/llm.md, docs/DEPLOY.md). help.html's round-1 claim that health
    'shares the baked RELEASE version' is the help-surface twin of the killed misconception;
    docs/CLI.md formerly said only that /health 'reads the same version source' without stating
    what it serves. llm.md's pre-relaxation claim that /health 'shows that version too' is the
    exact misconception HIGH-2 was raised to kill (LOW-2 also qualifies its `git describe`
    parenthetical the way DEPLOY.md does); DEPLOY.md must name the 'unknown' outcome its own
    `git describe --tags --always --dirty` recipe produces on a tag-less checkout; and the
    llm.md section must sit under its own `##` (the round-3 merge nested it under P4's egress
    `##`). Text pins on both the corrected phrasing and the absence of the false claim, on every
    surface, so a future edit that reintroduces the phrase on any one of them is caught."""
    llm = (_ROOT / "app/capabilities/llm.md").read_text(encoding="utf-8")
    # MED-2: the false pre-relaxation claim is gone; the coarse/unknown behavior is stated.
    assert "shows that version too" not in llm, (
        "llm.md still claims /health carries the full version"
    )
    # LOW-1 (round 3): the section is a top-level `##`, not nested under P4's egress `##`.
    assert "\n## Release identity\n" in llm, "Release identity must be its own ## section"
    assert "\n### Release identity\n" not in llm, "Release identity must not be an ### subsection"
    section = llm[llm.find("\n## Release identity\n") :][:700]
    assert "coarse major.minor" in section, section
    assert "unknown" in section, section
    # LOW-2 (round 5): the `git describe` parenthetical is qualified as DEPLOY.md does -- an
    # ordinary three-segment tag DOES parse, so only a non-parsing describe (a two-segment tag)
    # is `unknown`. Pins the corrected qualifier, so reverting to the bare "git describe output"
    # list (which is wrong for the common case) fails.
    assert "such as a two-segment tag" in section, section
    # LOW-3 (round 3): DEPLOY.md names the `unknown` outcome for the coarse /health form.
    deploy = (_ROOT / "docs/DEPLOY.md").read_text(encoding="utf-8")
    seg = deploy[deploy.find("coarse major.minor") :][:400]
    assert "coarse major.minor" in deploy, "DEPLOY.md dropped the coarse /health description"
    assert "unknown" in seg, "DEPLOY.md does not name the 'unknown' /health outcome"
    # MED-1 (round 5): the in-app help surface -- the one an operator reads inside the running
    # app, with no llm.md or DEPLOY.md in front of them -- carries the same rule, and the exact
    # round-1 misconception ("shares the baked RELEASE version") is gone from it.
    help_html = (_ROOT / "app/templates/help.html").read_text(encoding="utf-8")
    assert "shares the baked RELEASE version" not in help_html, (
        "help.html still claims /health carries the full baked RELEASE version"
    )
    assert "coarse major.minor" in help_html, "help.html dropped the coarse /health description"
    # LOW-1 (round 5): the CLI surface states what /health SERVES, not only what it reads.
    cli = (_ROOT / "docs/CLI.md").read_text(encoding="utf-8")
    assert "coarse major.minor" in cli, "docs/CLI.md does not say what /health serves"


def test_baked_identity():
    import tempfile
    from unittest.mock import patch

    from app import release

    with tempfile.TemporaryDirectory(prefix="loctms_release_identity_") as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text('[project]\nversion="0.8.0"\n', encoding="utf-8")
        with patch.object(release, "BASE_DIR", root):
            try:
                release.release_identity.cache_clear()
                assert release.app_version() == "0.8.0"
                (root / "RELEASE").write_text("v0.9.0-2-gabcdef0", encoding="utf-8")
                (root / "RELEASE_COMMIT").write_text("abcdef0123456789", encoding="utf-8")
                release.release_identity.cache_clear()
                assert release.release_identity() == {
                    "version": "v0.9.0-2-gabcdef0",
                    "tag": "v0.9.0",
                    "commit": "abcdef0123456789",
                }
            finally:
                release.release_identity.cache_clear()
    docker = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG RELEASE" in docker and "> /app/RELEASE" in docker
    assert "postgresql-client" in docker
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "RELEASE: ${RELEASE:-}" in compose and "localization-os:latest" not in compose
    ci = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert '--build-arg RELEASE="$(git describe --tags --always)"' in ci
    assert '--build-arg RELEASE_COMMIT="$(git rev-parse HEAD)"' in ci


def main():
    tests = [
        test_release_manifest,
        test_release_surfaces,
        test_coarse_version_literals,
        test_health_release_doc_truth,
        test_baked_identity,
    ]
    assert {f.__name__ for f in tests} == {n for n in globals() if n.startswith("test_")}
    for test in tests:
        test()
    print("verify_release_manifest OK")


if __name__ == "__main__":
    main()
