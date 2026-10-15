"""FX-1 save validation regressions. Offline, through test_verify_suites only."""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_fx1_")
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli
from app.db import engine, init_db
from app.main import app
from app.models import EngineProfile, Setting, Workflow
from app.services import (
    apikeys,
    engines,
    jev_inspector,
    qe_jev_loop,
    security,
    settings,
    setup_author,
    workflows,
)

errors = []


def check(ok, label):
    if not ok:
        errors.append(label)
        print("FAIL " + label)


def refused(fn, label, words=""):
    try:
        fn()
    except (ValueError, setup_author.SetupAuthorError) as exc:
        check(words.lower() in str(exc).lower(), label + ": " + str(exc))
    else:
        check(False, label + " accepted")


def run_cli(argv, contains=""):
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            code = cli(argv)
        except SystemExit as exc:
            code = exc.code
    if contains:
        check(contains in output.getvalue(), "CLI refusal reason: " + output.getvalue())
    return code


def main():
    init_db()
    local = engines.create_profile("FX1 judge", "jev_local", base_url="http://127.0.0.1:8077")
    hosted = engines.create_profile(
        "FX1 hosted",
        "jev",
        api_key="fx1-fake-key",  # pregate: allow-secret offline judge fixture
    )
    for ref in ("jev", f"engine:{local.id}", f"engine:{hosted.id}"):
        refused(
            lambda ref=ref: settings.validate_value("default_fallback_provider", ref), "#188 " + ref
        )
        refused(
            lambda ref=ref: settings.set_many({"default_fallback_provider": ref}),
            "#188 set_many " + ref,
        )
    settings.validate_value("default_fallback_provider", "")
    with Session(engine) as s:
        wf = workflows.create_workflow(s, "FX1")
        st = workflows.add_step(s, wf.id, name="original", provider="mock")
        for key, bads, issue in (
            ("qe_threshold", [-5, 101, "abc", "85%", 70.9, 70.0, True], "#190"),
            ("qe_autofix_max_rounds", [0, 6, -1, "abc", 2.7, True], "#191"),
            ("qe_autofix_provider", ["engine:999999"], "#192"),
        ):
            for bad in bads:
                refused(
                    lambda key=key, bad=bad: workflows.add_step(
                        s, wf.id, name="bad", provider="mock", **{key: bad}
                    ),
                    f"{issue} add {bad!r}",
                )
                refused(
                    lambda key=key, bad=bad: workflows.update_step(s, st.id, **{key: bad}),
                    f"{issue} edit {bad!r}",
                )
        for threshold in (0, "70", 100):
            check(
                workflows.update_step(s, st.id, qe_threshold=threshold).qe_threshold
                == int(threshold),
                "#190 valid boundary",
            )
        for rounds, expected in ((1, 1), (5, 5), ("", 3), (None, 3)):
            check(
                workflows.update_step(s, st.id, qe_autofix_max_rounds=rounds).qe_autofix_max_rounds
                == expected,
                "#191 valid/default",
            )
        wid, sid = wf.id, st.id
    refused(
        lambda: engines.create_profile(
            "bad floor",
            "jev_local",
            base_url="http://127.0.0.1:8077",
            cfg={"confidence_floor": 1.0},
        ),
        "#203 create",
    )
    refused(lambda: engines.update_profile(local.id, cfg={"confidence_floor": 1.0}), "#203 update")
    for key, value in (("confidence_floor", 0.7), ("meaning_floor", 0.8), ("timeout_s", 12)):
        engines.update_profile(local.id, cfg={key: value})
        updated = engines.update_profile(local.id, cfg={key: ""})
        check(key not in updated.config, "#204 blank clears " + key)
    check(engines.update_profile(local.id, model="").model == "jev-latest", "#204 blank model")
    with Session(engine) as s:
        old = s.get(EngineProfile, local.id)
        old.config = {"confidence_floor": 1.0}
        s.add(old)
        s.commit()
    check(
        jev_inspector.inspect_payload(engines.get_profile(local.id))["warnings"],
        "#203 legacy inspect warns",
    )
    check(
        engines.update_profile(local.id, name="Legacy renamed").config["confidence_floor"] == 1.0,
        "#203 stored floor untouched",
    )
    engines.update_profile(local.id, cfg={"confidence_floor": ""})
    for bad in (
        "english",
        "french",
        "fr;DROP",
        "en-abcd-XX-junk!",
        "en--US",
        "en-abc",
        "en-1901-1901",
        "",
        None,
        5,
    ):
        refused(
            lambda bad=bad: jev_inspector._clean_locale(bad, "Source locale"),
            "#210 locale " + repr(bad),
            "2-3 letter language",
        )
    for label in ("Source", "Target"):
        refused(
            lambda label=label: jev_inspector._clean_text(42, label),
            "#210 " + label,
            "must be text",
        )
    for code in (
        "en",
        "de",
        "pt-BR",
        "zh-Hans",
        "es-419",
        "zh-Hant-TW",
        "sr-Latn-RS",
        "sr-Cyrl-RS",
        "de-CH-1901",
        "sl-rozaj-biske",
    ):
        try:
            check(jev_inspector._clean_locale(code, "Locale") == code, "#210 valid " + code)
        except ValueError as exc:
            check(False, "#210 valid " + code + ": " + str(exc))
    for code, canonical in (("ZH-hant-tw", "zh-Hant-TW"), ("pt_BR", "pt-BR"), ("EN", "en")):
        check(jev_inspector._clean_locale(code, "Locale") == canonical, "locale canonical " + code)
    for bad in (1.0, "1.0", 1.1, True):
        refused(
            lambda bad=bad: settings.validate_value("qe_jev_confidence_floor", bad),
            "install floor " + repr(bad),
            "less than 1.0",
        )
    refused(
        lambda: settings.set_many({"qe_jev_confidence_floor": "1.0"}),
        "install floor set_many",
        "less than 1.0",
    )
    for good in (0.5, "0.999"):
        settings.validate_value("qe_jev_confidence_floor", good)
    check(
        settings.parse_qe_jev_confidence_floor("1.0", clamp=True) == 1.0,
        "legacy install floor loads",
    )
    with Session(engine) as s:
        s.merge(Setting(key="qe_jev_confidence_floor", value="1.0"))
        s.commit()
    settings.invalidate_cache()
    settings.apply_to_config()
    try:
        check(
            bool(jev_inspector.inspect_payload(engines.get_profile(local.id))["warnings"]),
            "inherited legacy floor warns",
        )
        settings.set_many({"qe_jev_enabled": "1"})
        check(
            settings.get("qe_jev_confidence_floor") == "1.0",
            "unrelated settings preserve legacy floor",
        )
    finally:
        settings.set_many({"qe_jev_confidence_floor": "0.6"})
    with Session(engine) as s:
        refused(
            lambda: workflows.update_step(s, sid, qe_autofix_provider="engine:99999"),
            "missing fixer wording",
            "no engine #99999 exists",
        )
        # External steps are not supported by setup-author's draft flow.
        for step_type in ("lqa", "human_review", "tm_match"):
            draft = {
                "name": "Hidden QE " + step_type,
                "source_locale": "en-US",
                "target_locales": ["fr-FR"],
                "steps": [
                    {
                        "name": "review",
                        "step_type": step_type,
                        "qe_threshold": "junk",
                        "qe_autofix_max_rounds": "junk",
                        "qe_provider": "junk",
                        "qe_autofix_provider": "junk",
                    }
                ],
            }
            try:
                warnings = []
                setup_author.apply_workflow_draft(
                    s, draft, actor="tester", org_id=None, warnings=warnings
                )
                check(
                    any("dropped" in w and "QE" in w for w in warnings),
                    "hidden QE warning " + step_type,
                )
                wf = s.exec(select(Workflow).where(Workflow.name == draft["name"])).one()
                from app.models import WorkflowStep

                step = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wf.id)).one()
                check(
                    step.qe_threshold == 70
                    and step.qe_autofix_max_rounds == 3
                    and not step.qe_provider
                    and not step.qe_autofix_provider,
                    "hidden QE dropped " + step_type,
                )
                draft["name"] += " defaults"
                draft["steps"][0].update(qe_threshold=70.0, qe_autofix_max_rounds=3.0)
                setup_author.apply_workflow_draft(s, draft, actor="tester", org_id=None)
            except setup_author.SetupAuthorError as exc:
                check(False, "hidden QE " + step_type + ": " + str(exc))
    with patch.object(qe_jev_loop, "refine", return_value={}) as refine:
        for args in (
            ["--max-tries", "0"],
            ["--max-tries", "-1"],
            ["--target-score", "150"],
            ["--target-score", "-1"],
            ["--pairs", "missing.jsonl", "--source", "x"],
            ["--pairs", "", "--target", "x"],
        ):
            check(run_cli(["lqa", "jev-refine", *args]) == 2, "#211 " + " ".join(args))
        check(not refine.called, "#211 invalid input never runs")
    surfaces(local, wid, sid)
    assert not errors, errors
    print("verify_fx1_validation OK")


def surfaces(local, wid, sid):
    client = TestClient(app)
    user = security.create_user("fx1@test.invalid", "FX1", "admin", "fx1-test-password")
    _, token = apikeys.create_key(user.id, "fx1", scopes="api docs admin")
    settings.set_many({"remote_admin_enabled": "1"})
    headers = {"X-Loc-Token": token}
    check(
        run_cli(["config", "set", "qe_jev_confidence_floor", "1.0"], contains="less than 1.0") == 2,
        "install floor CLI refuses",
    )
    r = client.post(
        "/api/admin/config",
        json={"settings": {"qe_jev_confidence_floor": "1.0"}},
        headers=headers,
    )
    check(r.status_code == 400 and "less than 1.0" in r.text, "install floor API refuses")
    with Session(engine) as s:
        workflows.update_step(s, sid, qe_autofix_max_rounds=5)
    check(
        run_cli(["workflow", "edit-step", str(wid), str(sid), "--qe-autofix-max-rounds", ""]) == 0,
        "#191 CLI blank rounds restores default",
    )
    from app.models import WorkflowStep

    with Session(engine) as s:
        check(s.get(WorkflowStep, sid).qe_autofix_max_rounds == 3, "#191 CLI blank persisted 3")
    for key, bad in (
        ("qe_threshold", "70.9"),
        ("qe_autofix_max_rounds", "6"),
        ("qe_autofix_provider", "engine:999999"),
    ):
        for suffix, body in (
            ("", {"name": "bad", "provider": "mock", key: bad}),
            (f"/{sid}/edit", {key: bad}),
        ):
            r = client.post(f"/api/workflows/{wid}/steps{suffix}", json=body, headers=headers)
            check(r.status_code == 400, f"API refuses {key} {suffix}: {r.status_code}")
        for url in (f"/workflows/{wid}/steps", f"/workflows/{wid}/steps/{sid}/edit"):
            r = client.post(
                url,
                data={"name": "bad", "step_type": "llm_agent", "provider": "mock", key: bad},
                follow_redirects=False,
            )
            check(r.status_code == 400, f"web refuses {key}: {r.status_code}")
        flag = "--" + key.replace("_", "-")
        reason = {
            "qe_threshold": "--qe-threshold",
            "qe_autofix_max_rounds": "QE auto-fix max rounds",
            "qe_autofix_provider": "unknown auto-fix post-editor",
        }[key]
        check(
            run_cli(
                [
                    "workflow",
                    "add-step",
                    str(wid),
                    "--name",
                    "bad",
                    "--provider",
                    "mock",
                    flag,
                    bad,
                ],
                contains=reason,
            )
            == 2,
            "CLI add " + key,
        )
        check(
            run_cli(["workflow", "edit-step", str(wid), str(sid), flag, bad], contains=reason) == 2,
            "CLI edit " + key,
        )
        draft = {
            "name": "Refused draft",
            "source_locale": "en-US",
            "target_locales": ["fr-FR"],
            "steps": [{"name": "bad", "step_type": "llm_agent", "provider": "mock", key: bad}],
        }
        with Session(engine) as s:
            refused(
                lambda draft=draft: setup_author.apply_workflow_draft(
                    s, draft, actor="tester", org_id=None
                ),
                "author confirm " + key,
            )
            check(
                not s.exec(select(Workflow).where(Workflow.name == "Refused draft")).first(),
                "author rejection atomic",
            )
        if key != "qe_autofix_provider":
            step, warnings = setup_author._validate_step_draft(draft["steps"][0], 0, [], ["mock"])
            check(step[key] == bad and warnings, "author preserves invalid " + key)
    for ref in ("jev", f"engine:{local.id}"):
        check(
            run_cli(
                ["config", "set", "default_fallback_provider", ref], contains="translation engine"
            )
            == 2,
            "#188 CLI",
        )
        r = client.post(
            "/api/admin/config",
            json={"settings": {"default_fallback_provider": ref}},
            headers=headers,
        )
        check(r.status_code == 400 and "translation engine" in r.text, "#188 API: " + r.text)
    for action in ("create", "update"):
        args = ["engine", action]
        if action == "create":
            args += [
                "--name",
                "bad CLI judge",
                "--kind",
                "jev_local",
                "--base-url",
                "http://127.0.0.1:8077",
            ]
        else:
            args += [str(local.id)]
        check(run_cli([*args, "--config", "confidence_floor=1.0"]) == 2, "#203 CLI " + action)
    r = client.post(
        f"/api/admin/engines/{local.id}/update",
        json={"config": {"confidence_floor": 1.0}},
        headers=headers,
    )
    check(r.status_code == 400, "#203 API")
    engines.update_profile(
        local.id, cfg={"confidence_floor": 0.7, "meaning_floor": 0.8, "timeout_s": 12}
    )
    r = client.post(
        f"/api/admin/engines/{local.id}/update",
        json={"config": {"confidence_floor": ""}},
        headers=headers,
    )
    check(
        r.status_code == 200 and "confidence_floor" not in engines.get_profile(local.id).config,
        "#204 API clears",
    )
    check(
        run_cli(
            [
                "engine",
                "update",
                str(local.id),
                "--model",
                "",
                "--config",
                "meaning_floor=",
                "--config",
                "timeout_s=",
            ]
        )
        == 0,
        "#204 CLI update",
    )
    prof = engines.get_profile(local.id)
    check(prof.config == {} and prof.model == "jev-latest", "#204 CLI clears and normalizes")
    for url, body in (
        (
            "/setup-ai/engines",
            {
                "name": "bad web",
                "kind": "jev_local",
                "base_url": "http://127.0.0.1:8077",
                "confidence_floor": "1.0",
            },
        ),
        (
            f"/setup-ai/engines/{local.id}",
            {"name": "bad web", "base_url": "http://127.0.0.1:8077", "confidence_floor": "1.0"},
        ),
    ):
        r = client.post(url, data=body, follow_redirects=False)
        check(
            r.status_code == 303
            and "engine_error=confidence_floor" in r.headers.get("location", ""),
            "#203 web: " + str(r.status_code),
        )
    r = client.post(
        "/api/admin/engines",
        json={
            "name": "bad API",
            "kind": "jev_local",
            "base_url": "http://127.0.0.1:8077",
            "config": {"confidence_floor": 1.0},
        },
        headers=headers,
    )
    check(r.status_code == 400 and "confidence_floor" in r.text, "#203 API create")
    with Session(engine) as s:
        old = s.get(EngineProfile, local.id)
        old.config = {"confidence_floor": 1.0}
        s.add(old)
        s.commit()
    for surface in ("web", "API", "CLI"):
        name = "Legacy " + surface
        if surface == "web":
            r = client.post(
                f"/setup-ai/engines/{local.id}",
                data={
                    "name": name,
                    "base_url": "http://127.0.0.1:8077",
                    "confidence_floor": "1.0",
                    "api_key": "offline-rotation",  # pregate: allow-secret offline rotation fixture
                },
                follow_redirects=False,
            )
            check(
                r.status_code == 303 and "engine_error" not in r.headers.get("location", ""),
                "M1 web round-trip",
            )
        elif surface == "API":
            r = client.post(
                f"/api/admin/engines/{local.id}/update",
                json={"name": name, "enabled": False, "config": {"confidence_floor": 1.0}},
                headers=headers,
            )
            check(r.status_code == 200, "M1 API round-trip")
        else:
            check(
                run_cli(
                    [
                        "engine",
                        "update",
                        str(local.id),
                        "--name",
                        name,
                        "--config",
                        "confidence_floor=1.0",
                    ]
                )
                == 0,
                "M1 CLI round-trip",
            )
        prof = engines.get_profile(local.id)
        check(
            prof.name == name and prof.config["confidence_floor"] == 1.0, "M1 persisted " + surface
        )
    check(
        not prof.enabled and engines.dec_key(prof.api_key_enc) == "offline-rotation",
        "M1 disable and re-key",
    )
    refused(
        lambda: engines.update_profile(local.id, cfg={"confidence_floor": 1.1}),
        "M1 changed invalid floor",
    )
    engines.update_profile(local.id, enabled=True, cfg={"confidence_floor": 0.7})
    for field, bad, words in (
        ("source", 42, "Source must be text"),
        ("target", [], "Target must be text"),
        ("source_locale", "english", "locale code"),
    ):
        body = {
            "source": "Hello",
            "target": "Hallo",
            "source_locale": "en",
            "target_locale": "de",
            field: bad,
        }
        r = client.post(f"/api/admin/engines/{local.id}/jev-try", json=body, headers=headers)
        check(r.status_code == 400 and words in r.text, "#210 API " + field)
    mcp_dir = Path(__file__).resolve().parents[1] / "integrations" / "mcp-server"
    sys.path.insert(0, str(mcp_dir))
    spec = importlib.util.spec_from_file_location("fx1_mcp_server", mcp_dir / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    def local_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
        )

    with patch.object(server, "_client", local_client), patch.object(server, "TOKEN", token):
        for key, bad in (
            ("qe_threshold", 70.9),
            ("qe_autofix_max_rounds", 6),
            ("qe_autofix_provider", "engine:999999"),
        ):
            out = json.loads(
                asyncio.run(
                    server.create_workflow(
                        name="MCP " + key, steps=[{"name": "bad", "provider": "mock", key: bad}]
                    )
                )
            )
            check(out["steps_failed"] == 1 and out["steps_added"] == 0, "MCP create refuses " + key)
            try:
                asyncio.run(
                    server.edit_workflow(
                        action="edit_step", workflow_id=wid, step_id=sid, fields={key: bad}
                    )
                )
            except server.ToolError as exc:
                check("400" in str(exc), "MCP edit refusal " + str(exc))
            else:
                check(False, "MCP edit accepted " + key)


if __name__ == "__main__":
    main()
