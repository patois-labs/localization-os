"""Prompt Tap pages render every call outcome shape (main-red fix, 2026-10-06).

Regression: issue #549 changed the shared `status_badge` macro title to
`(lbl|striptags) if lbl.__html__ is defined else lbl`. A Ledger call record with NO
`outcome` key (a sparse or legacy line) makes the tap templates pass an Undefined label,
and reading `.__html__` off an Undefined raised UndefinedError, so /prompt-tap (and a
run's detail page) returned 500 "'dict object' has no attribute 'outcome'".

Seeds REAL rows through the capture path (ok, refused-by-spend-cap, provider-error) plus a
raw outcome-less line in a run, in an action's orphan bucket and in the legacy flat file,
then renders /prompt-tap, /prompt-tap/runs and /prompt-tap/runs/{id}: every page is 200
and each outcome shows the same badge it showed before the UX overhaul (an outcome-less
call keeps its empty danger badge). Also renders the label-bearing macros directly with an
undefined label. Hermetic (throwaway LOC_TMS_DATA_DIR); ASCII prints.
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tap_outcome_")

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import TapAction, TapActionKind, TapRun, TapStatus
from app.services import prompt_tap, security, settings, tap_envelope
from tests._csrf import csrf_for

OUTCOMES = ("ok", "refused-by-spend-cap", "provider-error")
BADGE_OK = '<span class="badge exact" title="ok">ok</span>'
BADGE_CAP = '<span class="badge danger" title="refused (spend cap)">refused (spend cap)</span>'
BADGE_ERR = '<span class="badge danger" title="provider-error">provider-error</span>'
# What the pre-overhaul macro rendered for an outcome-less call (label Undefined -> "").
BADGE_NONE = '<span class="badge danger" title=""></span>'
BADGE_NONE_RUN = '<span class="badge danger" title="error">error</span>'


def _login(c):
    csrf_for(c)
    r = c.post(
        "/auth/login",
        data={"email": "admin", "password": "admin", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, ("admin login", r.status_code)
    csrf_for(c)


def _rec(outcome, prompt, meta):
    return prompt_tap.build_record(
        provider="mock",
        provider_kind="mock",
        model="m",
        task="t",
        system="s",
        prompt=prompt,
        max_tokens=8,
        payload={"text": "x"},
        outcome=outcome,
        response_text="y" if outcome == "ok" else None,
        error=None if outcome == "ok" else "boom",
        cost=0.0,
        tap_meta=meta,
    )


def _append_raw(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.write((json.dumps(rec) + "\n").encode("utf-8"))


def seed():
    settings.set_many({"prompt_tap_enabled": "1", "prompt_tap_envelopes_enabled": "1"})
    aid, rid = tap_envelope.new_id(), tap_envelope.new_id()
    with Session(engine) as s:
        s.add(
            TapAction(
                id=aid,
                kind=TapActionKind.DIRECT,
                requested_locales=["es-ES"],
                status=TapStatus.COMPLETED,
            )
        )
        s.commit()
        s.add(TapRun(id=rid, action_id=aid, target_locale="es-ES", status=TapStatus.COMPLETED))
        s.commit()
    for oc in OUTCOMES:
        prompt_tap.capture(_rec(oc, "run-" + oc, {"action_id": aid, "run_id": rid}))
        prompt_tap.capture(_rec(oc, "orphan-" + oc, {"action_id": aid}))
        prompt_tap.capture(_rec(oc, "flat-" + oc, None))
    # Sparse lines with no `outcome` key: in the run, in the orphan bucket, in the flat file.
    _append_raw(
        prompt_tap._action_path(aid), {"request": {}, "meta": {"action_id": aid, "run_id": rid}}
    )
    _append_raw(prompt_tap._action_path(aid), {"request": {}, "meta": {"action_id": aid}})
    _append_raw(prompt_tap._path(), {"ts": "2026-10-06T00:00:00+00:00", "request": {}})
    return aid, rid


def _page(c, url):
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[-600:])
    return r.text


def test_pages_render_every_outcome():
    _aid, rid = seed()
    c = TestClient(app)
    _login(c)
    page = _page(c, "/prompt-tap")
    for badge in (BADGE_OK, BADGE_CAP, BADGE_ERR, BADGE_NONE):
        assert badge in page, ("/prompt-tap badge missing", badge)
    print("  (LIST) /prompt-tap renders ok, spend-cap, provider-error and outcome-less calls OK")

    runs = _page(c, "/prompt-tap/runs")
    assert rid in runs, "the seeded run must be listed on /prompt-tap/runs"
    print("  (RUNS) /prompt-tap/runs lists the seeded run OK")

    for url in (f"/prompt-tap/runs/{rid}", f"/prompt-tap/runs/{rid}?bodies=1"):
        detail = _page(c, url)
        # The run view normalizes a missing outcome to None (tap_runs), so that call shows
        # the plain "error" badge, as it did before the overhaul.
        for badge in (BADGE_OK, BADGE_CAP, BADGE_ERR, BADGE_NONE_RUN):
            assert badge in detail, (url, "badge missing", badge)
    print("  (RUN) /prompt-tap/runs/{id} renders every outcome (digest and bodies) OK")


def test_macros_accept_undefined_label():
    env = Environment(
        loader=FileSystemLoader(os.path.join(_ROOT, "app", "templates")),
        autoescape=select_autoescape(),
    )
    env.globals["static_v"] = lambda name: "fixture"

    def render(src, **ctx):
        return env.from_string("{% import '_macros.html' as m %}" + src).render(**ctx)

    row = {"status": "error"}  # a dict row with no `label` key -> Undefined
    badge = render("{{ m.status_badge(row.status, row.label) }}", row=row)
    assert badge == BADGE_NONE, badge
    assert 'aria-label=""' in render("{{ m.action_bar(row.label) }}", row=row)
    assert 'aria-label=""' in render("{{ m.step_indicator(row.label, []) }}", row=row)
    assert 'aria-label=""' in render("{% call m.data_table(row.label, []) %}{% endcall %}", row=row)
    print("  (MACRO) badge/action_bar/step_indicator/data_table take an undefined label OK")


def main():
    init_db()
    security.seed_initial()
    test_pages_render_every_outcome()
    test_macros_accept_undefined_label()
    print("PROMPT TAP OUTCOME RENDER OK")


if __name__ == "__main__":
    main()
