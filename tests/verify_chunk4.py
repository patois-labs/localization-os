"""Chunk 4 verification — provider layer + MT pre-translation, offline.

- run_completion returns text via the mock provider
- failover: a deliberately failing primary falls over to the backup model
- segmented pretranslate: 100% TM short-circuit, glossary constraints honored,
  plain segments translated; provider_calls counted correctly
- full-text TM: first run calls the provider; an identical doc returns from
  Full-Text TM with ZERO provider calls
- the SSE /translate-stream endpoint streams per-segment events + a summary
"""

import os
import sys
from io import BytesIO

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.main import app
from app.models import Segment
from app.services import settings, translate
from app.services.providers import (
    Provider,
    ProviderSpec,
    register_provider,
    run_completion,
)

TMX = """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4"><header srclang="en-US"/><body>
  <tu><tuv xml:lang="en-US"><seg>Adventure awaits.</seg></tuv>
      <tuv xml:lang="fr-FR"><seg>L'aventure vous attend.</seg></tuv></tu>
</body></tmx>"""

GLOSSARY = "source_term,target_term,dnt,sense_label,context_cue\ncheckout,caisse,,,\n"


class FailingProvider(Provider):
    name = "failer"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        FailingProvider.calls += 1
        raise RuntimeError("simulated provider outage")


class CountingMock(Provider):
    """Wraps the mock translate behavior but counts calls."""

    name = "counter"
    calls = 0

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        CountingMock.calls += 1
        from app.services.providers import MockProvider

        return MockProvider().complete(prompt, system=system, task=task, payload=payload, **kw)


def docx_from(paragraphs) -> bytes:
    d = Docx()
    for p in paragraphs:
        d.add_paragraph(p)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def new_project(client, name):
    r = client.post(
        "/projects",
        data={
            "name": name,
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    return int(r.headers["location"].rsplit("/", 1)[-1])


def main() -> None:
    init_db()
    client = TestClient(app)

    # --- run_completion via mock ---
    # Keyless "claude" with MOCK_FALLBACK_OK resolves to the offline MockProvider, and
    # run_completion records the substitution HONESTLY as provider_used="mock" (F6) so
    # the flag mode can never reproduce the mislabeling that hid the 2026-08-07 incident.
    res = run_completion(ProviderSpec(provider="claude", model="m"), "hello", task=None)
    assert res["text"] == "hello" and res["provider_used"] == "mock", res
    print("  run_completion via mock OK (keyless 'claude' honestly recorded as 'mock')")

    # --- failover ---
    register_provider("failer", FailingProvider())
    spec = ProviderSpec(
        provider="failer",
        model="m1",
        fallback_provider="mock",
        fallback_model="m2",
        retry_max=1,
        backoff_base=0,
    )
    res = run_completion(spec, "hi", task=None)
    assert res["provider_used"] == "mock", f"expected failover to mock, got {res}"
    assert res["attempts"] == 3, (
        f"expected 2 failed + 1 success = 3 attempts, got {res['attempts']}"
    )
    assert FailingProvider.calls == 2, FailingProvider.calls
    print(f"  failover OK (attempts={res['attempts']}, primary tried {FailingProvider.calls}x)")

    # --- stats['failovers'] actually increments on a real pretranslate run ---
    # (ENTERPRISE_ROADMAP "known stats bug": the per-segment path's ProviderSpec
    # never carried a fallback_provider, so run_completion's failover branch was
    # unreachable here even though it worked in isolation above. A project whose
    # primary provider ("failer") always errors, with default_fallback_provider
    # set to "mock", must now fail over per segment and count each one.)
    settings.set_many({"default_fallback_provider": "mock"})
    r = client.post(
        "/projects",
        data={
            "name": "Chunk4 Failover",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "failer",
        },
        follow_redirects=False,
    )
    pid_fo = int(r.headers["location"].rsplit("/", 1)[-1])
    FailingProvider.calls = 0
    r = client.post(
        f"/projects/{pid_fo}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "fo.docx",
                docx_from(["First.", "Second."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    fo_doc = int(r.headers["location"].rsplit("/", 1)[-1])
    with Session(engine) as s:
        fo_stats = translate.pretranslate_document(s, fo_doc, "fr-FR")
    assert fo_stats["failovers"] == 2, f"expected 2 failovers (one per segment), got {fo_stats}"
    assert fo_stats["provider_used"].get("mock") == 2, fo_stats["provider_used"]
    assert fo_stats["translated"] == 2, fo_stats
    settings.set_many({"default_fallback_provider": ""})  # reset for other suites/projects
    print(
        f"  project-level failover: stats['failovers']={fo_stats['failovers']} "
        "(primary 'failer' errored, fell over to mock per segment)"
    )

    # --- segmented pretranslate ---
    pid = new_project(client, "Chunk4 Segmented")
    client.post(
        f"/projects/{pid}/tm/import",
        data={"source_locale": "en-US", "target_locale": "fr-FR"},
        files={"file": ("m.tmx", TMX.encode(), "application/xml")},
    )
    client.post(
        f"/projects/{pid}/glossary/import",
        data={"source_locale": "en-US", "target_locale": "fr-FR"},
        files={"file": ("g.csv", GLOSSARY.encode(), "text/csv")},
    )
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                docx_from(["Adventure awaits.", "Use code at checkout.", "A plain sentence."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    seg_doc = int(r.headers["location"].rsplit("/", 1)[-1])

    with Session(engine) as s:
        stats = translate.pretranslate_document(s, seg_doc, "fr-FR")
    assert stats["from_segment_tm"] == 1, stats
    assert stats["provider_calls"] == 2, stats
    assert stats["translated"] == 3, stats

    with Session(engine) as s:
        segs = {
            x.order_index: x
            for x in s.exec(
                select(Segment).where(
                    Segment.document_id == seg_doc, Segment.target_locale == "fr-FR"
                )
            ).all()
        }
    assert segs[0].target_text == "L'aventure vous attend.", segs[0].target_text
    assert "caisse" in segs[1].target_text, segs[1].target_text  # glossary applied
    assert segs[2].target_text.startswith("[fr-FR]"), segs[2].target_text  # mock MT
    print(
        f"  segmented: TM={stats['from_segment_tm']} MT={stats['provider_calls']} (glossary honored)"  # noqa: E501
    )

    # --- full-text TM short-circuit ---
    pid2 = new_project(client, "Chunk4 FullText")
    body = docx_from(["The whole document body for full text mode."])
    r = client.post(
        f"/projects/{pid2}/documents",
        data={"mode": "full_text"},
        files={
            "file": (
                "a.docx",
                body,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    doc_a = int(r.headers["location"].rsplit("/", 1)[-1])
    r = client.post(
        f"/projects/{pid2}/documents",
        data={"mode": "full_text"},
        files={
            "file": (
                "b.docx",
                body,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    doc_b = int(r.headers["location"].rsplit("/", 1)[-1])

    register_provider("claude", CountingMock())  # count provider calls for this project
    with Session(engine) as s:
        st_a = translate.pretranslate_document(s, doc_a, "fr-FR")
        st_b = translate.pretranslate_document(s, doc_b, "fr-FR")
    assert st_a["provider_calls"] == 1 and st_a["from_full_text_tm"] == 0, st_a
    assert st_b["provider_calls"] == 0 and st_b["from_full_text_tm"] == 1, st_b
    assert CountingMock.calls == 1, (
        f"provider should be called once total, got {CountingMock.calls}"
    )
    print(
        f"  full-text TM: doc A provider call, doc B from memory (provider hits={CountingMock.calls})"  # noqa: E501
    )

    # --- SSE stream endpoint ---
    pid3 = new_project(client, "Chunk4 SSE")
    r = client.post(
        f"/projects/{pid3}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "s.docx",
                docx_from(["One.", "Two."]),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    sse_doc = int(r.headers["location"].rsplit("/", 1)[-1])
    body = client.post(f"/documents/{sse_doc}/translate-stream", data={"locale": "fr-FR"}).text
    assert (
        '"type": "segment"' in body and '"type": "summary"' in body and '"type": "done"' in body
    ), body[:300]
    print("  SSE stream emits segment + summary + done events")

    print("CHUNK 4 OK")


if __name__ == "__main__":
    main()
