"""S4 query diet (ADR 0093): SQL statements per plain translate job stay under budget.

The engine-redesign S4 sprint cut a plain translate job (POST /api/documents/{id}/translate,
mock engine, the load harness's HTML and Markdown documents, three target locales) from about
308 statements (HTML, 12 segments) and 216 (Markdown, 7 segments) to about 138 and 112
(134 and 116 here, with a glossary); the S4 follow-up (ADR 0096) took it to 98 and 83 here by
cutting fixed per-job reads. This suite counts every statement the request and its
jobs run (tests/_stmt_counter.py hooks SQLAlchemy's before_cursor_execute; the suite's own
polling is not counted) and fails when:

  * a job costs more than its budget (the measured number plus headroom), or
  * one MORE segment costs more than PER_SEGMENT_BUDGET statements. That is the N+1 guard:
    the per-segment floor is three statements (the AI opt-out guard's one joined read at the
    engine call, the human-work freshness read of the segment in quality.commit_mt_output,
    the segment UPDATE). A per-segment re-read of the glossary, LLMemory, opt-out rows or the
    job row shows up here at once, whatever the document size.

What the cuts were (each keeps results byte-identical; the existing translate, TM, glossary
and export suites pin that):
  - ai_optout: document -> project -> customer in ONE joined read; providers.run_completion's
    guard hands its fresh answer once to the provider complete() guard it reaches next
  - glossary_common._select_matched_entries / glossary_hub._emit_for_entry and
    llmemory.select_for_segment: a per-run `entry_cache` / `recall_cache`
    (app/services/run_cache.py snapshots) instead of a read per segment
  - translate.pretranslate_stream pins the run's document/project/customer rows
  - translate_hub2._emit builds its event before the commit; quality.commit_mt_output skips
    the targeted status read when the row was just re-loaded in the same call
  - jobs._run_translate_document writes progress at most every _PROGRESS_EVERY_S
  - context_pkg.resolve and style_guides.resolve_style_guide read their layers in one
    statement; lifecycle._all_segments_translated reads two columns in one join
S4 follow-up (ADR 0096), fixed cost per job:
  - the run's read cache is owned by pretranslate_stream and handed to the Prompt Tap
    finalize and enrich, which report the run's own glossary plan, pins, matches, style
    guide, memories and TM sources instead of resolving them again (no replayed sense-choice
    engine call); the leg's segments are read once for the roll-up and the enrich
  - Tap writes are single guarded UPDATEs (run status, run detail, action manifest); the
    action and its legs, and run and action, are one read each
  - document -> project -> customer is one joined read (run_cache.load_document_chain) in
    the job handler, the stream pin and the enrich
  - jobs._update is one conditional UPDATE; the finish notification takes the row the job
    hook just read; lifecycle reads the project and its stopped locales in one statement
  - the translate door reads the project's target locales, the source-segment check and the
    per-language source text once per request, not once per language
  - TM attached sources resolve once for the batched lookup and the few-shot pull
    (tm_common.source_ids_memo); LLMemory reads every in-scope kind once per run

Fully offline: mock engine, a throwaway LOC_TMS_DATA_DIR, auth off.
"""

import os
import random
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_qbudget_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import GlossaryEntry, GlossarySense, Job, JobStatus, Role, Segment
from app.services import apikeys, jobs, security, settings
from tests._stmt_counter import StatementCounter

TARGETS = ["es-ES", "fr-FR", "de-DE"]
# Budgets per job (statements), measured 2026-10-23 on the S4 branch plus headroom. Raise one
# only with a reason in the same change (a new per-job read someone decided is worth it).
HTML_JOB_BUDGET = 110  # measured 98 here (with the glossary fixture); S4 was 134
MD_JOB_BUDGET = 95  # measured 83 here; S4 was 116
PER_SEGMENT_BUDGET = 3.6  # measured 3.25; one more read per segment gives about 4.3
FIXTURE_PASSWORD = "offline-fixture-password-123"  # pregate: allow-secret offline fixture

PRODUCTS = ["Ridgeline 45", "Ember 2 shelter", "Larch fleece", "Summit 30 pack"]
TRAILS = ["Colorado Trail", "Wonderland Trail", "John Muir Trail", "Long Trail"]


def make_html(n, rng, extra=0):
    """The load harness's campaign page (the load harness's make_html)."""
    bulk = "".join(
        f"<p>Note {n}-{i}: field report {i} from the {rng.choice(TRAILS)} with the "
        f"{rng.choice(PRODUCTS)}, logged on day {rng.randint(1, 90)}.</p>"
        for i in range(extra)
    )
    p, q = rng.sample(PRODUCTS, 2)
    t = rng.choice(TRAILS)
    pct, price, days = rng.choice([10, 15, 20]), rng.choice([75, 99, 120]), rng.randint(2, 14)
    return f"""<html><body>
<h1>Campaign {n}: into the backcountry before the snow</h1>
<p>Shorter days, emptier trails. Everything you need for one last long walk of the season.</p>
<p>We packed our <b>{p}</b> and the new <b>{q}</b> into one bundle, tested on the {t}.</p>
<p>Save {pct}% on the bundle until November 30. Free shipping on orders over ${price}.</p>
<p>Hi {{first_name}}, your order ships in {days} business days.</p>
<ul><li>Warm when wet, packs into its own pocket.</li><li>Two doors, pitches fast.</li>
<li>A down jacket rated to -5 C for camp.</li></ul>
<p>Read the packing list or ask our guides on live chat.</p>
{bulk}
<p><a href="https://example.com/fall">Shop the fall bundle</a></p>
</body></html>"""


def make_md(n, rng):
    """The load harness's packing list (the load harness's make_md)."""
    p = rng.choice(PRODUCTS)
    return f"""# Packing list {n}

What our guides carry when the nights drop below freezing.

## Layers that earn their weight

1. **{p}**: {rng.randint(300, 1600)} g, tested for {rng.randint(3, 40)} days on the trail.
2. Pack your stove fuel in the lid so it stays warm.
3. Start walking before 8:00 AM to finish in daylight.

[Get the bundle](https://example.com/bundle)
"""


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def main() -> None:
    init_db()
    settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
    settings.invalidate_cache()
    # Progress writes are time-throttled; a slow box would add writes the code did not
    # choose to make. The guard measures the code, so the clock is taken out.
    jobs._PROGRESS_EVERY_S = 3600.0
    user = security.create_user("qbudget@example.test", "Q Budget", Role.ADMIN, FIXTURE_PASSWORD)
    key = apikeys.create_key(user.id, "qbudget", scopes="api docs")[1]
    client = TestClient(app)
    h = {"X-Loc-Token": key}
    counter = StatementCounter(engine)

    cust = (client.post("/api/customers", json={"name": "Budget Co"}, headers=h).json() or {}).get(
        "id"
    ) or 1
    r = client.post(
        "/api/projects",
        headers=h,
        json={
            "name": "Budget",
            "customer_id": cust,
            "source_locale": "en-US",
            "targets": ",".join(TARGETS),
            "provider": "mock",
        },
    )
    if r.status_code >= 300:
        _fail(f"project create: {r.status_code} {r.text[:200]}")
    pid = r.json()["id"]
    # A populated glossary (gate L1): "field report" matches every bulk segment of the big
    # document (the per-run entry cache must keep the marginal cost flat), "bundle" matches
    # the fixed text and carries a sense (the per-run senses cache is exercised).
    with Session(engine) as s:
        s.add(
            GlossaryEntry(
                project_id=pid,
                source_locale="en-US",
                target_locale="",
                source_term="field report",
                target_term="informe de campo",
            )
        )
        bundle = GlossaryEntry(
            project_id=pid,
            source_locale="en-US",
            target_locale="",
            source_term="bundle",
            target_term="lote",
        )
        s.add(bundle)
        s.commit()
        s.refresh(bundle)
        s.add(
            GlossarySense(
                glossary_entry_id=bundle.id,
                sense_label="retail",
                context_cue="a set of products sold together",
                target_term="paquete",
            )
        )
        s.commit()

    rng = random.Random(7)
    bodies = {
        "warm": ("warm.html", make_html(1, rng)),
        "html": ("campaign.html", make_html(2, rng)),
        "md": ("packing.md", make_md(3, rng)),
        "big": ("campaign-big.html", make_html(4, rng, extra=20)),
    }
    docs = {}
    for name, (fname, body) in bodies.items():
        u = client.post(
            "/api/documents",
            headers=h,
            data={"project_id": str(pid)},
            files={"file": (fname, body.encode())},
        )
        if u.status_code >= 300:
            _fail(f"upload {fname}: {u.status_code} {u.text[:200]}")
        docs[name] = u.json()["document_id"]

    def wait(job_ids):
        deadline = time.time() + 120
        with counter.paused():
            while time.time() < deadline:
                with Session(engine) as s:
                    rows = [s.get(Job, j) for j in job_ids]
                    if all(
                        j is not None and j.status in (JobStatus.COMPLETED, JobStatus.FAILED)
                        for j in rows
                    ):
                        return [j.status for j in rows]
                time.sleep(0.02)
        _fail(f"jobs {job_ids} did not finish")

    def translate(doc_id):
        with counter.measure() as m:
            t = client.post(
                f"/api/documents/{doc_id}/translate", headers=h, json={"targets": TARGETS}
            )
            if t.status_code >= 300:
                _fail(f"translate {doc_id}: {t.status_code} {t.text[:200]}")
            job_ids = [j["job_id"] for j in t.json()["jobs"]]
            statuses = wait(job_ids)
        if any(st != JobStatus.COMPLETED for st in statuses):
            _fail(f"translate {doc_id} did not complete: {statuses}")
        return m, len(job_ids)

    def segments_per_locale(doc_id):
        with counter.paused(), Session(engine) as s:
            rows = s.exec(
                select(Segment.id).where(
                    Segment.document_id == doc_id, Segment.target_locale == TARGETS[0]
                )
            ).all()
        return len(rows)

    # Settle the process caches the run reads through (project create bumps the settings
    # version, which drops the settings cache at the next probe), then warm every first-use
    # read with one throwaway translation.
    with counter.paused():
        settings._checked_at = -1e9
        settings.get("pii_redaction")
    translate(docs["warm"])

    results = {}
    for name in ("html", "md", "big"):
        m, n_jobs = translate(docs[name])
        results[name] = (m.total / n_jobs, segments_per_locale(docs[name]), m)
        print(
            f"  {name}: {m.total} statements for {n_jobs} jobs = {m.total / n_jobs:.1f} per job "
            f"({results[name][1]} segments)"
        )

    def _top(m):
        return "\n".join(f"    {n:4d}  {sql[:150]}" for sql, n in m.top(15))

    per_job, _segs, m = results["html"]
    if per_job > HTML_JOB_BUDGET:
        _fail(f"HTML job: {per_job:.1f} statements > budget {HTML_JOB_BUDGET}\n{_top(m)}")
    print(f"  HTML job within budget: {per_job:.1f} <= {HTML_JOB_BUDGET} OK")
    per_job, _segs, m = results["md"]
    if per_job > MD_JOB_BUDGET:
        _fail(f"Markdown job: {per_job:.1f} statements > budget {MD_JOB_BUDGET}\n{_top(m)}")
    print(f"  Markdown job within budget: {per_job:.1f} <= {MD_JOB_BUDGET} OK")

    small, small_segs, _m = results["html"]
    big, big_segs, m_big = results["big"]
    if big_segs <= small_segs:
        _fail(f"the big document has {big_segs} segments, the small one {small_segs}")
    marginal = (big - small) / (big_segs - small_segs)
    if marginal > PER_SEGMENT_BUDGET:
        _fail(
            f"each extra segment costs {marginal:.2f} statements > {PER_SEGMENT_BUDGET} "
            f"(an N+1 crept in)\n{_top(m_big)}"
        )
    print(f"  per extra segment: {marginal:.2f} <= {PER_SEGMENT_BUDGET} statements OK")
    counter.close()
    print("verify_query_budget: all checks OK")


if __name__ == "__main__":
    main()
