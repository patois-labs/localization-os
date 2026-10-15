"""ATTACH AN ALREADY-ESTABLISHED TM / GLOSSARY TO A PROJECT (feedback item 5).

2026-08-03: the project panel could only IMPORT a TMX/CSV/TBX *file*. If the
memory or terminology already existed in this install there was no way to point at it.
Two link tables now exist, and they use two DIFFERENT mechanisms on purpose:

  * ProjectTmSource is a REFERENCE. There is no first-class "a TM" entity, so attaching
    cannot mean "add this TM"; it means "when THIS project looks up, ALSO match that
    project's rows". Realised by widening the ONE scope seam every lookup path funnels
    through (tm._tm_read_scope).
  * ProjectGlossary is a PIN of an existing module Glossary, unioned into
    glossary_module.build_glossary_plan.

GROUPS

  [A] THE MATCHING PAYOFF, AND ITS EXACT REVERSAL. All five read paths -- 100% exact
      short-circuit, fuzzy, the batch path, TM few-shot examples and full-text
      -- start returning the attached project's rows, and the
      WRITE side does not move: count/ft_count/TMX export/write-back stay own-project,
      so a project never starts writing into or exporting somebody else's memory.
      Then DETACH, and every one of those paths is compared BYTE FOR BYTE against a
      capture taken BEFORE the attach. That capture is the point of the whole design:
      because nothing is copied, undo is exact, which is what makes this safe to try
      on real data.

  [B] THE LINK IS A HINT, NOT AN AUTHORITY -- the isolation half, and the one the
      reviewers should attack. Refusals at the write door are the easy part (self,
      cross-customer, cross-org, duplicate). The load-bearing cells are the ones where
      the write door is BYPASSED ENTIRELY: a ProjectTmSource row is INSERTed straight
      into the database pointing at another customer's / another org's project, exactly
      as an attacker with a write primitive on the link table would, and the read path
      still returns ZERO of that project's rows -- because tm._tm_source_project_ids
      re-derives what a link may reach from the LIVE Project rows through the same
      access predicate the write door uses, on every single lookup. [B7] proves the
      same machinery makes a link go inert the moment its source is reassigned, with no
      cleanup job. [B8] proves attachment is ONE LEVEL: A->B->C leaves C unreachable
      from A, so the reachable set stays bounded and auditable from the project page.

  [C] GLOSSARY PINS. An approved pin reaches match_terms on an UNTYPED project (the
      thing implicit auto-attach deliberately never does); a DRAFT pin contributes
      NOTHING until approved, so a pin is not a way past review; a step-level pin still
      wins as an exclusive override; and a TYPED project's implicit auto-attach plan is
      captured BEFORE the feature can touch it and compared byte-for-byte with no pins
      present -- the byte-identical gate rule 2 rests on.

  [D] THE WEB SURFACE (auth ON). Attach/detach round-trip for both kinds; a VIEWER sees
      the current lists but is offered no form and is refused at the POST; and the
      candidate dropdowns are filtered SERVER-SIDE -- a foreign customer's project is
      neither offered nor accepted if submitted anyway.

  [E] THE CLI SURFACE. Both verb families round-trip in-process, agree with the web on
      what they refuse, and print ASCII only.

  [F] THE TEMPLATE RIDER. Documents renders ABOVE Members on the project page.

  [G] THE MIGRATION. Built to the previous head and to the new head and diffed: the
      revision adds exactly two tables and their six indexes and NOTHING else; then it
      downgrades cleanly back.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR via tests._bootstrap.init_env; provider
keys blanked). ASCII-only output. Final line: "PROJECT SOURCES OK".
"""

import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_projsrc_")

from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Customer,
    Glossary,
    Organization,
    Project,
    ProjectGlossary,
    ProjectTmSource,
    Role,
    SegmentTM,
)
from app.services import (
    access,
    project_sources,
    security,
    settings,
    tm,
)
from app.services import (
    glossary_module as gm,
)

init_db()

PREV_HEAD = "c4b7e1f92a3d"
NEW_HEAD = "d5a3c8b1e470"

#: Exactly what revision d5a3c8b1e470 is allowed to add. Anything else in the diff
#: fails [G1] -- including a table or index this suite's author forgot to list.
EXPECTED_NEW_TABLES = {"projecttmsource", "projectglossary"}
EXPECTED_NEW_INDEXES = {
    ("projecttmsource", "ix_projecttmsource_project_id"),
    ("projecttmsource", "ix_projecttmsource_source_project_id"),
    ("projecttmsource", "ux_projecttmsource_pair"),
    ("projectglossary", "ix_projectglossary_project_id"),
    ("projectglossary", "ix_projectglossary_glossary_id"),
    ("projectglossary", "ux_projectglossary_pair"),
}

_TMPDIRS: list = []
_SEQ = [0]


def _seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _customer(s, name=None, org_id=None) -> Customer:
    c = Customer(name=name or f"Cust {_seq()}", org_id=org_id)
    s.add(c)
    s.commit()
    s.refresh(c)
    return c


def _project(
    s, name=None, customer_id=None, org_id=None, content_type_id=None, src="en-US"
) -> Project:
    p = Project(
        name=name or f"Proj {_seq()}",
        source_locale=src,
        customer_id=customer_id,
        org_id=org_id,
        content_type_id=content_type_id,
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    return p


def _plant_link(project_id, source_project_id):
    """INSERT a ProjectTmSource row STRAIGHT into the database, bypassing every
    service-layer check -- the hostile / corrupted-row case. This is deliberately NOT
    project_sources.add_tm_source: the whole claim under test is that the write door is
    not the only thing standing between a link row and another tenant's data."""
    with Session(engine) as s:
        s.add(ProjectTmSource(project_id=project_id, source_project_id=source_project_id))
        s.commit()


def _plant_pin(project_id, glossary_id):
    with Session(engine) as s:
        s.add(ProjectGlossary(project_id=project_id, glossary_id=glossary_id))
        s.commit()


def _tm_probe(
    s,
    pid,
    *,
    query="The quick brown fox jumps over it.",
    fuzzy_query="The quick brown fox jumped over it.",
    ft_query="Whole document body here.",
    conc="renard",
):
    """One call per READ path, rendered into a comparable dict.

    This is the capture [A5] diffs before-attach against after-detach. It deliberately
    covers every path _tm_read_scope feeds, so a future change that widens ONE of them
    without going through the seam shows up here as a difference."""
    exact = tm.lookup(s, pid, query, "en-US", "fr-FR")
    fuzzy = tm.lookup(s, pid, fuzzy_query, "en-US", "fr-FR", min_score=60)
    batch = tm.lookup_batch(s, pid, [query, fuzzy_query], "en-US", "fr-FR", min_score=60)
    examples = tm.example_matches_batch(s, pid, [fuzzy_query], "en-US", "fr-FR", floor=60, window=3)
    return {
        "exact": exact and (exact["match_pct"], exact["target_text"]),
        "fuzzy": fuzzy and (fuzzy["match_pct"], fuzzy["target_text"]),
        "batch": {k: (v and v["target_text"]) for k, v in batch.items()},
        "examples": {k: [e["target_text"] for e in v] for k, v in examples.items()},
        "ft": tm.ft_lookup(s, pid, ft_query, "en-US", "fr-FR"),
        "concordance": sorted(r["target"] for r in tm.concordance(s, pid, conc, "en-US", "fr-FR")),
        "count": tm.count(s, pid),
        "ft_count": tm.ft_count(s, pid),
    }


# ============================================================================
# [A] The matching payoff, and its exact reversal
# ============================================================================
def test_a_attach_widens_every_read_path_and_detach_restores():
    with Session(engine) as s:
        cust = _customer(s, "Acme A")
        target = _project(s, "A target", customer_id=cust.id)
        source = _project(s, "A source", customer_id=cust.id)
        tpid, spid = target.id, source.id
        # The SOURCE project holds the memory; the TARGET holds one unrelated row of
        # its own, so "before" is not trivially empty and a widened scope cannot be
        # confused with "it started working at all".
        tm.write_back(
            s, tpid, "An unrelated local sentence.", "Une phrase locale.", "en-US", "fr-FR"
        )
        tm.write_back(
            s,
            spid,
            "The quick brown fox jumps over it.",
            "Le renard brun rapide saute par-dessus.",
            "en-US",
            "fr-FR",
        )
        tm.write_back(
            s,
            spid,
            "The quick brown fox naps.",
            "Le renard brun rapide fait la sieste.",
            "en-US",
            "fr-FR",
        )
        tm.ft_store(
            s, spid, "Whole document body here.", "Corps du document ici.", "en-US", "fr-FR"
        )

    # ---- the BEFORE capture (no link exists) --------------------------------
    with Session(engine) as s:
        before = _tm_probe(s, tpid)
    assert before["exact"] is None and before["fuzzy"] is None, before
    assert before["ft"] is None and before["concordance"] == [], before
    assert before["count"] == 1 and before["ft_count"] == 0, before
    print("  [A1] before attach: the target project matches ONLY its own 1 entry")

    # ---- attach -------------------------------------------------------------
    with Session(engine) as s:
        view = project_sources.add_tm_source(s, tpid, spid, created_by="t@x.com")
        assert view["source_project_id"] == spid and view["entry_count"] == 2, view
        assert view["pairs"] == ["en-US -> fr-FR"], view
    with Session(engine) as s:
        after = _tm_probe(s, tpid)
    assert after["exact"] == (100, "Le renard brun rapide saute par-dessus."), after
    print("  [A2] the 100% exact short-circuit returns the ATTACHED project's row")
    assert after["fuzzy"] and after["fuzzy"][0] < 100, after
    assert after["fuzzy"][1] == "Le renard brun rapide saute par-dessus.", after
    assert (
        after["batch"]["The quick brown fox jumps over it."]
        == "Le renard brun rapide saute par-dessus."
    )
    print("  [A3] fuzzy + the batch path return attached rows (match_pct %d)" % after["fuzzy"][0])  # noqa: UP031
    ex = after["examples"]["The quick brown fox jumped over it."]
    assert len(ex) == 2 and "Le renard brun rapide fait la sieste." in ex, after
    print("  [A4] TM few-shot examples draw %d pairs from the attached project" % len(ex))  # noqa: UP031
    assert after["ft"] == "Corps du document ici.", after
    print("  [A5] the full-text path honours it too")
    # ...but CONCORDANCE does NOT (isolation review ISO-3). Every MATCH path above
    # gained the source's rows; the one BROWSE path in this probe stayed exactly where
    # it was. Asserted here, next to the paths that did move, so a future change that
    # widens the browse surface fails beside the cells that say why it must not.
    assert after["concordance"] == before["concordance"] == [], (before, after)
    print(
        "  [A5b] ...while CONCORDANCE stays own-project: an attach widens what you "
        "MATCH, never what you can read out"
    )

    # The WRITE side did NOT move. This is the half a "just widen the scope" change
    # gets wrong: counting/exporting somebody else's rows as your own would make the
    # project page lie and a TMX export exfiltrate another project's memory.
    assert after["count"] == before["count"] == 1, (before, after)
    assert after["ft_count"] == before["ft_count"] == 0, (before, after)
    with Session(engine) as s:
        xml = tm.export_tmx(s, tpid, "en-US", "fr-FR").decode("utf-8")
    assert "renard brun rapide" not in xml, xml[:400]
    assert "Une phrase locale." in xml, xml[:400]
    print(
        "  [A6] counts and the TMX export stay OWN-PROJECT -- an attach widens "
        "MATCHING only, never ownership"
    )

    # ---- detach: byte-identical to the before capture -----------------------
    with Session(engine) as s:
        assert project_sources.remove_tm_source(s, tpid, spid) is True
    with Session(engine) as s:
        restored = _tm_probe(s, tpid)
    if restored != before:
        _fail(
            f"detach did NOT restore the pre-attach behaviour.\n  before={before!r}\n"
            f"  after detach={restored!r}"
        )
    print(
        "  [A7] DETACH is byte-identical to the pre-attach capture on all 8 probes "
        "(nothing was copied, so undo is exact)"
    )
    # ...and re-attaching brings it back, so the reversal is not a one-way door.
    with Session(engine) as s:
        project_sources.add_tm_source(s, tpid, spid)
    with Session(engine) as s:
        assert _tm_probe(s, tpid)["exact"] == after["exact"]
        assert project_sources.remove_tm_source(s, tpid, spid) is True
        assert project_sources.remove_tm_source(s, tpid, spid) is False
    print("  [A8] re-attach restores it; a second detach reports False (idempotent)")


def test_a_own_project_wins_ties():
    """PRECEDENCE (behaviour review F1/F2). When the project's own row and a borrowed
    row are equally good, whose target is served?

    Before the fix: whichever the scan reached first, which meant the LOWER rowid --
    and the attached source is by definition the older, already-established TM, so it
    usually won. Two consequences, both silent: a curated own translation was shadowed
    on the 100% path (the one that AUTO-APPLIES without review), and a reviewer's
    correction could never take effect, because approving writes a NEW own row with a
    HIGHER id. The only escape was detaching the whole source."""
    with Session(engine) as s:
        cust = _customer(s, "Tie Corp")
        # The SOURCE project is created (and written) FIRST, so its rows carry the
        # lower ids -- the realistic ordering for an "already established" TM, and the
        # one that made the old behaviour lose.
        source = _project(s, "A tie source", customer_id=cust.id)
        target = _project(s, "A tie target", customer_id=cust.id)
        spid, tpid = source.id, target.id
        SRC = "Save your changes before leaving."
        tm.write_back(s, spid, SRC, "BORROWED", "en-US", "fr-FR")
        tm.write_back(s, tpid, SRC, "OWN", "en-US", "fr-FR")
        project_sources.add_tm_source(s, tpid, spid)

    with Session(engine) as s:
        one = tm.lookup(s, tpid, SRC, "en-US", "fr-FR")
        batch = tm.lookup_batch(s, tpid, [SRC], "en-US", "fr-FR")[SRC]
        assert one["match_pct"] == 100 and one["target_text"] == "OWN", one
        assert batch["target_text"] == "OWN", batch
        # The two exact paths MUST agree: a document translated segment-by-segment and
        # the same document translated in a batch cannot disagree about precedence.
        assert one["id"] == batch["id"], (one, batch)
    print(
        "  [A9] at an equal 100% hash the project's OWN row wins on BOTH exact "
        "paths, even though the borrowed row has the lower id"
    )

    # A reviewer's correction: a NEW own row, therefore the HIGHEST id of all three.
    # Under the old rule this could never win. It must win immediately, with the
    # source still attached.
    with Session(engine) as s:
        tm.write_back(s, tpid, SRC, "CORRECTED", "en-US", "fr-FR")
    with Session(engine) as s:
        fixed = tm.lookup(s, tpid, SRC, "en-US", "fr-FR")
        assert fixed["target_text"] == "CORRECTED", fixed
        assert tm.lookup_batch(s, tpid, [SRC], "en-US", "fr-FR")[SRC]["target_text"] == "CORRECTED"
    print(
        "  [A10] a post-attach local correction takes effect IMMEDIATELY -- no "
        "detach needed (the bug that made 'fix this one segment' mean 'give up all "
        "borrowed memory')"
    )

    # Determinism: build the mirror-image install where the OWN row was written FIRST.
    # The winner must be decided by the RULE, not by insertion order.
    with Session(engine) as s:
        cust2 = _customer(s, "Tie Corp Two")
        t2 = _project(s, "A tie target 2", customer_id=cust2.id)
        s2 = _project(s, "A tie source 2", customer_id=cust2.id)
        t2id, s2id = t2.id, s2.id
        tm.write_back(s, t2id, SRC, "OWN", "en-US", "fr-FR")  # own row FIRST now
        tm.write_back(s, s2id, SRC, "BORROWED", "en-US", "fr-FR")
        project_sources.add_tm_source(s, t2id, s2id)
    with Session(engine) as s:
        assert tm.lookup(s, t2id, SRC, "en-US", "fr-FR")["target_text"] == "OWN"
    print(
        "  [A11] the winner is the same with the insertion order REVERSED -- "
        "decided by the rule, not by rowid"
    )

    # The fuzzy path shares the tie-break, so a borrowed row cannot win a tie there
    # either (it is the path that feeds suggestions and few-shot examples).
    with Session(engine) as s:
        cust3 = _customer(s, "Tie Corp Three")
        src3 = _project(s, "A tie source 3", customer_id=cust3.id)
        tgt3 = _project(s, "A tie target 3", customer_id=cust3.id)
        s3id, t3id = src3.id, tgt3.id
        NEAR = "Discard the draft before leaving."
        tm.write_back(s, s3id, NEAR, "BORROWED-FUZZY", "en-US", "fr-FR")
        tm.write_back(s, t3id, NEAR, "OWN-FUZZY", "en-US", "fr-FR")
        project_sources.add_tm_source(s, t3id, s3id)
    with Session(engine) as s:
        fz = tm.lookup(s, t3id, "Discard the draft before leaving!", "en-US", "fr-FR", min_score=60)
        assert fz and fz["match_pct"] < 100, fz
        assert fz["target_text"] == "OWN-FUZZY", fz
    print(
        "  [A12] the FUZZY path shares the tie-break, so a borrowed row cannot win "
        "an equal-score suggestion either"
    )


def test_a_fulltext_precedence():
    """N1: the ranking path the first precedence pass MISSED.

    ft_lookup matters most. A FULL_TEXT hit short-circuits the ENTIRE document
    (translate.py's memory check returns the stored body wholesale), so a borrowed body
    winning the tie is not one bad segment -- it is the whole document served from
    somebody else's memory, auto-applied, with a re-approval unable to displace it
    because a new own row has a HIGHER id."""
    with Session(engine) as s:
        cust = _customer(s, "FT Tie Corp")
        # Source project first, so ITS row carries the lower id (the losing case).
        source = _project(s, "A ft source", customer_id=cust.id)
        target = _project(s, "A ft target", customer_id=cust.id)
        spid, tpid = source.id, target.id
        BODY = "The whole document body, verbatim."
        tm.ft_store(s, spid, BODY, "CORPS-EMPRUNTE", "en-US", "fr-FR")
        tm.ft_store(s, tpid, BODY, "CORPS-PROPRE", "en-US", "fr-FR")
        project_sources.add_tm_source(s, tpid, spid)
    with Session(engine) as s:
        assert tm.ft_lookup(s, tpid, BODY, "en-US", "fr-FR") == "CORPS-PROPRE", tm.ft_lookup(
            s, tpid, BODY, "en-US", "fr-FR"
        )
        # ...and the borrowed body is still reachable when the project has none of
        # its own, so the cell cannot pass by full-text attachment being broken.
        assert tm.ft_lookup(s, spid, BODY, "en-US", "fr-FR") == "CORPS-EMPRUNTE"
    print(
        "  [A13] FULL-TEXT: the project's own stored body wins the tie over a "
        "borrowed one with a lower id"
    )

    # A post-attach correction (ft_store overwrites in place or adds a newer row)
    # must take effect immediately, with the source still attached.
    with Session(engine) as s:
        tm.ft_store(s, tpid, BODY, "CORPS-CORRIGE", "en-US", "fr-FR")
    with Session(engine) as s:
        assert tm.ft_lookup(s, tpid, BODY, "en-US", "fr-FR") == "CORPS-CORRIGE"
    print("  [A14] a post-attach FULL-TEXT correction wins immediately (no detach)")


# ============================================================================
# [B] The link is a HINT, not an authority
# ============================================================================
def test_b_write_door_refusals():
    with Session(engine) as s:
        cust_a = _customer(s, "Ring A")
        cust_b = _customer(s, "Ring B")
        mine = _project(s, "B mine", customer_id=cust_a.id)
        theirs = _project(s, "B theirs", customer_id=cust_b.id)
        house = _project(s, "B house", customer_id=None)
        mine_id, theirs_id, house_id = mine.id, theirs.id, house.id

        try:
            project_sources.add_tm_source(s, mine_id, mine_id)
            _fail("a self-link was accepted")
        except project_sources.AttachError as exc:
            assert "own translation memory" in str(exc), exc
        print("  [B1] self-link refused")

        try:
            project_sources.add_tm_source(s, mine_id, theirs_id)
            _fail("a cross-customer attach was accepted")
        except project_sources.AttachError as exc:
            assert "different customer" in str(exc), exc
        print("  [B2] cross-CUSTOMER attach refused at the write door")

        try:
            project_sources.add_tm_source(s, mine_id, 999999)
            _fail("an unknown source project was accepted")
        except project_sources.AttachError:
            pass
        print("  [B3] unknown source project refused")

        # A customer-LESS (house) project IS attachable -- the install-wide shared
        # bucket, the same way a NULL-customer glossary is shared everywhere else.
        project_sources.add_tm_source(s, mine_id, house_id)
        project_sources.add_tm_source(s, mine_id, house_id)  # duplicate -> no-op
        n = s.exec(select(ProjectTmSource).where(ProjectTmSource.project_id == mine_id)).all()
        assert len(n) == 1, "duplicate attach created %d rows" % len(n)  # noqa: UP031
        print(
            "  [B4] a customer-LESS 'house' project attaches; a duplicate attach is a no-op (1 row)"
        )


def test_b_picker_offers_established_memory_only():
    """The >= 1-REACHABLE-ENTRY picker rule, in its OWN customer ring.

    ISOLATION NOTE (ISO-10). This cell used to live inside
    test_b_write_door_refusals and share that cell's customer. The candidate pool for a
    project is "every project of my customer, plus every customer-LESS project", and
    BOTH of those grow as other cells run -- so the cell's claim silently depended on
    which cells had executed before it, and on nothing later in the same cell having
    written a TM row. A reviewer saw it offer the empty project once. Rather than leave
    a claim whose truth depends on execution order, the fixture now uses a private
    customer, no customer-less projects, and asserts the DIRECT observable (the entry
    count) alongside the picker's answer, so a future failure names its own cause
    instead of being a mystery.

    The rule itself: the card attaches an ALREADY-ESTABLISHED memory, so a project with
    nothing to offer is not offered. This is also what keeps a freshly created project
    of ANY type from perturbing another project's page -- the property
    verify_keys_inertness [H1] depends on."""
    with Session(engine) as s:
        cust = _customer(s, "Picker Corp")  # private ring, no bleed
        mine = _project(s, "B picker mine", customer_id=cust.id)
        empty = _project(s, "B picker empty", customer_id=cust.id)
        mine_id, empty_id = mine.id, empty.id

        def offered():
            return {
                c["source_project_id"] for c in project_sources.tm_source_candidates(s, mine_id)
            }

        assert project_sources._tm_entry_count(s, empty_id) == 0, (
            "fixture broken: the 'empty' project already holds TM rows"
        )
        assert empty_id not in offered(), "an EMPTY project was offered as a TM source"

        tm.write_back(s, empty_id, "Now it has memory.", "MAINTENANT", "en-US", "fr-FR")
        assert project_sources._tm_entry_count(s, empty_id) == 1
        assert empty_id in offered(), "a project with TM entries was NOT offered"
        # Nothing else of this customer's leaked into the pool.
        assert offered() == {empty_id}, offered()

        # ...but an EXPLICIT attach of an empty project (the CLI, by id) is still
        # allowed: attaching before importing is legitimate, and the filter is a
        # picker nicety, not a rule.
        blank = _project(s, "B picker blank", customer_id=cust.id)
        project_sources.add_tm_source(s, mine_id, blank.id)
        assert blank.id in project_sources.allowed_tm_source_ids(s, mine_id)
        assert blank.id not in offered(), "an attached project stayed in the picker"
    print(
        "  [B4b] the picker offers ESTABLISHED memory only (0 entries -> not "
        "offered, 1 entry -> offered) in a private customer ring, while an explicit "
        "attach by id still works on an empty project"
    )


def test_b_tm_org_leg_is_ungated_too():
    """The TM twin of [C11]: cross_project_scope_allows' org leg, GATE OFF.

    F5 was raised about the glossary predicate, and fixing only that one left the two
    siblings with OPPOSITE discipline: gate-off, an org-stamped install could attach TM
    across orgs while it could not pin a glossary across orgs. Same kind of pair, same
    question, two answers -- and the TM half is the one that feeds live translations.
    Both legs are now ungated and read identically, so this cell is deliberately shaped
    like [C11] and asserts the SAME three things on the TM side.

    Note the customers are DELIBERATELY the same on both sides, so only the org leg can
    refuse anything: if it were still gated, the customer leg would wave this through
    and the cell would fail."""
    with Session(engine) as s:
        org1 = Organization(name="Org TmGate 1")
        org2 = Organization(name="Org TmGate 2")
        s.add(org1)
        s.add(org2)
        s.commit()
        s.refresh(org1)
        s.refresh(org2)
        cust = _customer(s, "TmGate Corp", org_id=org1.id)
        mine = _project(s, "B tmgate mine", customer_id=cust.id, org_id=org1.id)
        foreign = _project(s, "B tmgate foreign", customer_id=cust.id, org_id=org2.id)
        same = _project(s, "B tmgate same", customer_id=cust.id, org_id=org1.id)
        mine_id, foreign_id, same_id = mine.id, foreign.id, same.id
        # Deliberately far apart lexically: with near-identical strings the fuzzy
        # path matches one probe against the OTHER project's row, which reads like
        # a leak and hides a real one.
        tm.write_back(
            s, foreign_id, "Zebra crossing at midnight.", "OTHER-ORG-TM", "en-US", "fr-FR"
        )
        tm.write_back(
            s, same_id, "Quantum flux capacitor readings.", "SAME-ORG-TM", "en-US", "fr-FR"
        )

        assert not access.multi_org_active(), "this cell is about the GATE-OFF case"
        # (1) the predicate itself, both directions.
        assert access.cross_project_scope_allows(mine, s.get(Project, same_id))
        assert not access.cross_project_scope_allows(mine, s.get(Project, foreign_id))
        # (2) the SERVICE door refuses it, and the picker never offers it.
        offered = {c["source_project_id"] for c in project_sources.tm_source_candidates(s, mine_id)}
        assert same_id in offered, offered
        assert foreign_id not in offered, (
            "gate OFF: an org-2 project was offered as a TM source to an org-1 project"
        )
        try:
            project_sources.add_tm_source(s, mine_id, foreign_id)
            _fail("gate OFF: an org-2 project was ATTACHED to an org-1 project")
        except project_sources.AttachError as exc:
            assert "different customer or organization" in str(exc), exc
        # ...while the same-org sibling still attaches, so the cell cannot pass by the
        # predicate having become a blanket refusal.
        project_sources.add_tm_source(s, mine_id, same_id)
        assert project_sources.allowed_tm_source_ids(s, mine_id) == (same_id,)
    print(
        "  [B4c] gate OFF: an org-stamped cross-ORG attach is refused by the "
        "predicate AND the service, and never offered -- while the same-org one "
        "still works"
    )

    # (3) the WEB door refuses a hand-posted foreign id (the picker is not the gate).
    security.create_user("b-tmgate@x.com", "B TmGate", Role.ADMIN, "b-tmgate-password-123")
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        _login(c, "b-tmgate@x.com", "b-tmgate-password-123")
        r = c.post(
            "/projects/%d/tm-sources/add" % mine_id,  # noqa: UP031
            data={"source_project_id": str(foreign_id)},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "err=tm_source" in r.headers["location"], r.headers.get(
            "location"
        )
        with Session(engine) as s:
            assert project_sources.allowed_tm_source_ids(s, mine_id) == (same_id,)
        page = c.get("/projects/%d" % mine_id).text  # noqa: UP031
        assert "B tmgate foreign" not in page, "the foreign-org project was named on the page"
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print(
        "  [B4d] gate OFF: the WEB door refuses a hand-posted cross-ORG source id "
        "and the page never names it"
    )

    # (4) and a link planted around BOTH doors is inert at READ time -- the same
    # re-derivation that carries the cross-customer and multi-org cells, now covering
    # the gate-off cross-org case that previously had no read-time defence at all.
    _plant_link(mine_id, foreign_id)
    with Session(engine) as s:
        planted = s.exec(
            select(ProjectTmSource).where(
                ProjectTmSource.project_id == mine_id,
                ProjectTmSource.source_project_id == foreign_id,
            )
        ).all()
        assert len(planted) == 1, "the cross-org row was not actually planted"
        assert project_sources.allowed_tm_source_ids(s, mine_id) == (same_id,), (
            "a planted gate-off cross-ORG link resolved to a reachable source"
        )
        assert tm.lookup(s, mine_id, "Zebra crossing at midnight.", "en-US", "fr-FR") is None
        assert (
            tm.lookup(s, mine_id, "Quantum flux capacitor readings.", "en-US", "fr-FR")[
                "target_text"
            ]
            == "SAME-ORG-TM"
        )
        assert {v["source_project_id"] for v in project_sources.list_tm_sources(s, mine_id)} == {
            same_id
        }
    print(
        "  [B4e] gate OFF: a PLANTED cross-ORG link is inert at read time and is "
        "not listed, while the legitimate same-org source keeps matching"
    )


def test_b_null_org_normalizes_off_gate():
    """NEW-1: off-gate, org_id is a column NOTHING WRITES.

    content._org_for stamps NULL unless the sacred gate is on, and
    db._ensure_default_org backfills NULL -> the Default org only AT BOOT. So a
    single-tenant install's rows are a MIXTURE -- stamped before the last restart, NULL
    since -- and the ungated org leg added in 4a13bda compared the raw column. The
    result: create a project and attach a source to it and the attach was REFUSED
    (stamped target vs NULL source) until somebody restarted the server, at which point
    the same action started working. A restart-dependent refusal is worse than either
    answer, and the mirror hole was open too: a NULL target applied no org filter at
    all. Both predicates now normalize NULL -> the install's default org first."""
    from app.services import content as content_svc

    default_org = access._install_default_org_id()
    assert default_org is not None, "the install seed did not create a default org"

    with Session(engine) as s:
        cust = _customer(s, "NullOrg Corp")
        # THE PRODUCTION SHAPE: `stamped` is what a project looks like AFTER a boot
        # backfill; `fresh` is what content.project_create actually writes right now.
        stamped = _project(s, "B nullorg stamped", customer_id=cust.id, org_id=default_org)
        fresh = content_svc.project_create(
            s, "B nullorg fresh", customer_id=cust.id, target_locales="fr-FR"
        )
        s.refresh(fresh)
        assert fresh.org_id is None, (
            f"fixture assumption broken: project_create now stamps org_id={fresh.org_id!r} off-gate, "  # noqa: E501
            "so this cell no longer reproduces the mixture it is about"
        )
        stamped_id, fresh_id = stamped.id, fresh.id
        tm.write_back(s, fresh_id, "Freshly created row.", "FRAICHE", "en-US", "fr-FR")
        tm.write_back(s, stamped_id, "Backfilled row.", "REMPLIE", "en-US", "fr-FR")

        # BOTH directions, immediately, with no restart in between.
        project_sources.add_tm_source(s, stamped_id, fresh_id)
        assert (
            tm.lookup(s, stamped_id, "Freshly created row.", "en-US", "fr-FR")["target_text"]
            == "FRAICHE"
        )
        project_sources.add_tm_source(s, fresh_id, stamped_id)
        assert (
            tm.lookup(s, fresh_id, "Backfilled row.", "en-US", "fr-FR")["target_text"] == "REMPLIE"
        )
    print(
        "  [B4f] off-gate: a JUST-CREATED (NULL-org) project and a backfilled "
        "(default-org) one attach to each other in BOTH directions immediately -- "
        "no restart required"
    )

    # A genuine SECOND org is still refused off-gate, both directions -- so the
    # normalization did not simply switch the org leg off.
    with Session(engine) as s:
        other = Organization(name="Org NullOrg Second")
        s.add(other)
        s.commit()
        s.refresh(other)
        assert other.id != default_org
        cust2 = _customer(s, "NullOrg Second Corp", org_id=other.id)
        second = _project(s, "B nullorg second", customer_id=cust2.id, org_id=other.id)
        # Same customer on both sides for the strict pair, so only the org leg can
        # refuse: a project of org-2 owned by the SAME customer as the org-1 pair.
        second_same_cust = _project(s, "B nullorg second sc", customer_id=cust2.id, org_id=other.id)
        second_id, second_sc_id = second.id, second_same_cust.id
        tm.write_back(s, second_id, "Second org row.", "SECONDE", "en-US", "fr-FR")

        for a, b in (
            (fresh_id, second_id),
            (second_id, fresh_id),
            (stamped_id, second_id),
            (second_id, stamped_id),
        ):
            try:
                project_sources.add_tm_source(s, a, b)
                _fail("off-gate: a genuine cross-ORG attach (%d -> %d) was accepted" % (a, b))  # noqa: UP031
            except project_sources.AttachError:
                pass
        # ...while two projects of that SAME second org still attach to each other.
        project_sources.add_tm_source(s, second_sc_id, second_id)
        assert project_sources.allowed_tm_source_ids(s, second_sc_id) == (second_id,)
    print(
        "  [B4g] off-gate: two genuinely DIFFERENT orgs are still refused in both "
        "directions, while two projects of the same second org attach"
    )

    # NULL-NULL (a legacy install that has never been restarted) still works.
    with Session(engine) as s:
        cust3 = _customer(s, "NullOrg Legacy Corp")
        n1 = _project(s, "B nullorg legacy one", customer_id=cust3.id, org_id=None)
        n2 = _project(s, "B nullorg legacy two", customer_id=cust3.id, org_id=None)
        tm.write_back(s, n2.id, "Legacy pair row.", "HERITAGE", "en-US", "fr-FR")
        project_sources.add_tm_source(s, n1.id, n2.id)
        assert (
            tm.lookup(s, n1.id, "Legacy pair row.", "en-US", "fr-FR")["target_text"] == "HERITAGE"
        )
    print("  [B4h] off-gate: a NULL-org / NULL-org pair still attaches and matches")


def test_c_glossary_org_normalization():
    """NEW-1 on the glossary side: the pin predicate AND build_glossary_plan's own
    org filter, which is OLDER than this feature and carried the identical latent bug
    on main -- a typed project created since the last restart auto-attached NOTHING,
    because its NULL org_id never equalled the backfilled glossaries' Default org."""
    from app.services import content as content_svc
    from app.services import content_types

    default_org = access._install_default_org_id()
    with Session(engine) as s:
        cust = _customer(s, "GlossOrg Corp", org_id=default_org)
        ct = content_types.create(s, "GlossOrgType", customer_id=cust.id, org_id=default_org)
        # A backfilled (default-org) APPROVED glossary of that content type...
        g = _mk_glossary(
            s,
            "C glossorg",
            cust.id,
            term=("dashboard", "ORG-TABLEAU"),
            content_type_id=ct.id,
            org_id=default_org,
        )
        gid = g.id
        # ...and a TYPED project created the way project_create actually creates one.
        proj = content_svc.project_create(
            s, "C glossorg proj", customer_id=cust.id, target_locales="fr-FR"
        )
        proj.content_type_id = ct.id
        s.add(proj)
        s.commit()
        s.refresh(proj)
        assert proj.org_id is None, f"fixture assumption broken: {proj.org_id!r}"
        pid = proj.id

        # AUTO-ATTACH: the long-standing filter must find the backfilled glossary.
        plan, terms = _terms(s, proj)
        assert gid in plan.glossary_ids, (
            f"a NULL-org typed project auto-attached nothing from a default-org glossary: {plan!r}"
        )
        assert terms == ["ORG-TABLEAU"], terms
        # PIN: the predicate agrees with its neighbour, as it must.
        assert access.project_can_use_glossary(proj, s.get(Glossary, gid))
        offered = {c["glossary_id"] for c in project_sources.glossary_pin_candidates(s, pid)}
        assert gid in offered, offered
    print(
        "  [C12] off-gate: a JUST-CREATED (NULL-org) typed project auto-attaches AND "
        "can pin a backfilled (default-org) glossary -- the pre-existing filter bug "
        "the pin predicate had mirrored"
    )

    # A genuinely second-org glossary is still refused (the leg is not switched off).
    with Session(engine) as s:
        other = Organization(name="Org GlossOrg Second")
        s.add(other)
        s.commit()
        s.refresh(other)
        cust2 = _customer(s, "GlossOrg Second Corp", org_id=other.id)
        foreign = _mk_glossary(
            s, "C glossorg foreign", cust2.id, term=("dashboard", "FOREIGN-ORG"), org_id=other.id
        )
        proj = s.get(Project, pid)
        assert not access.project_can_use_glossary(proj, s.get(Glossary, foreign.id))
        assert foreign.id not in {
            c["glossary_id"] for c in project_sources.glossary_pin_candidates(s, pid)
        }
    print("  [C13] off-gate: a genuinely second-org glossary is still refused")


def test_b_hostile_link_reaches_nothing():
    """THE CELL THE REVIEWERS SHOULD TRY TO BREAK.

    A ProjectTmSource row is INSERTed directly, naming another customer's project --
    what an attacker with a write primitive on the link table (or a botched migration,
    or a restored backup) leaves behind. If the read scope trusted the link, this is a
    cross-tenant TM leak into live translations."""
    with Session(engine) as s:
        victim_cust = _customer(s, "Victim Corp")
        attacker_cust = _customer(s, "Attacker Ltd")
        victim = _project(s, "B victim", customer_id=victim_cust.id)
        attacker = _project(s, "B attacker", customer_id=attacker_cust.id)
        vpid, apid = victim.id, attacker.id
        tm.write_back(s, vpid, "Merger closes on Friday.", "SECRET-VICTIM-TARGET", "en-US", "fr-FR")
        tm.ft_store(s, vpid, "Whole confidential body.", "SECRET-VICTIM-FT", "en-US", "fr-FR")

    _plant_link(apid, vpid)
    with Session(engine) as s:
        planted = s.exec(select(ProjectTmSource).where(ProjectTmSource.project_id == apid)).all()
        assert len(planted) == 1, "the hostile row was not actually planted"
        # ...and it is invisible to the resolver.
        assert project_sources.allowed_tm_source_ids(s, apid) == (), (
            "the hostile link resolved to a reachable source id"
        )
        probe = _tm_probe(
            s,
            apid,
            query="Merger closes on Friday.",
            fuzzy_query="Merger closed on Friday.",
            ft_query="Whole confidential body.",
            conc="secret",
        )
    for key, val in probe.items():
        if key in ("count", "ft_count"):
            continue
        rendered = repr(val)
        if "SECRET-VICTIM" in rendered:
            _fail(f"HOSTILE LINK LEAKED through the {key} path: {rendered}")
    assert probe["exact"] is None and probe["ft"] is None, probe
    print(
        "  [B5] HOSTILE LINK (INSERTed straight into the table, cross-customer): "
        "every read path returns ZERO of the victim's rows"
    )
    # The management surfaces refuse to show it too -- a stale/hostile link must not
    # appear on the project page as if it were contributing coverage.
    with Session(engine) as s:
        assert project_sources.list_tm_sources(s, apid) == [], (
            "a hostile link was listed on the management surface"
        )
    print("  [B6] ...and it is not listed as a source either")


def test_b_multi_org_hostile_link():
    """The same attack under the SACRED multi-org gate, planted CROSS-ORG.

    Two independent claims: the write door refuses a cross-org attach, and -- the one
    that matters -- a link planted around the write door still reaches nothing, because
    the org leg of access.cross_project_scope_allows is re-evaluated at READ time.

    RETROACTIVE TIGHTENING is asserted on the leg the gate actually adds. The org leg is
    now ungated ([B4c]), so a cross-org link can no longer be created off-gate and there
    is nothing left for the gate to retroactively kill there. What the gate DOES add is
    the NULL-ORG QUARANTINE: off-gate an un-stamped (NULL-org) project applies no org
    filter at all, so two legacy projects attach happily; switch the gate on and both
    are quarantined, exactly the way same_org() quarantines an un-stamped row. That link
    goes inert with no cleanup step, while an org-stamped same-org link keeps working --
    which is what makes this a tightening rather than a blanket shutdown."""
    with Session(engine) as s:
        org_a = Organization(name="Org SRC A")
        org_b = Organization(name="Org SRC B")
        s.add(org_a)
        s.add(org_b)
        s.commit()
        s.refresh(org_a)
        s.refresh(org_b)
        # Same CUSTOMER-less shape on every side so ONLY the org differs -- otherwise a
        # pass could come from the customer leg and the org leg could be dead code.
        pa = _project(s, "B org A", customer_id=None, org_id=org_a.id)
        pb = _project(s, "B org B", customer_id=None, org_id=org_b.id)
        pa2 = _project(s, "B org A two", customer_id=None, org_id=org_a.id)
        # The LEGACY pair: no org stamp on either side (a pre-multi-org install).
        pn1 = _project(s, "B org null one", customer_id=None, org_id=None)
        pn2 = _project(s, "B org null two", customer_id=None, org_id=None)
        a_id, b_id, a2_id = pa.id, pb.id, pa2.id
        n1_id, n2_id = pn1.id, pn2.id
        tm.write_back(s, b_id, "Cross org secret text.", "SECRET-ORG-B", "en-US", "fr-FR")
        tm.write_back(s, a2_id, "Same org sibling text.", "SAME-ORG-A", "en-US", "fr-FR")
        tm.write_back(s, n2_id, "Legacy unstamped text.", "LEGACY-NULL-ORG", "en-US", "fr-FR")

    # Gate OFF: the cross-ORG attach is ALREADY refused (the org leg is ungated), while
    # the same-org and the legacy NULL-org pairs both attach.
    with Session(engine) as s:
        try:
            project_sources.add_tm_source(s, a_id, b_id)
            _fail("gate OFF: a cross-ORG attach was accepted (the org leg is ungated)")
        except project_sources.AttachError:
            pass
        project_sources.add_tm_source(s, a_id, a2_id)
        project_sources.add_tm_source(s, n1_id, n2_id)
        assert project_sources.allowed_tm_source_ids(s, a_id) == (a2_id,)
        assert project_sources.allowed_tm_source_ids(s, n1_id) == (n2_id,)
        assert (
            tm.lookup(s, n1_id, "Legacy unstamped text.", "en-US", "fr-FR")["target_text"]
            == "LEGACY-NULL-ORG"
        )
    print(
        "  [B7] gate OFF: cross-ORG is already refused, while a same-org link and a "
        "legacy NULL-org pair both attach and match"
    )

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            # RETROACTIVE tightening: the legacy NULL-org link made a moment ago is now
            # quarantined, with no cleanup step having run...
            assert project_sources.allowed_tm_source_ids(s, n1_id) == (), (
                project_sources.allowed_tm_source_ids(s, n1_id)
            )
            probe = _tm_probe(
                s,
                n1_id,
                query="Legacy unstamped text.",
                fuzzy_query="Legacy unstamped texts.",
                ft_query="nothing",
                conc="legacy",
            )
            assert "LEGACY-NULL-ORG" not in repr(probe), probe
            # ...while the org-stamped SAME-ORG link keeps working, so the gate did not
            # simply switch attachment off wholesale (a test that passes for the wrong
            # reason).
            assert project_sources.allowed_tm_source_ids(s, a_id) == (a2_id,)
            same = tm.lookup(s, a_id, "Same org sibling text.", "en-US", "fr-FR")
            assert same and same["target_text"] == "SAME-ORG-A", same
        print(
            "  [B8] gate ON: the legacy NULL-org link is quarantined RETROACTIVELY "
            "while the org-stamped same-org one keeps working"
        )

        # A fresh cross-org attach is refused at the door, and a planted one is inert.
        with Session(engine) as s:
            try:
                project_sources.add_tm_source(s, a_id, b_id)
                _fail("a cross-ORG attach was accepted under the sacred gate")
            except project_sources.AttachError:
                pass
        _plant_link(a_id, b_id)
        with Session(engine) as s:
            assert project_sources.allowed_tm_source_ids(s, a_id) == (a2_id,), (
                "a planted cross-ORG link resolved under the sacred gate"
            )
            assert tm.lookup(s, a_id, "Cross org secret text.", "en-US", "fr-FR") is None
        print(
            "  [B9] gate ON: cross-ORG attach refused at the door AND a planted one "
            "reaches zero rows (the same-org link still resolving beside it)"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()
        with Session(engine) as s:
            s.exec(
                text("DELETE FROM projecttmsource WHERE project_id IN (:a, :n)"),
                params={"a": a_id, "n": n1_id},
            )
            s.commit()


def test_b_stale_link_and_no_transitivity():
    with Session(engine) as s:
        c1 = _customer(s, "Stale One")
        c2 = _customer(s, "Stale Two")
        mine = _project(s, "B stale mine", customer_id=c1.id)
        src = _project(s, "B stale src", customer_id=c1.id)
        mine_id, src_id = mine.id, src.id
        tm.write_back(s, src_id, "Reassignment probe text.", "STALE-TARGET", "en-US", "fr-FR")
        project_sources.add_tm_source(s, mine_id, src_id)
        hit = tm.lookup(s, mine_id, "Reassignment probe text.", "en-US", "fr-FR")
        assert hit and hit["target_text"] == "STALE-TARGET", hit
        # The source moves to another customer AFTER the link was legitimately made.
        moved = s.get(Project, src_id)
        moved.customer_id = c2.id
        s.add(moved)
        s.commit()
    with Session(engine) as s:
        assert project_sources.allowed_tm_source_ids(s, mine_id) == (), (
            "a link survived its source moving to another customer"
        )
        assert tm.lookup(s, mine_id, "Reassignment probe text.", "en-US", "fr-FR") is None
        assert project_sources.list_tm_sources(s, mine_id) == []
    print(
        "  [B10] a legitimately-created link goes INERT the moment its source "
        "changes customer -- no cleanup job, because the link was never the "
        "authority"
    )

    # Transitivity: A -> B -> C. A must see B's rows and NOT C's.
    with Session(engine) as s:
        cust = _customer(s, "Chain Corp")
        a = _project(s, "B chain A", customer_id=cust.id)
        b = _project(s, "B chain B", customer_id=cust.id)
        c = _project(s, "B chain C", customer_id=cust.id)
        a_id, b_id, c_id = a.id, b.id, c.id
        # Deliberately far apart lexically, so a fuzzy near-miss between the two probe
        # strings cannot be mistaken for (or hide) a transitive reach.
        hop1, hop2 = "Zebra crossing at midnight.", "Quantum flux capacitor readings."
        tm.write_back(s, b_id, hop1, "HOP-ONE", "en-US", "fr-FR")
        tm.write_back(s, c_id, hop2, "HOP-TWO", "en-US", "fr-FR")
        project_sources.add_tm_source(s, a_id, b_id)
        project_sources.add_tm_source(s, b_id, c_id)
    with Session(engine) as s:
        one = tm.lookup(s, a_id, hop1, "en-US", "fr-FR")
        two = tm.lookup(s, a_id, hop2, "en-US", "fr-FR")
        assert one and one["target_text"] == "HOP-ONE", one
        assert two is None, f"attachment CHAINED transitively (A reached C via B): {two!r}"
        assert project_sources.allowed_tm_source_ids(s, a_id) == (b_id,)
        # ...and B itself does reach C, so the chain exists and simply does not compose.
        assert tm.lookup(s, b_id, hop2, "en-US", "fr-FR")
    print(
        "  [B11] attachment is ONE LEVEL: A->B->C leaves C unreachable from A "
        "(while B->C itself works, so the chain is real and just does not compose)"
    )


def test_b_concordance_is_own_project_only():
    """ISO-3. Attachment widens MATCHING; it must not widen BROWSING.

    The threat is not abstract. `/documents/{id}/concordance` takes an arbitrary
    substring from the editor and returns every TM row containing it. If it honoured
    attachments, any member of the borrowing project -- who may have no visibility into
    the source project at all -- could walk that project's entire memory a query at a
    time. A match, by contrast, answers a bounded question about a segment the caller
    is already holding."""
    with Session(engine) as s:
        cust = _customer(s, "Concord Corp")
        source = _project(s, "B concord source", customer_id=cust.id)
        target = _project(s, "B concord target", customer_id=cust.id)
        spid, tpid = source.id, target.id
        # The source's memory is full of a distinctive token a curious member of the
        # target project could fish for.
        for i in range(4):
            tm.write_back(
                s,
                spid,
                "Acquisition memo paragraph %d." % i,  # noqa: UP031
                "CONFIDENTIEL-%d" % i,  # noqa: UP031
                "en-US",
                "fr-FR",
            )
        project_sources.add_tm_source(s, tpid, spid)

    with Session(engine) as s:
        # MATCHING works -- the feature does what it promises.
        hit = tm.lookup(s, tpid, "Acquisition memo paragraph 2.", "en-US", "fr-FR")
        assert hit and hit["target_text"] == "CONFIDENTIEL-2", hit
        # BROWSING returns nothing of the source's, on either side of the row.
        for probe in ("acquisition", "confidentiel", "memo", "paragraph"):
            rows = tm.concordance(s, tpid, probe, "en-US", "fr-FR")
            assert rows == [], (
                "concordance leaked %d source row(s) for probe %r: %r"  # noqa: UP031
                % (  # noqa: RUF100, UP031
                    len(rows),
                    probe,
                    rows[:3],
                )
            )
        # ...and the source project's OWN concordance still works, so the cell cannot
        # pass because concordance is simply broken.
        own = tm.concordance(s, spid, "confidentiel", "en-US", "fr-FR")
        assert len(own) == 4, own
    print(
        "  [B12] CONCORDANCE stays own-project under an attachment (4 probes, both "
        "sides of the row) while lookups still match -- and the source project's "
        "own concordance is unaffected"
    )


def test_b_global_null_row_still_excluded_under_the_gate():
    """The NULL LEG (behaviour review F8's surviving mutation).

    _tm_scope's multi-org branch drops the NULL-project "global" leg -- that is leak
    vector 9. The attach widening rides INSIDE that branch, so the obvious way to get
    it wrong is to rebuild the widened predicate as `col.in_((pid, *srcs, None))` on
    BOTH branches. Every other cell would still pass: they all assert about rows that
    belong to a real project. This one plants a NULL-project row and requires that a
    project WITH a live, legitimate attachment still cannot see it."""
    with Session(engine) as s:
        org = Organization(name="Org NullLeg")
        s.add(org)
        s.commit()
        s.refresh(org)
        target = _project(s, "B nullleg target", customer_id=None, org_id=org.id)
        source = _project(s, "B nullleg source", customer_id=None, org_id=org.id)
        tpid, spid = target.id, source.id
        tm.write_back(s, spid, "Attached same-org row.", "SAME-ORG-OK", "en-US", "fr-FR")
        project_sources.add_tm_source(s, tpid, spid)
        # A NULL-project (org-less) "global" row -- what an old install's shared TM
        # looked like. Written directly: no service creates these any more.
        s.add(
            SegmentTM(
                project_id=None,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="Org-less global row.",
                target_text="GLOBAL-LEAK",
                source_hash=tm.hash_text("Org-less global row.", "en-US", "fr-FR"),
            )
        )
        s.commit()

    settings.set_many({"security_auth_required": "1", "multi_org_enabled": "1"})
    settings.invalidate_cache()
    try:
        with Session(engine) as s:
            # The attachment is LIVE (so the widened branch is genuinely in play)...
            assert project_sources.allowed_tm_source_ids(s, tpid) == (spid,)
            assert (
                tm.lookup(s, tpid, "Attached same-org row.", "en-US", "fr-FR")["target_text"]
                == "SAME-ORG-OK"
            )
            # ...and the NULL-project row is STILL invisible on every match path.
            assert tm.lookup(s, tpid, "Org-less global row.", "en-US", "fr-FR") is None
            assert (
                tm.lookup_batch(s, tpid, ["Org-less global row."], "en-US", "fr-FR")[
                    "Org-less global row."
                ]
                is None
            )
            ex = tm.example_matches_batch(
                s, tpid, ["Org-less global rows."], "en-US", "fr-FR", floor=60, window=3
            )
            assert not any("GLOBAL-LEAK" in e["target_text"] for v in ex.values() for e in v), ex
        print(
            "  [B13] gate ON with a LIVE attachment: the NULL-project global row is "
            "still excluded on every match path (leak vector 9 survives the "
            "widening)"
        )
    finally:
        settings.set_many({"security_auth_required": "0", "multi_org_enabled": "0"})
        settings.invalidate_cache()


def test_b_unique_index_exists_on_the_create_all_path_too():
    """The duplicate refusal, proven against the HERMETIC (create_all) schema.

    [G2] proves the unique index on the ALEMBIC path. But every verify suite -- and so
    every other cell here -- runs on the create_all path, where the index comes from
    the MODEL's __table_args__. Dropping `unique=True` there would leave [G2] green
    (alembic still has it) and the duplicate-attach cell green (the service's
    existence check hides it), so nothing would notice that the real backstop against
    a concurrent double-insert had gone. This cell reads the live test schema."""
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    for table, index in (
        ("projecttmsource", "ux_projecttmsource_pair"),
        ("projectglossary", "ux_projectglossary_pair"),
    ):
        found = {ix["name"]: ix for ix in insp.get_indexes(table)}
        assert index in found, (
            f"{index} is MISSING from the create_all schema (indexes: {sorted(found)})"
        )
        assert found[index]["unique"], f"{index} exists but is NOT unique"
    # And it actually refuses, on this schema, at the DB level.
    with Session(engine) as s:
        cust = _customer(s, "Dup Corp")
        a = _project(s, "B dup a", customer_id=cust.id)
        b = _project(s, "B dup b", customer_id=cust.id)
        aid, bid = a.id, b.id
    with Session(engine) as s:
        s.add(ProjectTmSource(project_id=aid, source_project_id=bid))
        s.commit()
    refused = False
    with Session(engine) as s:
        try:
            s.add(ProjectTmSource(project_id=aid, source_project_id=bid))
            s.commit()
        except Exception:
            s.rollback()
            refused = True
    assert refused, (
        "the create_all schema accepted a DUPLICATE (project, source) "
        "pair -- the unique index is not doing its job"
    )
    print(
        "  [B14] both pair uniques exist AND refuse a duplicate on the create_all "
        "(hermetic) schema, not just the alembic one"
    )


# ============================================================================
# [C] Glossary pins
# ============================================================================
def _terms(s, project, src="en-US", tgt="fr-FR", text_="Please open the dashboard now."):
    """The terms a run of `project` would actually apply, via the SAME two calls the
    orchestrator/translate paths make (build_glossary_plan -> match_terms_merged)."""
    plan = gm.build_glossary_plan(s, project, src, tgt)
    hits = gm.match_terms_merged(s, project.id, text_, src, tgt, plan)
    return plan, sorted(h["target_term"] for h in hits if h.get("target_term"))


def _mk_glossary(
    s,
    name,
    customer_id,
    *,
    approved=True,
    term=("dashboard", "TABLEAU"),
    content_type_id=None,
    org_id=None,
):
    g = gm.create(s, name, org_id=org_id, customer_id=customer_id, content_type_id=content_type_id)
    gm.set_pairs(s, g.id, ["en-US->fr-FR"])
    gm.add_term(s, g.id, term[0], target_term=term[1], source_locale="en-US", target_locale="fr-FR")
    if approved:
        gm.approve(s, g.id, "t@x.com")
    return s.get(Glossary, g.id)


def test_c_glossary_pin_on_untyped_project():
    with Session(engine) as s:
        cust = _customer(s, "Gloss Corp")
        proj = _project(s, "C untyped", customer_id=cust.id)  # UNTYPED: no content type
        g = _mk_glossary(s, "C approved", cust.id)
        gid = g.id

        # BEFORE: an untyped project auto-attaches NOTHING (the byte-identical gate).
        plan_before, terms_before = _terms(s, proj)
        assert plan_before.glossary_ids == () and plan_before.include_legacy is True
        assert terms_before == [], terms_before

        project_sources.add_glossary_pin(s, proj.id, gid, created_by="t@x.com")
        plan_after, terms_after = _terms(s, proj)
        assert plan_after.glossary_ids == (gid,), plan_after
        assert plan_after.include_legacy is True, plan_after
        assert terms_after == ["TABLEAU"], terms_after
    print(
        "  [C1] an APPROVED pin reaches match_terms on an UNTYPED project -- the "
        "case implicit auto-attach deliberately never covers"
    )

    # Detach restores the untyped plan exactly.
    with Session(engine) as s:
        proj = s.get(Project, proj.id)
        assert project_sources.remove_glossary_pin(s, proj.id, gid) is True
        plan_off, terms_off = _terms(s, proj)
        assert (plan_off, terms_off) == (plan_before, terms_before), (plan_off, terms_off)
    print("  [C2] unpinning restores the untyped plan byte-identically")


def test_c_draft_pin_contributes_nothing():
    with Session(engine) as s:
        cust = _customer(s, "Draft Corp")
        proj = _project(s, "C draft", customer_id=cust.id)
        g = _mk_glossary(s, "C draft gloss", cust.id, approved=False)
        gid, pid = g.id, proj.id
        project_sources.add_glossary_pin(s, pid, gid)
        plan, terms = _terms(s, proj)
        assert plan.glossary_ids == (), f"a DRAFT pin contributed to the plan: {plan!r}"
        assert terms == [], terms
        # It IS listed (so the operator can see it is waiting), flagged inactive.
        listed = project_sources.list_glossary_pins(s, pid)
        assert len(listed) == 1 and listed[0]["active"] is False, listed
    print("  [C3] a DRAFT pin contributes NOTHING and is listed as not-yet-applied")
    with Session(engine) as s:
        gm.approve(s, gid, "t@x.com")
        proj = s.get(Project, pid)
        plan, terms = _terms(s, proj)
        assert plan.glossary_ids == (gid,) and terms == ["TABLEAU"], (plan, terms)
        # ...and un-approving takes it straight back out, with the pin still in place.
        gm.unapprove(s, gid)
        plan2, terms2 = _terms(s, proj)
        assert plan2.glossary_ids == () and terms2 == [], (plan2, terms2)
        assert len(project_sources.list_glossary_pins(s, pid)) == 1
    print(
        "  [C4] approving turns the SAME pin on, un-approving turns it back off "
        "(approval is enforced at run time, not captured at pin time)"
    )


def test_c_step_pin_still_wins_and_typed_autoattach_unchanged():
    from app.services import content_types

    with Session(engine) as s:
        cust = _customer(s, "Typed Corp")
        ct = content_types.create(s, "Marketing", customer_id=cust.id)
        typed = _project(s, "C typed", customer_id=cust.id, content_type_id=ct.id)
        auto = _mk_glossary(
            s, "C auto", cust.id, term=("dashboard", "AUTO-TABLEAU"), content_type_id=ct.id
        )
        pinned = _mk_glossary(s, "C pinned", cust.id, term=("dashboard", "PIN-TABLEAU"))
        auto_id, pin_id, tpid = auto.id, pinned.id, typed.id

        # THE BYTE-IDENTICAL CAPTURE: a typed project's IMPLICIT auto-attach plan, taken
        # with zero pin rows in existence. Whatever else this feature does, this must
        # not move -- it is exactly what rule 2 of build_glossary_plan promises.
        base_plan, base_terms = _terms(s, typed)
        assert auto_id in base_plan.glossary_ids, base_plan
        assert pin_id in base_plan.glossary_ids, "both are approved+general, so both auto-attach"
        assert base_terms and base_terms[0].endswith("TABLEAU"), base_terms

        # A STEP pin is still an exclusive override: only that glossary, no legacy.
        step_plan = gm.build_glossary_plan(s, typed, "en-US", "fr-FR", pinned_glossary_id=auto_id)
        assert step_plan.glossary_ids == (auto_id,), step_plan
        assert step_plan.include_legacy is False, step_plan
        step_terms = sorted(
            h["target_term"]
            for h in gm.match_terms_merged(
                s, tpid, "Please open the dashboard now.", "en-US", "fr-FR", step_plan
            )
        )
        assert step_terms == ["AUTO-TABLEAU"], step_terms
    print(
        "  [C5] a STEP pin is still an exclusive override (one glossary, no legacy, "
        "no auto-attach) -- unchanged"
    )

    with Session(engine) as s:
        typed = s.get(Project, tpid)
        project_sources.add_glossary_pin(s, tpid, pin_id)
        plan, terms = _terms(s, typed)
        assert plan.glossary_ids[0] == pin_id, (
            f"a project pin did not outrank auto-attach: {plan!r}"
        )
        assert set(plan.glossary_ids) == set(base_plan.glossary_ids), (
            f"pinning changed WHICH glossaries apply, not just their order: {plan!r}"
        )
        assert plan.include_legacy is True, plan
        # A step pin STILL wins over the project pin.
        step_plan = gm.build_glossary_plan(s, typed, "en-US", "fr-FR", pinned_glossary_id=auto_id)
        assert step_plan.glossary_ids == (auto_id,), step_plan
    print(
        "  [C6] on a TYPED project a project pin ranks FIRST (explicit > implicit) "
        "without changing the SET, and a step pin still beats it"
    )

    with Session(engine) as s:
        typed = s.get(Project, tpid)
        assert project_sources.remove_glossary_pin(s, tpid, pin_id) is True
        plan, terms = _terms(s, typed)
        if (plan, terms) != (base_plan, base_terms):
            _fail(
                "unpinning did NOT restore the typed project's implicit plan:\n"
                f"  base={base_plan!r} {base_terms!r}\n  now={plan!r} {terms!r}"
            )
    print(
        "  [C7] BYTE-IDENTICAL: with no pin rows a typed project's implicit "
        "auto-attach plan is exactly what it was before this feature existed"
    )


def test_c_glossary_pin_isolation():
    with Session(engine) as s:
        c1 = _customer(s, "Pin Ring One")
        c2 = _customer(s, "Pin Ring Two")
        proj = _project(s, "C ring", customer_id=c1.id)
        theirs = _mk_glossary(s, "C theirs", c2.id, term=("dashboard", "FOREIGN"))
        shared = _mk_glossary(s, "C shared", None, term=("dashboard", "SHARED"))
        pid, foreign_id, shared_id = proj.id, theirs.id, shared.id
        try:
            project_sources.add_glossary_pin(s, pid, foreign_id)
            _fail("a cross-customer glossary pin was accepted")
        except project_sources.AttachError as exc:
            assert "different customer" in str(exc), exc
        # A customer-LESS glossary is shared and pins fine.
        project_sources.add_glossary_pin(s, pid, shared_id)
        assert project_sources.pinned_glossary_ids(s, pid) == (shared_id,)
        # The candidate list never offers the foreign one.
        offered = {c["glossary_id"] for c in project_sources.glossary_pin_candidates(s, pid)}
        assert foreign_id not in offered, offered
    print(
        "  [C8] cross-customer glossary pin refused and never offered; a "
        "customer-less shared glossary pins fine"
    )

    # ...and a pin row planted around the write door reaches nothing at RUN time.
    _plant_pin(pid, foreign_id)
    with Session(engine) as s:
        assert project_sources.pinned_glossary_ids(s, pid) == (shared_id,), (
            "a PLANTED cross-customer pin reached the run-time plan"
        )
        proj = s.get(Project, pid)
        _, terms = _terms(s, proj)
        assert "FOREIGN" not in terms, terms
        assert {p["glossary_id"] for p in project_sources.list_glossary_pins(s, pid)} == {shared_id}
    print(
        "  [C9] a PLANTED cross-customer pin contributes nothing and is not listed "
        "(the same read-time re-derivation as the TM side)"
    )


def test_c_pin_candidates_respect_the_callers_own_visibility():
    """ISO-4. The project ring is not the only scope that governs a pin.

    A LEGACY UNASSIGNED glossary (customer_id NULL) is customer-less, so the project
    ring lets any project use it -- which is correct, it is the shared bucket. But
    /glossaries deliberately hides that whole section from a customer-BOUND user (its
    'Unassigned (needs a customer)' list is can_manage-and-unbound only). Without the
    caller scope, the pin picker became a second door onto exactly the rows the
    glossary module refuses to show them, and the POST accepted them too."""
    with Session(engine) as s:
        cust = _customer(s, "Bound Corp")
        proj = _project(s, "C bound", customer_id=cust.id)
        legacy = _mk_glossary(s, "C legacy unassigned", None, term=("dashboard", "LEGACY"))
        mine = _mk_glossary(s, "C bound own", cust.id, term=("dashboard", "BOUND-OWN"))
        pid, legacy_id, mine_id = proj.id, legacy.id, mine.id

        # UNBOUND staff (bound_customer_id None) -- the legacy glossary is offered,
        # exactly as before. This half must keep working or the fix is a regression.
        staff = {c["glossary_id"] for c in project_sources.glossary_pin_candidates(s, pid)}
        assert {legacy_id, mine_id} <= staff, staff
        project_sources.add_glossary_pin(s, pid, legacy_id)
        assert legacy_id in project_sources.pinned_glossary_ids(s, pid)
        assert project_sources.remove_glossary_pin(s, pid, legacy_id) is True

        # BOUND user (bound to this very customer, so nothing else is in the way):
        # their own customer's glossary is offered, the legacy unassigned one is not.
        bound = {
            c["glossary_id"]
            for c in project_sources.glossary_pin_candidates(s, pid, bound_customer_id=cust.id)
        }
        assert mine_id in bound, bound
        assert legacy_id not in bound, (
            "a customer-BOUND caller was offered a legacy unassigned glossary"
        )
        # ...and hand-posting the id is refused too -- the picker is not the gate.
        try:
            project_sources.add_glossary_pin(s, pid, legacy_id, bound_customer_id=cust.id)
            _fail("a bound caller PINNED a legacy unassigned glossary by id")
        except project_sources.AttachError as exc:
            # Same message as a ring refusal: a caller must not be able to tell a
            # glossary they may not see from one that does not exist.
            assert "different customer" in str(exc), exc
        assert project_sources.pinned_glossary_ids(s, pid) == ()
    print(
        "  [C10] a customer-BOUND caller neither sees nor can POST-pin a legacy "
        "unassigned glossary, while unbound staff still can"
    )


def test_c_org_leg_is_ungated_matching_auto_attach():
    """F5. The pin predicate and the auto-attach filter it sits beside must agree.

    build_glossary_plan's typed branch has ALWAYS applied its org filter ungated
    (`if proj_org is not None: where(Glossary.org_id == proj_org)`). The pin predicate
    originally gated its org leg on multi_org_active(), like the USER-facing org
    predicates in access.py -- so on an org-stamped install with the gate OFF (i.e.
    every install that ever ran _ensure_default_org) a pin could reach across orgs
    while auto-attach refused to. Two neighbours, same question, different answers."""
    with Session(engine) as s:
        org1 = Organization(name="Org PinGate 1")
        org2 = Organization(name="Org PinGate 2")
        s.add(org1)
        s.add(org2)
        s.commit()
        s.refresh(org1)
        s.refresh(org2)
        cust = _customer(s, "PinGate Corp", org_id=org1.id)
        proj = _project(s, "C pingate", customer_id=cust.id, org_id=org1.id)
        # Same CUSTOMER, different ORG -- so only the org leg can refuse it.
        foreign = _mk_glossary(
            s, "C pingate foreign", cust.id, term=("dashboard", "OTHER-ORG"), org_id=org2.id
        )
        same = _mk_glossary(
            s, "C pingate same", cust.id, term=("dashboard", "SAME-ORG"), org_id=org1.id
        )
        pid, foreign_id, same_id = proj.id, foreign.id, same.id

        assert not access.multi_org_active(), "this cell is about the GATE-OFF case"
        offered = {c["glossary_id"] for c in project_sources.glossary_pin_candidates(s, pid)}
        assert same_id in offered, offered
        assert foreign_id not in offered, (
            "gate OFF: an org-2 glossary was offered to an org-1 project"
        )
        try:
            project_sources.add_glossary_pin(s, pid, foreign_id)
            _fail("gate OFF: an org-2 glossary was PINNED to an org-1 project")
        except project_sources.AttachError:
            pass
        # The neighbour's answer, for the same pair: auto-attach would not take it
        # either. THAT is the property under test -- consistency, not strictness.
        assert access.project_can_use_glossary(proj, s.get(Glossary, same_id))
        assert not access.project_can_use_glossary(proj, s.get(Glossary, foreign_id))
    print(
        "  [C11] gate OFF: the pin predicate refuses a cross-ORG glossary, matching "
        "the ungated org filter build_glossary_plan has always applied"
    )


# ============================================================================
# [D] The web surface (auth ON)
# ============================================================================
from fastapi.testclient import TestClient

from app.main import app
from tests._csrf import csrf_for


def _login(c, who, pw):
    csrf_for(c)
    r = c.post(
        "/auth/login", data={"email": who, "password": pw, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303, f"login {who} -> {r.status_code}"
    csrf_for(c)
    return r


def test_d_web_roundtrip_and_viewer():
    with Session(engine) as s:
        cust = _customer(s, "Web Corp")
        other = _customer(s, "Web Other Corp")
        target = _project(s, "D target", customer_id=cust.id)
        source = _project(s, "D source", customer_id=cust.id)
        foreign = _project(s, "D foreign", customer_id=other.id)
        gl = _mk_glossary(s, "D gloss", cust.id)
        tpid, spid, fpid, gid = target.id, source.id, foreign.id, gl.id
        tm.write_back(s, spid, "Web attach probe text.", "WEB-TARGET", "en-US", "fr-FR")
    security.create_user("d-admin@x.com", "D Admin", Role.ADMIN, "d-admin-password-123")
    security.create_user("d-view@x.com", "D Viewer", Role.VIEWER, "d-viewer-password-123")
    # A viewer only reaches a project it is a MEMBER of (project scoping) — the point
    # of this cell is what a legitimate read-only user sees, not what an outsider sees.
    with Session(engine) as s:
        access.add_member(s, tpid, "d-view@x.com", added_by="d-admin@x.com")
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        _login(c, "d-admin@x.com", "d-admin-password-123")
        page = c.get("/projects/%d" % tpid).text  # noqa: UP031
        assert "Translation memory sources" in page and "Attached glossaries" in page
        # The candidate dropdown is filtered SERVER-SIDE: the foreign customer's
        # project name must not be in the HTML at all.
        assert "D source" in page, "the same-customer candidate was not offered"
        assert "D foreign" not in page, "a FOREIGN customer's project was offered"
        print(
            "  [D1] the project page renders both cards; the dropdown offers the "
            "same-customer project and never names the foreign one"
        )

        r = c.post(
            "/projects/%d/tm-sources/add" % tpid,  # noqa: UP031
            data={"source_project_id": str(spid)},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "tm_source_added" in r.headers["location"], r.headers
        r = c.post(
            "/projects/%d/glossaries/add" % tpid,  # noqa: UP031
            data={"glossary_id": str(gid)},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "glossary_pinned" in r.headers["location"], r.headers
        with Session(engine) as s:
            assert project_sources.allowed_tm_source_ids(s, tpid) == (spid,)
            assert project_sources.pinned_glossary_ids(s, tpid) == (gid,)
            assert tm.lookup(s, tpid, "Web attach probe text.", "en-US", "fr-FR")
        print(
            "  [D2] POST attach round-trips for BOTH kinds and the lookup path "
            "immediately reflects it"
        )

        # Submitting a foreign id ANYWAY (the dropdown is a suggestion, not a gate).
        r = c.post(
            "/projects/%d/tm-sources/add" % tpid,  # noqa: UP031
            data={"source_project_id": str(fpid)},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "err=tm_source" in r.headers["location"], r.headers
        with Session(engine) as s:
            assert project_sources.allowed_tm_source_ids(s, tpid) == (spid,)
        print(
            "  [D3] hand-submitting a FOREIGN project id is refused server-side "
            "(the dropdown is not the gate)"
        )

        v = TestClient(app)
        _login(v, "d-view@x.com", "d-viewer-password-123")
        vpage = v.get("/projects/%d" % tpid).text  # noqa: UP031
        assert "Translation memory sources" in vpage, "a viewer lost the list entirely"
        assert "D source" in vpage, "a viewer cannot see the current sources"
        assert "/tm-sources/add" not in vpage and "/tm-sources/remove" not in vpage, (
            "a viewer was offered the attach/detach forms"
        )
        assert "/glossaries/add" not in vpage and "/glossaries/remove" not in vpage, (
            "a viewer was offered the glossary forms"
        )
        rv = v.post(
            "/projects/%d/tm-sources/remove" % tpid,  # noqa: UP031
            data={"source_project_id": str(spid)},
            follow_redirects=False,
        )
        assert rv.status_code == 403, f"a viewer's detach POST returned {rv.status_code}"
        with Session(engine) as s:
            assert project_sources.allowed_tm_source_ids(s, tpid) == (spid,)
        print(
            "  [D4] a VIEWER sees both lists read-only: no forms in the HTML and the "
            "POST itself is 403 (the template gate is not the only gate)"
        )

        r = c.post(
            "/projects/%d/tm-sources/remove" % tpid,  # noqa: UP031
            data={"source_project_id": str(spid)},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        r = c.post(
            "/projects/%d/glossaries/remove" % tpid,  # noqa: UP031
            data={"glossary_id": str(gid)},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code
        with Session(engine) as s:
            assert project_sources.allowed_tm_source_ids(s, tpid) == ()
            assert project_sources.pinned_glossary_ids(s, tpid) == ()
        print("  [D5] POST detach round-trips for both kinds")
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()


def test_d_oversized_ids_and_honest_banners():
    """ISO-5 (overflow) + F9 (no-op detach) + F4/F6 (disclosure) on the real routes."""
    from app.services import content_types

    with Session(engine) as s:
        cust = _customer(s, "Banner Corp")
        ct_a = content_types.create(s, "BannerMarketing", customer_id=cust.id)
        ct_b = content_types.create(s, "BannerLegal", customer_id=cust.id)
        target = _project(s, "D banner target", customer_id=cust.id, content_type_id=ct_a.id)
        # A source whose rows are all stamped with the OTHER content type, so the
        # attach succeeds and matches nothing -- F4's silent no-op.
        source = _project(s, "D banner source", customer_id=cust.id, content_type_id=ct_b.id)
        tpid, spid = target.id, source.id
        for i in range(3):
            row = SegmentTM(
                project_id=spid,
                source_locale="en-US",
                target_locale="fr-FR",
                source_text="Legal clause %d." % i,  # noqa: UP031
                target_text="CLAUSE-%d" % i,  # noqa: UP031
                source_hash=tm.hash_text("Legal clause %d." % i, "en-US", "fr-FR"),  # noqa: UP031
                content_type_id=ct_b.id,
            )
            s.add(row)
        s.commit()
        # A glossary of the OTHER content type, pinnable but mismatched (F6).
        mismatched = _mk_glossary(
            s,
            "D other-type gloss",
            cust.id,
            term=("dashboard", "LEGAL-TABLEAU"),
            content_type_id=ct_b.id,
        )
        gid = mismatched.id
    security.create_user("d2-admin@x.com", "D2 Admin", Role.ADMIN, "d2-admin-password-123")
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        _login(c, "d2-admin@x.com", "d2-admin-password-123")

        # --- ISO-5: an absurd id must not 500 any of the four routes. `int()` alone
        # accepts it and the SQLite binder then raises OverflowError several layers
        # down; the bound now lives in the service so all four inherit it.
        huge = "9" * 40
        for path, field in (
            ("tm-sources/add", "source_project_id"),
            ("tm-sources/remove", "source_project_id"),
            ("glossaries/add", "glossary_id"),
            ("glossaries/remove", "glossary_id"),
        ):
            r = c.post("/projects/%d/%s" % (tpid, path), data={field: huge}, follow_redirects=False)  # noqa: UP031
            assert r.status_code == 303, (
                f"{path} returned {r.status_code} for an oversized id (expected a clean 303)"
            )
            # And a negative / non-numeric one, the other two shapes of the same bug.
            for bad in ("-1", "0", "not-a-number", ""):
                r = c.post(
                    "/projects/%d/%s" % (tpid, path),  # noqa: UP031
                    data={field: bad},
                    follow_redirects=False,  # noqa: RUF100, UP031
                )
                assert r.status_code == 303, f"{path} returned {r.status_code} for id {bad!r}"
        print(
            "  [D6] all four POST routes answer 303 (not 500) for oversized, "
            "negative, zero, non-numeric and blank ids"
        )

        # --- F9: detaching something that was never attached must not claim success.
        r = c.post(
            "/projects/%d/tm-sources/remove" % tpid,  # noqa: UP031
            data={"source_project_id": str(spid)},
            follow_redirects=False,
        )
        assert "tm_source_absent" in r.headers["location"], r.headers["location"]
        page = c.get("/projects/%d?tm_source_absent=1" % tpid).text  # noqa: UP031
        assert "was not attached" in page, "the no-op detach banner is missing"
        r = c.post(
            "/projects/%d/glossaries/remove" % tpid,  # noqa: UP031
            data={"glossary_id": str(gid)},
            follow_redirects=False,
        )
        assert "glossary_pin_absent" in r.headers["location"], r.headers["location"]
        print(
            "  [D7] a detach of something that was never attached reports 'was not "
            "attached' instead of a false success"
        )

        # --- F4: the content-type no-op is disclosed, with the REACHABLE count.
        r = c.post(
            "/projects/%d/tm-sources/add" % tpid,  # noqa: UP031
            data={"source_project_id": str(spid)},
            follow_redirects=False,
        )
        loc = r.headers["location"]
        assert "tm_src_n=0" in loc and "tm_src_total=3" in loc, loc
        page = c.get(
            "/projects/%d%s" % (tpid, loc.split("?", 1)[1] and "?" + loc.split("?", 1)[1])  # noqa: UP031
        ).text
        assert "0 of 3 entries are reachable" in page, (
            "the attach banner did not disclose the content-type shortfall"
        )
        assert "will not match anything" in page, page[:200]
        # The CARD shows the reachable count too, not the source's own 3.
        card = c.get("/projects/%d" % tpid).text  # noqa: UP031
        assert "of 3 (content type)" in card, "the card hid the reachable/total split"
        print(
            "  [D8] a content-type-mismatched attach is reported honestly: "
            "'0 of 3 entries are reachable', and the card shows the same split"
        )

        # --- F6: a wrong-content-type glossary pin is LABELLED (the precedence is
        # kept -- explicit pins win -- but it stops being a silent surprise).
        page = c.get("/projects/%d" % tpid).text  # noqa: UP031
        assert "other content type: BannerLegal" in page, (
            "the pin dropdown did not label a content-type mismatch"
        )
        r = c.post(
            "/projects/%d/glossaries/add" % tpid,  # noqa: UP031
            data={"glossary_id": str(gid)},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "glossary_pinned" in r.headers["location"]
        page = c.get("/projects/%d" % tpid).text  # noqa: UP031
        assert "other content type: BannerLegal" in page, (
            "the PINNED list did not label the content-type mismatch"
        )
        assert "outrank this project's own" in page, (
            "the pinned list did not disclose that the pin outranks own glossaries"
        )
        # ...and it really does still win, which is why the label matters.
        with Session(engine) as s:
            proj = s.get(Project, tpid)
            plan = gm.build_glossary_plan(s, proj, "en-US", "fr-FR")
            assert plan.glossary_ids and plan.glossary_ids[0] == gid, plan
        print(
            "  [D9] a wrong-content-type glossary is labelled in BOTH the dropdown "
            "and the pinned list, and still outranks the project's own (kept "
            "precedence, added disclosure)"
        )
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()


# ============================================================================
# [E] The CLI surface
# ============================================================================
def _cli(argv):
    """Run one CLI command in-process; returns (exit_code, stdout)."""
    import contextlib
    import io

    from app.cli.__main__ import main as cli_main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, buf.getvalue()


def test_e_cli_roundtrip():
    with Session(engine) as s:
        cust = _customer(s, "CLI Corp")
        other = _customer(s, "CLI Other Corp")
        target = _project(s, "E target", customer_id=cust.id)
        source = _project(s, "E source", customer_id=cust.id)
        foreign = _project(s, "E foreign", customer_id=other.id)
        gl = _mk_glossary(s, "E gloss", cust.id)
        tpid, spid, fpid, gid = target.id, source.id, foreign.id, gl.id
        tm.write_back(s, spid, "CLI attach probe text.", "CLI-TARGET", "en-US", "fr-FR")

    code, out = _cli(["project", "tm-sources", "add", str(tpid), str(spid)])
    assert code == 0, (code, out)
    assert "Nothing was copied" in out, out
    code, out = _cli(["project", "tm-sources", "list", str(tpid)])
    assert code == 0 and "E source" in out, (code, out)
    assert "en-US -> fr-FR" in out, out
    with Session(engine) as s:
        assert project_sources.allowed_tm_source_ids(s, tpid) == (spid,)
        assert tm.lookup(s, tpid, "CLI attach probe text.", "en-US", "fr-FR")
    print(
        "  [E1] `project tm-sources add/list` round-trips in-process and the lookup "
        "path reflects it"
    )

    code, out = _cli(["project", "tm-sources", "add", str(tpid), str(fpid)])
    assert code != 0, f"the CLI accepted a cross-customer attach: {out}"
    assert "different customer" in out, out
    code, out = _cli(["project", "tm-sources", "add", str(tpid), str(tpid)])
    assert code != 0 and "own translation memory" in out, (code, out)
    print("  [E2] the CLI refuses the SAME things the web does (cross-customer, self)")

    code, out = _cli(["project", "glossaries", "add", str(tpid), str(gid)])
    assert code == 0, (code, out)
    code, out = _cli(["--json", "project", "glossaries", "list", str(tpid)])
    assert code == 0 and '"glossary_id"' in out, (code, out)
    import json as _json

    payload = _json.loads(out)
    assert payload["glossaries"][0]["glossary_id"] == gid, payload
    assert payload["glossaries"][0]["active"] is True, payload
    print("  [E3] `project glossaries add/list` round-trips, and --json carries the applied flag")

    code, out = _cli(["project", "glossaries", "remove", str(tpid), str(gid)])
    assert code == 0 and "Detached glossary" in out, (code, out)
    code, out = _cli(["project", "tm-sources", "remove", str(tpid), str(spid)])
    assert code == 0, (code, out)
    code, out = _cli(["project", "tm-sources", "remove", str(tpid), str(spid)])
    assert code == 0 and "has no TM source" in out, (code, out)
    with Session(engine) as s:
        assert project_sources.allowed_tm_source_ids(s, tpid) == ()
        assert project_sources.pinned_glossary_ids(s, tpid) == ()
    print("  [E4] both remove verbs round-trip and are idempotent")

    # --remote is refused loudly rather than silently running against the host.
    code, out = _cli(
        [
            "--remote",
            "http://127.0.0.1:1",
            "--token",
            "x",
            "project",
            "tm-sources",
            "list",
            str(tpid),
        ]
    )
    assert code == 2, f"expected a loud --remote refusal, got {code}: {out}"
    print("  [E5] --remote is refused (exit 2), not silently run locally")


def test_e_cli_output_is_ascii():
    """ASCII-safe CLI output (Windows consoles). Every new verb's success AND failure
    text is checked, because an em-dash smuggled into an error message breaks a real
    terminal just as thoroughly as one in a table."""
    with Session(engine) as s:
        cust = _customer(s, "Ascii Corp")
        t = _project(s, "F target", customer_id=cust.id)
        src = _project(s, "F source", customer_id=cust.id)
        gl = _mk_glossary(s, "F gloss", cust.id, approved=False)
        tpid, spid, gid = t.id, src.id, gl.id
        tm.write_back(s, spid, "Ascii probe.", "CIBLE", "en-US", "fr-FR")
    runs = [
        ["project", "tm-sources", "add", str(tpid), str(spid)],
        ["project", "tm-sources", "list", str(tpid)],
        ["project", "tm-sources", "add", str(tpid), str(tpid)],  # refusal text
        ["project", "glossaries", "add", str(tpid), str(gid)],  # draft note
        ["project", "glossaries", "list", str(tpid)],
        ["project", "glossaries", "remove", str(tpid), str(gid)],
        ["project", "tm-sources", "remove", str(tpid), str(spid)],
    ]
    for argv in runs:
        _code, out = _cli(argv)
        try:
            out.encode("ascii")
        except UnicodeEncodeError as exc:
            _fail("non-ASCII in `{}` output: {}\n{}".format(" ".join(argv), exc, out))
    print("  [E6] every new verb's output (success AND refusal) is pure ASCII")


def test_d_concordance_route_gives_a_member_nothing_of_the_source():
    """ISO-3 at the ROUTE, with the identity that makes it a finding.

    `GET /concordance?project_id=…` is gated ONLY by _visibility_block on the project
    named in the query — i.e. on the BORROWING project. So the user below (a member of
    the target, with no membership of, and no way to open, the source) passes that gate
    legitimately. If concordance honoured attachments, this exact request is the one
    that walks the source project's memory. The service-level cell [B12] proves the
    scope; this proves the route inherits it and that the caller really is who the
    finding described."""
    with Session(engine) as s:
        cust = _customer(s, "Route Concord Corp")
        source = _project(s, "D concord source", customer_id=cust.id)
        target = _project(s, "D concord target", customer_id=cust.id)
        spid, tpid = source.id, target.id
        for i in range(3):
            tm.write_back(
                s,
                spid,
                "Board minutes item %d." % i,  # noqa: UP031
                "SECRET-MINUTE-%d" % i,  # noqa: UP031
                "en-US",
                "fr-FR",  # noqa: RUF100, UP031
            )
        tm.write_back(s, tpid, "Own public line.", "PUBLIQUE", "en-US", "fr-FR")
        project_sources.add_tm_source(s, tpid, spid)
    security.create_user("d-conc@x.com", "D Concord", Role.TRANSLATOR, "d-concord-password-123")
    with Session(engine) as s:
        # A member of the TARGET only. Never added to the source.
        access.add_member(s, tpid, "d-conc@x.com", added_by="system")
    settings.set_many({"security_auth_required": "1", "project_scoping": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        _login(c, "d-conc@x.com", "d-concord-password-123")
        # They can reach the target's concordance, and it answers about the target.
        r = c.get(
            "/concordance",
            params={"project_id": tpid, "q": "publique", "source": "en-US", "target": "fr-FR"},
        )
        assert r.status_code == 200, r.status_code
        assert len(r.json()["results"]) == 1, r.json()
        # ...and every probe at the SOURCE's content comes back empty.
        for probe in ("secret", "minute", "board", "item"):
            r = c.get(
                "/concordance",
                params={"project_id": tpid, "q": probe, "source": "en-US", "target": "fr-FR"},
            )
            assert r.status_code == 200, r.status_code
            assert r.json()["results"] == [], (
                "the concordance route leaked source rows for {!r}: {!r}".format(
                    probe, r.json()["results"]
                )
            )
        # The other door is shut too: they cannot simply name the source project.
        r = c.get(
            "/concordance",
            params={"project_id": spid, "q": "secret", "source": "en-US", "target": "fr-FR"},
        )
        assert r.status_code in (303, 403, 404), (
            f"a non-member reached the SOURCE project's concordance directly ({r.status_code})"
        )
        # Matching still works for that same user's project (the feature is intact).
        with Session(engine) as s:
            hit = tm.lookup(s, tpid, "Board minutes item 1.", "en-US", "fr-FR")
            assert hit and hit["target_text"] == "SECRET-MINUTE-1", hit
    finally:
        settings.set_many({"security_auth_required": "0", "project_scoping": "0"})
        settings.invalidate_cache()
    print(
        "  [D10] a TARGET-only member gets ZERO source rows from the /concordance "
        "route (4 probes) and cannot name the source project either, while the "
        "project's lookups still match it"
    )


# ============================================================================
# [H] Ownership: borrowed rows are read, never written
# ============================================================================
def test_h_usage_counters_stay_with_the_owner():
    """Borrowed applications refresh retention activity without changing use_count.

    Owner curation counters remain private; retention must preserve accepted reuse.
    """
    from app.models import DocStatus, Document, Segment, SegStatus, TransMode

    with Session(engine) as s:
        cust = _customer(s, "Usage Corp")
        source = _project(s, "H usage source", customer_id=cust.id)
        target = _project(s, "H usage target", customer_id=cust.id)
        spid, tpid = source.id, target.id
        SRC = "Apply this borrowed sentence."
        tm.write_back(s, spid, SRC, "EMPRUNTE", "en-US", "fr-FR")
        # The target has its OWN row for a different string, so the run applies one of
        # each and we can prove the bump reached the right one.
        OWN = "Apply this local sentence."
        tm.write_back(s, tpid, OWN, "LOCALE", "en-US", "fr-FR")
        project_sources.add_tm_source(s, tpid, spid)
        for row in s.exec(select(SegmentTM).where(SegmentTM.project_id.in_([spid, tpid]))):
            row.activity_at = None
            s.add(row)
        s.commit()

    with Session(engine) as s:
        doc = Document(
            project_id=tpid,
            filename="usage.txt",
            fmt="txt",
            mode=TransMode.SEGMENTED,
            status=DocStatus.NEW,
        )
        s.add(doc)
        s.commit()
        s.refresh(doc)
        for i, txt in enumerate((SRC, OWN)):
            s.add(
                Segment(
                    document_id=doc.id,
                    order_index=i,
                    source_text=txt,
                    target_locale="fr-FR",
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        did = doc.id

    # Drive the real apply seam (the same one the editor's "apply TM" button calls).
    from app.services import content as content_svc

    with Session(engine) as s:
        applied = content_svc.apply_tm_to_document(
            s, s.get(Document, did), s.get(Project, tpid), "fr-FR"
        )
    assert applied == 2, f"both segments should have been filled from TM, got {applied!r}"

    with Session(engine) as s:
        borrowed = s.exec(select(SegmentTM).where(SegmentTM.project_id == spid)).all()
        own = s.exec(select(SegmentTM).where(SegmentTM.project_id == tpid)).all()
        assert len(borrowed) == 1 and len(own) == 1, (borrowed, own)
        assert borrowed[0].use_count == 0, (
            "a borrower's run bumped the SOURCE project's use_count to %d" % borrowed[0].use_count  # noqa: UP031
        )
        assert borrowed[0].last_used_at is None
        assert borrowed[0].activity_at is not None, (
            "accepted borrowed TM must refresh retention activity"
        )
        # ...while the target's OWN applied row IS counted, so the cell cannot pass by
        # the bump being broken everywhere.
        assert own[0].use_count == 1, (
            "the project's OWN applied row was not counted: %d" % own[0].use_count  # noqa: UP031
        )
        assert own[0].last_used_at is not None
        # The borrowed target really was written to the segment -- the read half works.
        segs = s.exec(
            select(Segment).where(Segment.document_id == did).order_by(Segment.order_index)
        ).all()
        assert segs[0].target_text == "EMPRUNTE", segs[0].target_text
    print(
        "  [H1] an end-to-end apply writes the BORROWED target into the segment but "
        "leaves the source project's use_count at 0, refreshes retention activity, and "
        "project's own applied row is counted normally"
    )


def test_h_delete_cascades_both_link_directions():
    """ISO-1. Deleting a project that is either END of a link must not wedge.

    Both tables carry real FKs to project.id with SQLite FK enforcement ON, so a
    missing cascade raises IntegrityError at the FINAL delete -- after the documents,
    the segments and the whole project TM have already been COMMITTED by earlier
    steps. That is the husk this file's step-8b comment was written about: the project
    row survives, its content does not, and every retry fails identically."""
    from app.services import content as content_svc

    with Session(engine) as s:
        cust = _customer(s, "Cascade Corp")
        a = _project(s, "H cascade A", customer_id=cust.id)  # borrows from B
        b = _project(s, "H cascade B", customer_id=cust.id)  # lends to A
        c = _project(s, "H cascade C", customer_id=cust.id)  # pins a glossary
        gl = _mk_glossary(s, "H cascade gloss", cust.id)
        aid, bid, cid, gid = a.id, b.id, c.id, gl.id
        tm.write_back(s, bid, "Cascade probe row.", "CIBLE", "en-US", "fr-FR")
        project_sources.add_tm_source(s, aid, bid)
        project_sources.add_glossary_pin(s, cid, gid)

        # The preview names all three losses BEFORE the confirm.
        pv_a = content_svc.delete_project_preview(s, aid)
        pv_b = content_svc.delete_project_preview(s, bid)
        pv_c = content_svc.delete_project_preview(s, cid)
        assert pv_a["cascade"]["tm_sources_attached"] == 1, pv_a["cascade"]
        assert pv_a["cascade"]["tm_source_uses"] == 0, pv_a["cascade"]
        # B is somebody else's SOURCE -- the leg a hand-written cascade forgets, and
        # the number that tells an operator another project loses coverage.
        assert pv_b["cascade"]["tm_source_uses"] == 1, pv_b["cascade"]
        assert pv_b["cascade"]["tm_sources_attached"] == 0, pv_b["cascade"]
        assert pv_c["cascade"]["glossary_pins"] == 1, pv_c["cascade"]
        # None of the three BLOCKS the delete (blockers are for independent referrers).
        assert pv_a["blockers"]["total"] == 0 and pv_b["blockers"]["total"] == 0

    # SHAPE 1: delete the project that IS a source (the forgotten direction).
    with Session(engine) as s:
        assert content_svc.delete_project(s, bid) is not None
    with Session(engine) as s:
        assert s.get(Project, bid) is None, "the source project survived as a husk"
        assert (
            s.exec(select(ProjectTmSource).where(ProjectTmSource.source_project_id == bid)).all()
            == []
        )
        # The borrower degrades exactly as a manual detach would: its own rows only.
        assert project_sources.allowed_tm_source_ids(s, aid) == ()
        assert tm.lookup(s, aid, "Cascade probe row.", "en-US", "fr-FR") is None
        assert s.get(Project, aid) is not None, "the BORROWER was deleted too"
    print(
        "  [H2] deleting a project that is somebody's SOURCE cascades cleanly and "
        "degrades the borrower to its own memory (it is not itself deleted)"
    )

    # SHAPE 2: delete the project that HAS a source. SHAPE 3: one that pins a glossary.
    with Session(engine) as s:
        d = _project(s, "H cascade D", customer_id=s.get(Project, aid).customer_id)
        did = d.id
        tm.write_back(s, did, "Second cascade row.", "CIBLE2", "en-US", "fr-FR")
        project_sources.add_tm_source(s, aid, did)
    with Session(engine) as s:
        assert content_svc.delete_project(s, aid) is not None
    with Session(engine) as s:
        assert s.get(Project, aid) is None
        assert s.exec(select(ProjectTmSource).where(ProjectTmSource.project_id == aid)).all() == []
        assert s.get(Project, did) is not None, "the SOURCE was deleted with its borrower"
    with Session(engine) as s:
        assert content_svc.delete_project(s, cid) is not None
    with Session(engine) as s:
        assert s.get(Project, cid) is None
        assert s.exec(select(ProjectGlossary).where(ProjectGlossary.project_id == cid)).all() == []
        assert s.get(Glossary, gid) is not None, "the GLOSSARY was deleted with the pin"
    print(
        "  [H3] all three shapes delete cleanly (as borrower, as source, as pinner) "
        "and neither the source project nor the glossary is collateral"
    )


def test_h_pinned_glossary_stays_deletable():
    """ISO-2. A pin must not make a glossary permanently undeletable.

    glossary_module.delete already unpinned WORKFLOW STEPS; ProjectGlossary is a second
    FK it did not know about, so every attempt to delete a pinned glossary raised
    IntegrityError at the final DELETE -- with nothing on the glossary page naming the
    project responsible, so the operator had no way to clear it either."""
    with Session(engine) as s:
        cust = _customer(s, "Undeletable Corp")
        proj = _project(s, "H undeletable", customer_id=cust.id)
        gl = _mk_glossary(s, "H pinned gloss", cust.id)
        pid, gid = proj.id, gl.id
        project_sources.add_glossary_pin(s, pid, gid)
        assert project_sources.pinned_glossary_ids(s, pid) == (gid,)
    with Session(engine) as s:
        gm.delete(s, gid)  # must not raise
    with Session(engine) as s:
        assert s.get(Glossary, gid) is None, "the glossary survived its own delete"
        assert (
            s.exec(select(ProjectGlossary).where(ProjectGlossary.glossary_id == gid)).all() == []
        ), "the pin row outlived its glossary (a dangling FK)"
        assert project_sources.pinned_glossary_ids(s, pid) == ()
        assert s.get(Project, pid) is not None, "the PROJECT was deleted with the pin"
        # The project still renders + plans without its late glossary.
        plan = gm.build_glossary_plan(s, s.get(Project, pid), "en-US", "fr-FR")
        assert plan.glossary_ids == (), plan
    print(
        "  [H4] a PINNED glossary deletes cleanly, its pin rows go with it, and the "
        "project plans normally afterwards"
    )


def test_h_other_fk_referrers_do_not_wedge_a_delete():
    """NEW-5. Two more real FKs the isolation reviewer found, BOTH pre-dating this
    branch: TermCandidate.glossary_id (NOT NULL) and MemoryEntry/MemoryEvent.project_id.

    Same failure mode as ISO-1/ISO-2 -- IntegrityError at the final DELETE, with no
    surface naming the referrer -- so they are closed here rather than left as a known
    way to wedge the two deletes this branch already touches. The two get DIFFERENT
    treatment on purpose (see below), which is the interesting part."""
    from app.models import MemoryEntry, MemoryEvent, TermCandidate
    from app.services import content as content_svc

    # (a) TermCandidate: OWNED by the glossary -> cascades with it. A candidate is a
    # proposal INTO that glossary, and the accepted ones already exist as real entries.
    with Session(engine) as s:
        cust = _customer(s, "Candidate Corp")
        g = _mk_glossary(s, "H candidate gloss", cust.id)
        gid = g.id
        s.add(TermCandidate(glossary_id=gid, source_term="widget"))
        s.add(TermCandidate(glossary_id=gid, source_term="sprocket", status="dropped"))
        s.commit()
        assert len(s.exec(select(TermCandidate).where(TermCandidate.glossary_id == gid)).all()) == 2
    with Session(engine) as s:
        gm.delete(s, gid)  # must not raise
    with Session(engine) as s:
        assert s.get(Glossary, gid) is None, "the glossary survived its own delete"
        assert s.exec(select(TermCandidate).where(TermCandidate.glossary_id == gid)).all() == [], (
            "candidate rows outlived their glossary (a dangling FK)"
        )
    print(
        "  [H5] a glossary that has been through term extraction deletes cleanly; "
        "its candidates cascade with it"
    )

    # (b) LLMemory: owned by the CUSTOMER, not the project -- customer_id is NOT NULL
    # by design (the hard partition) and the project is only the narrower scope the
    # entry was learned in. So the rows are UN-SCOPED, not deleted: deleting a project
    # must not destroy what the customer learned.
    with Session(engine) as s:
        cust2 = _customer(s, "Memory Corp")
        proj = _project(s, "H memory proj", customer_id=cust2.id)
        pid, cid = proj.id, cust2.id
        s.add(
            MemoryEntry(
                customer_id=cid,
                project_id=pid,
                kind="term",
                source_locale="en-US",
                target_locale="fr-FR",
                intent="prefer 'tableau'",
            )
        )
        s.add(MemoryEvent(customer_id=cid, project_id=pid, kind="observed"))
        # A second entry with NO project scope, so we can prove the update is targeted.
        s.add(
            MemoryEntry(
                customer_id=cid,
                project_id=None,
                kind="term",
                source_locale="en-US",
                target_locale="fr-FR",
                intent="customer-wide rule",
            )
        )
        s.commit()
    with Session(engine) as s:
        assert content_svc.delete_project(s, pid) is not None
    with Session(engine) as s:
        assert s.get(Project, pid) is None, "the project survived as a husk"
        entries = s.exec(select(MemoryEntry).where(MemoryEntry.customer_id == cid)).all()
        events = s.exec(select(MemoryEvent).where(MemoryEvent.customer_id == cid)).all()
        assert len(entries) == 2, f"LLMemory entries were destroyed: {entries!r}"
        assert len(events) == 1, events
        assert all(e.project_id is None for e in entries), [e.project_id for e in entries]
        assert events[0].project_id is None
        # The learned content itself is intact -- un-scoped, not blanked.
        assert {e.intent for e in entries} == {"prefer 'tableau'", "customer-wide rule"}
    print(
        "  [H6] deleting a project UN-SCOPES its LLMemory rows to the customer "
        "(project_id -> NULL) instead of destroying what the customer learned"
    )


# ============================================================================
# [F] The template rider: Documents above Members
# ============================================================================
def test_f_documents_render_above_members():
    with Session(engine) as s:
        cust = _customer(s, "Order Corp")
        proj = _project(s, "F order", customer_id=cust.id)
        pid = proj.id
    security.create_user("f-admin@x.com", "F Admin", Role.ADMIN, "f-admin-password-123")
    settings.set_many({"security_auth_required": "1"})
    settings.invalidate_cache()
    try:
        c = TestClient(app)
        _login(c, "f-admin@x.com", "f-admin-password-123")
        html = c.get("/projects/%d" % pid).text  # noqa: UP031
        docs_at = html.find("<h2>Documents</h2>")
        members_at = html.find("<h2>Members</h2>")
        assert docs_at != -1, "the Documents section did not render at all"
        assert members_at != -1, "the Members section did not render (auth is ON)"
        assert docs_at < members_at, (
            "Documents must render ABOVE Members (Documents at %d, Members at %d)"  # noqa: UP031
            % (docs_at, members_at)
        )
        # The full card order (2026-09-24): Documents sits directly after
        # "Upload a document" and before "Target languages"; the TM + Glossary pair
        # stays side by side in one grid2 row.
        order = [
            "<h2>Overview</h2>",
            "<h2>Monthly spend budget</h2>",
            "<h2>Upload a document</h2>",
            "<h2>Documents</h2>",
            "<h2>Target languages</h2>",
            '<div class="grid2">',
            "<h2>Translation memory</h2>",
            "<h2>Glossary",
            "<h2>Members</h2>",
        ]
        positions = [html.find(marker) for marker in order]
        assert -1 not in positions, list(zip(order, positions, strict=True))
        assert positions == sorted(positions), (
            "project cards out of order: %r" % list(zip(order, positions, strict=True))  # noqa: UP031
        )
        grid = html[positions[5] : positions[8]]
        assert grid.count("<h2>") == 2, "the grid2 row must hold exactly TM + Glossary"
        # Both sections still carry their own contents unchanged.
        assert "No documents yet" in html and "Add member by email" in html, html[:200]
    finally:
        settings.set_many({"security_auth_required": "0"})
        settings.invalidate_cache()
    print("  [F1] project cards in order: Upload, Documents, Target languages, TM+Glossary")


# ============================================================================
# [G] The migration
# ============================================================================
def _upgrade(target: str) -> str:
    """`alembic upgrade <target>` against a FRESH temp data dir; returns the URL."""
    from alembic.config import Config

    from alembic import command

    fresh = tempfile.mkdtemp(prefix="loctms_projsrc_db_")
    _TMPDIRS.append(fresh)
    url = "sqlite:///" + os.path.join(fresh, "loc_tms.db").replace("\\", "/")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    prev = os.environ.get("LOC_TMS_DATA_DIR")
    os.environ["LOC_TMS_DATA_DIR"] = fresh
    try:
        command.upgrade(cfg, target)
    finally:
        if prev is not None:
            os.environ["LOC_TMS_DATA_DIR"] = prev
    return url


def _snapshot(url: str) -> dict:
    eng = create_engine(url)
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    columns, indexes = {}, set()
    for table in sorted(tables):
        for col in insp.get_columns(table):
            columns[(table, col["name"])] = bool(col["nullable"])
    with eng.connect() as conn:
        for name, tbl, _sql in conn.execute(
            text(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ).fetchall():
            indexes.add((tbl, name))
    eng.dispose()
    return {"tables": tables, "columns": columns, "indexes": indexes}


def test_g_revision_adds_and_only_adds():
    before = _snapshot(_upgrade(PREV_HEAD))
    # Pinned to NEW_HEAD, not "head": the claim is about ONE revision, and it must keep
    # meaning that after the next revision stacks on top (verify_keys_inertness [A]'s
    # lesson, which was written the day "head" silently stopped being the right target).
    after = _snapshot(_upgrade(NEW_HEAD))

    new_tables = after["tables"] - before["tables"]
    assert new_tables == EXPECTED_NEW_TABLES, (
        f"revision {NEW_HEAD} adds tables {sorted(new_tables)}, expected exactly {sorted(EXPECTED_NEW_TABLES)}"  # noqa: E501
    )
    assert not (before["tables"] - after["tables"]), "the revision DROPPED a table"

    # Not one PRE-EXISTING column changed (name set or nullability).
    pre_before = {k: v for k, v in before["columns"].items()}  # noqa: C416
    pre_after = {k: v for k, v in after["columns"].items() if k[0] not in EXPECTED_NEW_TABLES}
    assert pre_after == pre_before, (
        f"the revision disturbed pre-existing columns: {sorted(set(pre_after.items()) ^ set(pre_before.items()))}"  # noqa: E501
    )

    new_ix = {i for i in after["indexes"] - before["indexes"]}  # noqa: C416
    assert new_ix == EXPECTED_NEW_INDEXES, (
        f"revision {NEW_HEAD} adds indexes {sorted(new_ix)}, expected exactly {sorted(EXPECTED_NEW_INDEXES)}"  # noqa: E501
    )
    print(
        "  [G1] revision %s adds %d tables / %d indexes and changes NOTHING else"  # noqa: UP031
        % (NEW_HEAD, len(new_tables), len(new_ix))
    )

    # The pair uniques are real: a duplicate link cannot be stored even by raw SQL.
    eng = create_engine(_upgrade(NEW_HEAD))
    with eng.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO project (name, source_locale, default_provider, "
                "provider_mode, project_type, status, created_by, created_at) "
                "VALUES ('p1','en-US','claude','uniform','document','in_translation','', "
                "'2026-01-01 00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO project (name, source_locale, default_provider, "
                "provider_mode, project_type, status, created_by, created_at) "
                "VALUES ('p2','en-US','claude','uniform','document','in_translation','', "
                "'2026-01-01 00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO projecttmsource (project_id, source_project_id, created_by, "
                "created_at) VALUES (1, 2, '', '2026-01-01 00:00:00')"
            )
        )
        dup_refused = False
        try:
            conn.execute(
                text(
                    "INSERT INTO projecttmsource (project_id, source_project_id, "
                    "created_by, created_at) VALUES (1, 2, '', '2026-01-01 00:00:00')"
                )
            )
        except Exception:
            dup_refused = True
        assert dup_refused, "ux_projecttmsource_pair did not refuse a duplicate pair"
    eng.dispose()
    print("  [G2] ux_projecttmsource_pair refuses a duplicate pair at the DB level")


def test_g_downgrade_restores_the_previous_head():
    from alembic.config import Config

    from alembic import command

    fresh = tempfile.mkdtemp(prefix="loctms_projsrc_down_")
    _TMPDIRS.append(fresh)
    url = "sqlite:///" + os.path.join(fresh, "loc_tms.db").replace("\\", "/")
    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    prev = os.environ.get("LOC_TMS_DATA_DIR")
    os.environ["LOC_TMS_DATA_DIR"] = fresh
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, PREV_HEAD)
    finally:
        if prev is not None:
            os.environ["LOC_TMS_DATA_DIR"] = prev
    reference = _snapshot(_upgrade(PREV_HEAD))
    got = _snapshot(url)
    assert got["tables"] == reference["tables"], "downgrade left tables behind: {}".format(
        sorted(got["tables"] ^ reference["tables"])
    )
    assert got["indexes"] == reference["indexes"], "downgrade left indexes behind: {}".format(
        sorted(got["indexes"] ^ reference["indexes"])
    )
    print(f"  [G3] the revision downgrades cleanly back to {PREV_HEAD}")


def main():
    print("[A] the matching payoff, and its exact reversal")
    test_a_attach_widens_every_read_path_and_detach_restores()
    test_a_own_project_wins_ties()
    test_a_fulltext_precedence()
    print("[B] the link is a HINT, not an authority")
    test_b_write_door_refusals()
    test_b_picker_offers_established_memory_only()
    test_b_null_org_normalizes_off_gate()
    test_b_tm_org_leg_is_ungated_too()
    test_b_hostile_link_reaches_nothing()
    test_b_multi_org_hostile_link()
    test_b_stale_link_and_no_transitivity()
    test_b_concordance_is_own_project_only()
    test_b_global_null_row_still_excluded_under_the_gate()
    test_b_unique_index_exists_on_the_create_all_path_too()
    print("[C] glossary pins")
    test_c_glossary_pin_on_untyped_project()
    test_c_draft_pin_contributes_nothing()
    test_c_step_pin_still_wins_and_typed_autoattach_unchanged()
    test_c_glossary_pin_isolation()
    test_c_pin_candidates_respect_the_callers_own_visibility()
    test_c_org_leg_is_ungated_matching_auto_attach()
    test_c_glossary_org_normalization()
    print("[D] the web surface")
    test_d_web_roundtrip_and_viewer()
    test_d_oversized_ids_and_honest_banners()
    test_d_concordance_route_gives_a_member_nothing_of_the_source()
    print("[E] the CLI surface")
    test_e_cli_roundtrip()
    test_e_cli_output_is_ascii()
    print("[H] ownership: borrowed rows are read, never written")
    test_h_usage_counters_stay_with_the_owner()
    test_h_delete_cascades_both_link_directions()
    test_h_pinned_glossary_stays_deletable()
    test_h_other_fk_referrers_do_not_wedge_a_delete()
    print("[F] the template rider")
    test_f_documents_render_above_members()
    print("[G] the migration")
    test_g_revision_adds_and_only_adds()
    test_g_downgrade_restores_the_previous_head()
    print("PROJECT SOURCES OK")


if __name__ == "__main__":
    main()
