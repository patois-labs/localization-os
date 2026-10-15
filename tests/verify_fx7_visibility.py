"""FX-7 headless QE history, hermetic through the maintained suite wrapper."""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import verify_qe_autofix as af

# Initialize the fixture's throwaway database before any direct app imports.
assert Path(af.config.DATA_DIR).resolve().is_relative_to(Path(os.environ["TEMP"]).resolve())
af.init_db()

from app.cli.__main__ import main as cli
from app.services import qe_autofix, settings, tap_runs
from app.web.api_cli_misc_1 import _segment_detail_row


def main():
    settings.set_many({"prompt_tap_enabled": "1", "prompt_tap_envelopes_enabled": "1"})
    af.register_provider("afprov", af.AFProvider())
    client = af.TestClient(af.app)
    response = client.post(
        "/projects",
        data={"name": "FX7", "source_locale": "en-US", "target_locales": "fr-FR"},
        follow_redirects=False,
    )
    pid = int(response.headers["location"].rsplit("/", 1)[-1])
    judge = af.engines.create_profile("FX7 judge", "jev_local", base_url="http://127.0.0.1:8077")
    wid = af.make_wf("FX7 history", judge, qe_autofix=True, qe_autofix_cross_check=True)
    did = af.new_doc(client, pid, ["BAD example.", "UNSURE example.", "Clean example."])
    with patch.object(af.qe, "score_segment", wraps=af.qe.score_segment) as cross:
        events = af.run(wid, did)
    assert cross.call_args.kwargs["tap_meta"]["purpose"] == "cross_check"
    assert cross.call_args.kwargs["tap_meta"]["round"] == 1
    assert cross.call_args.kwargs["tap_meta"]["step_name"] == "T"
    rid = next(e["run_id"] for e in events if e.get("type") == "done")
    headers = af.api_headers()
    with patch.object(
        af.engines, "get_profile", side_effect=AssertionError("nested profile session")
    ):
        detail = client.get(
            f"/api/documents/{did}/segments?detail=segments", headers=headers
        ).json()
    rows = detail["locales"]["fr-FR"]["segment_details"]
    fixed = next(f for f in rows[0]["qa_flags"] if f["type"] == "qe_autofix")
    assert [r["score"] for r in fixed["rounds"]] == [60, 100]
    assert fixed["rounds"][1]["cross_check_score"] == 100
    assert fixed["stop_reason"] == "target_reached" and fixed["fixer"]["provider"] == "afprov"
    assert next(f for f in rows[0]["qa_flags"] if f["type"] == "qe")["categories"]
    run = client.get(f"/api/runs/{rid}", headers=headers).json()
    counts = run["qe_steps"][0]
    assert counts["qe"]["scored"] == 3 and counts["qe"]["passed"] == 2
    assert counts["autofix"]["attempted"] == 2 and counts["autofix"]["fixed"] == 1
    assert counts["autofix"]["skipped_by_reason"] == {}
    assert counts["autofix"]["rounds_used"] == 2 and counts["autofix"]["cost_usd"] > 0
    unsure = next(f for f in rows[1]["qa_flags"] if f["type"] == "qe_autofix")
    assert unsure["review_dimensions"] == ["fluency"]
    assert "fluency was unsure" in unsure["message"]
    assert "QE and auto-fix counts" in client.get(f"/runs/{rid}").text
    remote = SimpleNamespace(get=lambda path, **kw: client.get(path, headers=headers, **kw).json())
    for args, expected in (
        (["doc", "show", str(did), "--detail"], detail),
        (["workflow", "show-run", str(rid)], run),
    ):
        for prefix in ([], ["--remote", "http://127.0.0.1"]):
            out = io.StringIO()
            with (
                patch("app.cli._remote_impl_common._client", return_value=remote),
                patch("app.cli._remote_impl_part3._client", return_value=remote),
                patch("app.cli._remote_impl_part4._client", return_value=remote),
                contextlib.redirect_stdout(out),
            ):
                code = cli(["--json", *prefix, *args])
            assert code == 0, (args, prefix, out.getvalue())
            assert json.loads(out.getvalue()) == expected
    mcp_dir = Path(__file__).resolve().parents[1] / "integrations" / "mcp-server"
    sys.path.insert(0, str(mcp_dir))
    spec = importlib.util.spec_from_file_location("fx7_mcp", mcp_dir / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    def local_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=af.app), base_url="http://testserver", headers=headers
        )

    ledger_list = client.get("/api/prompt-tap/runs", headers=headers).json()
    ledger_id = ledger_list["runs"][0]["run_id"]
    ledger = client.get(f"/api/prompt-tap/runs/{ledger_id}", headers=headers).json()
    calls = [
        call for step in ledger["run"]["steps"] for unit in step["units"] for call in unit["calls"]
    ]
    assert {call["purpose"] for call in calls} == {
        "translate",
        "qe judge",
        "auto-fix post-editor",
        "cross-check",
    }
    assert all(step["step"] == "T" for step in ledger["run"]["steps"])
    assert all(
        call["round"] == 1
        for call in calls
        if call["purpose"] in ("auto-fix post-editor", "cross-check")
    )
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli(["prompt-tap", "run", ledger_id]) == 0
    assert "cross-check" in out.getvalue() and "round" in out.getvalue()

    with (
        patch.object(server, "_client", local_client),
        patch.object(server, "TOKEN", headers["X-Loc-Token"]),
    ):
        assert json.loads(asyncio.run(server.work_status(document_id=did, detail=True))) == {
            k: v for k, v in detail.items() if k != "ok"
        }
        assert json.loads(asyncio.run(server.work_status(run_id=rid))) == {
            k: v for k, v in run.items() if k != "ok"
        }
        assert json.loads(asyncio.run(server.ledger_runs(run_id=ledger_id))) == {
            k: v for k, v in ledger.items() if k != "ok"
        }
    for args, expected in (
        (["doc", "show", str(did), "--detail"], "Round 1"),
        (["workflow", "show-run", str(rid)], "attempted=2"),
    ):
        rendered = []
        for prefix in ([], ["--remote", "http://127.0.0.1"]):
            out = io.StringIO()
            with (
                patch("app.cli._remote_impl_part3._client", return_value=remote),
                patch("app.cli._remote_impl_part4._client", return_value=remote),
                contextlib.redirect_stdout(out),
            ):
                assert cli([*prefix, *args]) == 0
            assert expected in out.getvalue(), out.getvalue()
            rendered.append(out.getvalue())
        assert rendered[0] == rendered[1]
    # Durable visit data cannot be rebuilt from today's segment flags.
    with af.Session(af.db_engine) as session:
        for seg in af.segs_for(session, did):
            seg.qa_flags = []
            session.add(seg)
        session.commit()
    assert client.get(f"/api/runs/{rid}", headers=headers).json()["qe_steps"] == run["qe_steps"]
    # Later step totals use only that visit's scored segments; undecided is separate.
    with af.Session(af.db_engine) as session:
        af.workflows.add_step(
            session,
            wid,
            name="Second",
            provider="afprov",
            prompt_text="Translate: {text}",
            qe_gate=True,
            qe_provider=f"engine:{judge.id}",
        )
    did2 = af.new_doc(client, pid, ["BAD example.", "MAYBE example."])
    real_commit = af.orchestrator._db.commit_with_retry

    def retry_step_write(session, *, stage=None, **kwargs):
        if stage is not None and stage.__name__ == "_stage_step_write":
            stage()
            session.rollback()  # reproduce the real retry's expiration of dirty ORM state
        return real_commit(session, stage=stage, **kwargs)

    with patch.object(af.orchestrator._db, "commit_with_retry", side_effect=retry_step_write):
        events2 = af.run(wid, did2)
    rid2 = next(e["run_id"] for e in events2 if e.get("type") == "done")
    visits = client.get(f"/api/runs/{rid2}", headers=headers).json()["qe_steps"]
    assert len(visits) == 2 and visits[1]["step_name"] == "Second"
    assert all(v["qe"]["scored"] == v["qe"]["passed"] == v["qe"]["undecided"] == 1 for v in visits)
    assert visits[0]["autofix"]["fixed"] == 1 and visits[1]["autofix"]["attempted"] == 0
    # Public records are allow-listed, recursively sanitized, and keep stored text caps.
    record = qe_autofix.record(outcome="fixed")
    record["fixer"]["api_key"] = "fixture-credential"  # pregate: allow-secret redaction fixture
    record["rounds"] = [{"round": 1, "text": "x" * 3000}]
    row = _segment_detail_row(
        SimpleNamespace(id=1, order_index=0, status="mt", target_text="ok", qa_flags=[record])
    )
    # pregate: allow-secret assertion against the offline redaction fixture
    assert "fixture-credential" not in json.dumps(row)
    assert len(row["qa_flags"][0]["rounds"][0]["text"]) < 2100
    assert len(record["rounds"][0]["text"]) == 3000  # reading never changes stored records
    keyed = af.engines.create_profile(
        "FX7 keyed fixer",
        "local",
        base_url="http://127.0.0.1:8078",
        api_key="fx7-private-fixture-key",  # pregate: allow-secret offline redaction fixture
    )
    record["fixer"] = {"provider": f"engine:{keyed.id}", "model": "fixture"}
    # pregate: allow-secret echoed offline fixture credential, never a live key
    record["rounds"] = [{"round": 1, "text": "echo fx7-private-fixture-key"}]
    # pregate: allow-secret assertion against the offline redaction fixture
    assert "fx7-private-fixture-key" not in json.dumps(qe_autofix.public_flag(record))
    short_key = "fx7-key1"  # pregate: allow-secret short offline credential for cap regression
    record["rounds"] = [{"round": 1, "text": short_key * 300}]
    with patch.object(af.config, "LOCAL_API_KEY", short_key):
        capped = qe_autofix.public_flag(record)["rounds"][0]["text"]
    assert len(capped) <= qe_autofix.TEXT_CAP + len(" [truncated]")
    call = tap_runs._call_row(
        {"task": "jev_refine", "meta": {"round": 2, "purpose": "qe_autofix"}},
        bodies=False,
        include_key=False,
    )
    assert call["round"] == 2 and call["purpose"] == "auto-fix post-editor"
    assert tap_runs._step_unit({"step_name": "Translate"}, "classic")[0] == "Translate"
    for task, purpose, expected in (
        ("translate", None, "translate"),
        ("qe", None, "qe judge"),
        ("qe", "cross_check", "cross-check"),
    ):
        assert (
            tap_runs._call_row(
                {"task": task, "meta": {"purpose": purpose}}, bodies=False, include_key=False
            )["purpose"]
            == expected
        )
    counts = qe_autofix.visit_counts(
        [{"undecided": True}, {"skipped": True, "reason": "provider_error"}, {"passed": False}],
        [{"outcome": "needs_review", "rounds_used": 2, "cost_usd": 0.25}],
    )
    assert counts["qe"] == {
        "scored": 1,
        "passed": 0,
        "failed": 1,
        "undecided": 1,
        "skipped": 1,
        "skipped_by_reason": {"provider_error": 1},
    }
    assert counts["autofix"]["still_failing"] == 1 and counts["autofix"]["rounds_used"] == 2
    assert not af.FAILURES, af.FAILURES
    print("verify_fx7_visibility OK")


if __name__ == "__main__":
    main()
