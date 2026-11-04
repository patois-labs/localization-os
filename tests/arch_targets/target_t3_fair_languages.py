"""TARGET T3: languages and projects are served fairly when engine slots are scarce.

Why (load test, 2026-10-20): under load our Spanish finished in 81 s, French in 174 s and German in 258 s
because a batch's locales lined up behind each other for engine slots; a queue-based design finished all three
languages within 13 s of each other. A customer waits on the LAST language, so the spread is what they feel.

Setup (flood conditions, scaled down): 12 documents arriving one every 0.25 s, each fanned out to 3 locales with
run-batch; every run makes 3 engine calls (3 AI steps) through one engine that serves 3 calls at once (slow mock,
0.25 s per call); the global run cap is 6, so runs queue. Each run ends at a human review step; we record when.

Done when every check passes:
  1. Per document, the gap between its first and last locale reaching review is at most 2 run-lengths
     (p95 over documents).
  2. No locale is systematically last: the mean time-to-review of the slowest locale is within 25 % of the
     fastest locale's mean.
  3. Documents are not starved: the last document's first locale reaches review no later than
     (documents x locales / slots + 2) run-lengths after launch.
"""

import os
import secrets
import statistics
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._bootstrap import init_env

os.environ["LOC_TMS_MAX_CONCURRENT_RUNS"] = "6"
init_env("loctms_t3_")

from _targets import check, finish, pct, wait_until  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402

init_db()
from app.main import app  # noqa: E402
from app.models import Role  # noqa: E402
from app.services import apikeys, engines, providers_hub, security  # noqa: E402

CALL_S = 0.25
SLOTS = 3
DOCS = 12
STEPS = 3
ARRIVAL_S = 0.25
LOCALES = ["es-ES", "fr-FR", "de-DE"]
_inflight = threading.Semaphore(SLOTS)
CALLS = []


class SlowCapped(providers_hub.MockProvider):
    name = "slowcapped"

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        CALLS.append(time.time())
        with _inflight:  # the engine itself only serves SLOTS calls at once
            time.sleep(CALL_S)
        return super().complete(prompt, system=system, task=task, payload=payload, images=images, **kw)


prof = engines.create_profile("T3 capped", "local", base_url="http://127.0.0.1:9/v1", api_key="x", model="m",
                              cfg={"max_parallel_jobs": SLOTS}, actor="target")
ENGINE = f"engine:{prof.id}"
providers_hub.register_provider(ENGINE, SlowCapped())
u = security.create_user("pm@target.test", "Target PM", Role.ADMIN, secrets.token_urlsafe(24))
_row, KEY = apikeys.create_key(u.id, name="t3", scopes="api docs")
# The poll reads every run several times a second; the per-key API rate limit would answer 429
# for most of a minute on a fast box and stall the measurement, not the runs (as in the load
# harness, which turns it off).
from app.services import settings as _settings  # noqa: E402

_settings.set_many({"api_rate_limit_per_minute": "0", "api_read_rate_limit_per_minute": "0"}, actor="target")
c = TestClient(app, headers={"X-Loc-Token": KEY})


def ok(r):
    assert r.status_code < 300, f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}"
    return r.json()


cust = ok(c.post("/api/customers", json={"name": "T3 Co"}))["id"]
pid = ok(c.post("/api/projects", json={"name": "t3", "customer_id": cust, "source_locale": "en-US",
                                        "targets": ",".join(LOCALES), "provider": "mock"}))["id"]
wid = ok(c.post("/api/workflows", json={"name": "T3", "customer_id": cust, "scope": "customer"}))["id"]
for k in range(STEPS):
    ok(c.post(f"/api/workflows/{wid}/steps", json={"name": f"AI step {k}", "step_type": "llm_agent", "provider": ENGINE,
                                                    "prompt_text": f"Pass {k}: rewrite in {{target_locale}}:\n{{text}}"}))
ok(c.post(f"/api/workflows/{wid}/steps", json={"name": "Review", "step_type": "human_review",
                                                "assignee_email": "pm@target.test"}))
docs = []
for i in range(DOCS):
    body = f"<html><body><p>Campaign {i}: shorter days, emptier trails.</p></body></html>".encode()
    docs.append(ok(c.post("/api/documents", data={"project_id": str(pid)},
                          files={"file": (f"t3-{i}.html", body)}))["document_id"])

T0 = time.time()
runs = {}  # run_id -> (doc, locale)
launched = {}  # doc -> launch time
reached = {}


def launcher():
    for d in docs:
        launched[d] = time.time() - T0
        b = ok(c.post(f"/api/workflows/{wid}/run-batch", json={"doc_id": d, "locales": LOCALES, "max_parallel": 3}))
        for m in b["members"]:
            runs[m["run_id"]] = (d, m["locale"])
        time.sleep(ARRIVAL_S)


launcher_thread = threading.Thread(target=launcher, daemon=True)
launcher_thread.start()


def poll():
    if launcher_thread.is_alive():
        time.sleep(0.05)
    for rid in list(runs):
        if rid in reached:
            continue
        st = c.get(f"/api/runs/{rid}").json()
        s = (st.get("status") or "").lower()
        if s in ("paused", "completed", "failed", "cancelled"):
            reached[rid] = (time.time() - T0, s)
    return (not launcher_thread.is_alive()) and len(reached) == len(runs) == DOCS * len(LOCALES)


wait_until(poll, 300, every=0.1)
RUN_LEN = STEPS * CALL_S  # engine time of one run


def spreads():
    out = []
    for d in docs:
        ts = [reached[r][0] for r, (dd, _) in runs.items() if dd == d and r in reached]
        if len(ts) == len(LOCALES):
            out.append(max(ts) - min(ts))
    return out


def t_spread():
    sp = spreads()
    p95 = pct(sp, 0.95)
    return (p95 is not None and p95 <= 2 * RUN_LEN + 0.3), \
        f"per-document locale spread p95={p95 and round(p95, 2)}s (limit {2 * RUN_LEN + 0.3:.1f}s); all={[round(x, 2) for x in sp]}"


def t_no_locale_last():
    # time from the document's launch to review, averaged per locale
    by = {loc: statistics.mean([reached[r][0] - launched[d] for r, (d, l) in runs.items() if l == loc and r in reached]
                               or [0]) for loc in LOCALES}
    lo, hi = min(by.values()), max(by.values())
    return (lo > 0 and hi <= lo * 1.25), f"mean time-to-review by locale: { {k: round(v, 2) for k, v in by.items()} }"


def t_no_starvation():
    firsts = []
    for d in docs:
        ts = [reached[r][0] - launched[d] for r, (dd, _) in runs.items() if dd == d and r in reached]
        firsts.append(min(ts) if ts else 999)
    limit = (DOCS * len(LOCALES) / SLOTS + 2) * RUN_LEN + 0.5
    errs = [v for v in reached.values() if v[1] != "paused"]
    return (max(firsts) <= limit and not errs), \
        f"last document's first locale at {max(firsts):.2f}s (limit {limit:.1f}s); non-paused runs={len(errs)}"


print(f"  engine calls made: {len(CALLS)} (expected {DOCS * len(LOCALES) * STEPS})")
_one = c.get(f"/api/runs/{next(iter(runs))}").json()
print("  sample run step_states:", [(s.get("index"), s.get("status"), s.get("tokens_in")) for s in _one.get("step_states") or []], (_one.get("notes") or "")[:300], _one.get("warnings"))
check("1 per-document language spread", t_spread)
check("2 no language systematically last", t_no_locale_last)
check("3 no document starved", t_no_starvation)
finish("T3", "fair languages")
