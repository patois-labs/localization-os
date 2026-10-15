"""Chunk 3 verification — glossary, offline (MockProvider).

- import a glossary CSV with a base term, a DNT term, and a polysemous term
  that has two context senses
- context-dependent sense selection picks the right sense per segment
- match_terms surfaces DNT and base terms too
- the document grid renders term hits
- the LLM glossary builder proposes candidate terms
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
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.services import glossary

# polysemous "bank": finance vs river; plus a brand DNT and a base term
CSV = (
    "source_term,target_term,dnt,sense_label,context_cue\n"
    "Acme,Acme,true,,\n"  # do-not-translate brand
    "checkout,validation,,,\n"  # base term
    "bank,banque,,finance,money account deposit savings\n"
    "bank,rive,,river,river water shore stream\n"
)


def make_docx() -> bytes:
    d = Docx()
    d.add_paragraph("Deposit your savings at the bank with Acme.")  # finance sense
    d.add_paragraph("We sat by the bank of the river to watch the water.")  # river sense
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def main() -> None:
    init_db()
    client = TestClient(app)

    r = client.post(
        "/projects",
        data={
            "name": "Gloss Test",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "claude",
        },
        follow_redirects=False,
    )
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    # --- import glossary ---
    r = client.post(
        f"/projects/{pid}/glossary/import",
        data={"source_locale": "en-US", "target_locale": "fr-FR"},
        files={"file": ("g.csv", CSV.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert "gl_entries=3" in loc and "gl_senses=2" in loc, loc
    print("  imported 3 terms, 2 senses")

    # --- context-dependent sense selection ---
    with Session(engine) as s:
        fin = glossary.match_terms(
            s, pid, "Deposit your savings at the bank with Acme.", "en-US", "fr-FR"
        )
        by_term = {t["source_term"]: t for t in fin}
        assert by_term["bank"]["sense_label"] == "finance", by_term["bank"]
        assert by_term["bank"]["target_term"] == "banque"
        assert by_term["Acme"]["dnt"] is True, "DNT term not flagged"
        print(
            f"  finance segment -> bank={by_term['bank']['target_term']} ({by_term['bank']['sense_label']}), Acme dnt={by_term['Acme']['dnt']}"  # noqa: E501
        )

        riv = glossary.match_terms(
            s, pid, "We sat by the bank of the river to watch the water.", "en-US", "fr-FR"
        )
        bank2 = {t["source_term"]: t for t in riv}["bank"]
        assert bank2["sense_label"] == "river", bank2
        assert bank2["target_term"] == "rive"
        print(f"  river segment   -> bank={bank2['target_term']} ({bank2['sense_label']})")

    # --- grid renders term hits ---
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={
            "file": (
                "d.docx",
                make_docx(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    doc_id = int(r.headers["location"].rsplit("/", 1)[-1])
    page = client.get(f"/documents/{doc_id}?locale=fr-FR").text
    assert "banque" in page and "rive" in page, "context senses not shown in grid"
    assert "🔒" in page or "dnt" in page, "DNT term not shown in grid"
    print("  grid shows context-correct senses + DNT")

    # --- glossary builder ---
    suggestions = glossary.build_from_text(
        "Acme Studio ships Acme widgets. Smartling and Acme integrate well."
    )
    terms = {x["source_term"] for x in suggestions}
    assert "Acme" in terms, f"builder missed obvious term: {terms}"
    print(f"  builder proposed: {sorted(terms)}")

    # --- BUG-3: in-app one-click "Add term" ---
    with Session(engine) as s:
        before_n = glossary.count(s, pid)
    r = client.post(
        f"/projects/{pid}/glossary/add",
        data={
            "source_term": "widget",
            "target_term": "gadget",
            "target_locale": "fr-FR",
            "dnt": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "gl_added=1" in r.headers["location"], r.headers
    with Session(engine) as s:
        assert glossary.count(s, pid) == before_n + 1, "add_term did not persist"
        hit = {
            t["source_term"]: t
            for t in glossary.match_terms(s, pid, "Use the widget carefully.", "en-US", "fr-FR")
        }
        assert hit.get("widget", {}).get("target_term") == "gadget", hit
    # the persisted term now shows in the project glossary list
    proj_page = client.get(f"/projects/{pid}").text
    assert "widget" in proj_page and "gadget" in proj_page, "added term not listed on project page"
    # a blank source term is rejected (no phantom row created)
    r = client.post(
        f"/projects/{pid}/glossary/add",
        data={"source_term": "  ", "target_locale": "fr-FR"},
        follow_redirects=False,
    )
    assert "err=glossary_add" in r.headers["location"], r.headers
    with Session(engine) as s:
        assert glossary.count(s, pid) == before_n + 1, "blank term should not persist"
    print("  add-term route persists a single term (shows in glossary; blank rejected)")

    print("CHUNK 3 OK")


if __name__ == "__main__":
    main()
