"""Glossary sense selection + placeholder-interior matching (QA campaign 2026-07-28).

Hermetic + offline (throwaway LOC_TMS_DATA_DIR, provider keys blanked -> MockProvider).
Pins the three glossary-side fixes from the pre-demo QA campaign
(corpus-data/qa-campaign/CAMPAIGN.md):

  BUG-01  sense selection always returned the FIRST sense. `_choose_sense` answered
          `senses[0]` for EVERY failure mode (spend cap, provider exception,
          unparseable reply, out-of-range index) AND the offline mock's zero-evidence
          tie-break also lands on index 0 -- so "could not decide" was
          indistinguishable from "chose the first sense". Production demanded the
          BANKING sense `cheque` on "Please check the configuration file before you
          continue." Now: option 0 of the candidate list is the entry's BASE target,
          every undecidable path falls back to the base (via a deterministic cue
          check), and a sense is applied only when it is actually chosen.
    1. zero-cue segment -> BASE target, sense_label None (never senses[0])
    2. cue-matching segments -> the CORRECT sense, INCLUDING the second-listed one
    3. spend-cap path -> base (and the single-cue sense when there is evidence)
    4. provider exception -> base / deterministic cue fallback (case-insensitive)
    5. prose+fenced JSON (what the reference local model and Claude actually emit)
       is now parsed instead of silently pinning sense 0
    6. out-of-range index -> base
    7. a SINGLE-sense entry no longer forces its sense on a non-matching context
       (chip task_17fa5f6b) but still applies when its cue matches

  BUG-02  (surgical part) a term matched INSIDE a placeholder -- `{name}` satisfied the
          glossary term `name`, so the adherence gate demanded `nombre` in a target
          that must copy `{name}` verbatim: an UNSATISFIABLE MAJOR flag on a correct
          translation. Placeholder interiors are now blanked before term matching, so
          the term never reaches the prompt OR the gate.
    8. {name} / ${VAR} / $VAR / printf spans excluded; a term OUTSIDE a placeholder
       still matches; end-to-end no glossary_violation

  IMPORT  sense rows clobbered the base entry's term metadata (last-row-wins on the
          shared meta dict).
    9. a base row's meta survives later sense rows; a sense-only term still gets meta

    10. fail-before/pass-after against the pre-fix file (git show HEAD, file copy --
        never git stash: the stash stack is shared across worktrees).

Final line: "GLOSSARY SENSE SELECTION OK".
"""

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

_TMP = init_env("loctms_glsense_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import GlossaryEntry, GlossarySense, Project
from app.services import glossary, quality
from app.services.providers import MockProvider

init_db()

SRC, TGT = "en-US", "es-ES"

# The production shape: the BANKING sense is listed FIRST, exactly as in glossary 1.
BANK_CUE = "bank money deposit account"
GENERAL_CUE = "email settings configuration verify"


def _project(name: str) -> int:
    with Session(engine) as s:
        p = Project(name=name, source_locale=SRC)
        s.add(p)
        s.commit()
        return p.id


def _entry(pid: int, source_term: str, base_target: str, senses=()) -> int:
    """A project-scoped glossary entry with `senses` = [(label, cue, target), ...]."""
    with Session(engine) as s:
        e = GlossaryEntry(
            project_id=pid,
            source_locale=SRC,
            target_locale=TGT,
            source_term=source_term,
            target_term=base_target,
        )
        s.add(e)
        s.commit()
        s.refresh(e)
        for label, cue, target in senses:
            s.add(
                GlossarySense(
                    glossary_entry_id=e.id, sense_label=label, context_cue=cue, target_term=target
                )
            )
        s.commit()
        return e.id


def _match(pid: int, text: str, provider=None) -> dict:
    with Session(engine) as s:
        return {
            t["source_term"]: t
            for t in glossary.match_terms(s, pid, text, SRC, TGT, provider=provider)
        }


class _Boom(MockProvider):
    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        raise RuntimeError("provider exploded")


class _Canned(MockProvider):
    """Returns a fixed raw string, whatever the prompt (the real failure shapes)."""

    raw = '{"index": 0}'

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        return self.raw


# ---------------------------------------------------------------------------
# 1-2. the core defect: evidence decides, and BOTH senses are reachable
# ---------------------------------------------------------------------------
def test_zero_cue_falls_back_to_base():
    pid = _project("sense-base")
    _entry(
        pid,
        "check",
        "comprobar",
        [("banking", BANK_CUE, "cheque"), ("general", GENERAL_CUE, "revisar")],
    )

    got = _match(pid, "Please check it before you continue.")["check"]
    assert got["target_term"] == "comprobar", got
    assert got["sense_label"] is None, got
    print("  1. zero-cue segment -> BASE target (not the first-listed sense) OK")


def test_cue_match_picks_the_right_sense_both_ways():
    pid = _project("sense-cues")
    _entry(
        pid,
        "check",
        "comprobar",
        [("banking", BANK_CUE, "cheque"), ("general", GENERAL_CUE, "revisar")],
    )

    bank = _match(pid, "Deposit the check at the bank counter.")["check"]
    assert (bank["target_term"], bank["sense_label"]) == ("cheque", "banking"), bank

    # The SECOND-listed sense -- never demonstrably selected in the whole production
    # campaign (6/6 checkable cases went to the first sense).
    gen = _match(pid, "Please check the configuration file before you continue.")["check"]
    assert (gen["target_term"], gen["sense_label"]) == ("revisar", "general"), gen
    print("  2. cue-bearing segments -> the correct sense, second sense included OK")


# ---------------------------------------------------------------------------
# 3-6. every "cannot run / cannot decide" path lands on the base, never senses[0]
# ---------------------------------------------------------------------------
def test_spend_cap_path():
    from app.services import spend

    pid = _project("sense-spend")
    _entry(
        pid,
        "check",
        "comprobar",
        [("banking", BANK_CUE, "cheque"), ("general", GENERAL_CUE, "revisar")],
    )

    real = spend.blocked_for
    spend.blocked_for = lambda prov, now=None: "Spend cap reached"
    try:
        blind = _match(pid, "Please check it before you continue.")["check"]
        assert (blind["target_term"], blind["sense_label"]) == ("comprobar", None), blind
        # ...and with unambiguous deterministic evidence the sense still wins, with
        # no provider call at all.
        cued = _match(pid, "Deposit the check at the bank counter.")["check"]
        assert (cued["target_term"], cued["sense_label"]) == ("cheque", "banking"), cued
    finally:
        spend.blocked_for = real
    print("  3. spend cap -> base when blind, deterministic cue sense when evident OK")


def test_provider_error_path():
    pid = _project("sense-boom")
    _entry(
        pid,
        "check",
        "comprobar",
        [("banking", BANK_CUE, "cheque"), ("general", GENERAL_CUE, "revisar")],
    )

    blind = _match(pid, "Please check it before you continue.", provider=_Boom())["check"]
    assert (blind["target_term"], blind["sense_label"]) == ("comprobar", None), blind

    # the deterministic fallback is CASE-INSENSITIVE and word-boundary sane
    loud = _match(pid, "Deposit the CHECK at the BANK counter.", provider=_Boom())["check"]
    assert (loud["target_term"], loud["sense_label"]) == ("cheque", "banking"), loud

    # a cue word that is only a SUBSTRING of a segment word is not evidence
    sub = _match(pid, "Please check the embankment.", provider=_Boom())["check"]
    assert sub["sense_label"] is None, sub
    print("  4. provider exception -> base; cue fallback is case-insensitive + whole-word OK")


def test_prose_wrapped_json_is_parsed():
    pid = _project("sense-prose")
    _entry(
        pid,
        "check",
        "comprobar",
        [("banking", BANK_CUE, "cheque"), ("general", GENERAL_CUE, "revisar")],
    )

    prov = _Canned()
    # What the project's reference local model (and, often, Claude) actually emits.
    # A bare json.loads raised on this and silently pinned index 0 = the first sense.
    prov.raw = 'Here is the JSON output:\n```json\n{"index": 2}\n```'
    got = _match(pid, "Please check it before you continue.", provider=prov)["check"]
    assert (got["target_term"], got["sense_label"]) == ("revisar", "general"), got

    prov.raw = '{"index": 1}'  # clean JSON still works
    got = _match(pid, "Please check it before you continue.", provider=prov)["check"]
    assert (got["target_term"], got["sense_label"]) == ("cheque", "banking"), got

    prov.raw = '{"index": 0}'  # option 0 IS the base -- an explicit choice
    got = _match(pid, "Please check it before you continue.", provider=prov)["check"]
    assert (got["target_term"], got["sense_label"]) == ("comprobar", None), got
    print("  5. prose/fenced JSON parsed; index 0 means the BASE target OK")


def test_out_of_range_index():
    pid = _project("sense-range")
    _entry(
        pid,
        "check",
        "comprobar",
        [("banking", BANK_CUE, "cheque"), ("general", GENERAL_CUE, "revisar")],
    )
    prov = _Canned()
    prov.raw = '{"index": 99}'
    got = _match(pid, "Please check it before you continue.", provider=prov)["check"]
    assert (got["target_term"], got["sense_label"]) == ("comprobar", None), got
    prov.raw = "not json at all"
    got = _match(pid, "Please check it before you continue.", provider=prov)["check"]
    assert (got["target_term"], got["sense_label"]) == ("comprobar", None), got
    print("  6. nonsense index / junk reply -> base OK")


# ---------------------------------------------------------------------------
# 7. a single-sense entry must not force its sense (chip task_17fa5f6b)
# ---------------------------------------------------------------------------
def test_single_sense_entry_does_not_force_itself():
    pid = _project("sense-single")
    _entry(
        pid,
        "login",
        "inicio de sesion",
        [("verb", "sign in access authenticate", "iniciar sesion")],
    )

    noun = _match(pid, "Open the login screen from the menu.")["login"]
    assert (noun["target_term"], noun["sense_label"]) == ("inicio de sesion", None), noun

    verb = _match(pid, "You must login to access your account.")["login"]
    assert (verb["target_term"], verb["sense_label"]) == ("iniciar sesion", "verb"), verb

    # An entry with senses but NO base target contributes no constraint rather than an
    # empty required term (which would render "login->" into the prompt).
    pid2 = _project("sense-nobase")
    _entry(pid2, "login", "", [("verb", "sign in access", "iniciar sesion")])
    assert "login" not in _match(pid2, "Open the login screen from the menu.")
    assert (
        _match(pid2, "You must login to access your account.")["login"]["target_term"]
        == "iniciar sesion"
    )
    print("  7. single-sense entry no longer forces its sense on a cue-less context OK")


# ---------------------------------------------------------------------------
# 7b. Q4: provider resolution is LAZY -- a single-sense match set NEVER resolves a
#     provider, so an all-local run on an install whose default provider has no key
#     (out-of-box DEFAULT_PROVIDER="claude") does not raise at match time.
# ---------------------------------------------------------------------------
def test_lazy_provider_resolution_single_sense():
    pid = _project("lazy-prov")
    _entry(pid, "name", "nombre")  # single-sense (no GlossarySense rows)
    _entry(pid, "count", "recuento")  # single-sense

    calls = {"n": 0}
    real = glossary.get_provider

    def _spy():
        # Stand in for a cloud default provider with no key: resolving it would raise.
        calls["n"] += 1
        raise RuntimeError("get_provider() must not be called for a single-sense match")

    set_pieces("app.services.glossary", "get_provider", _spy)
    try:
        # A glossary IS present and terms DO match -- but none is multi-sense, so
        # matching must complete WITHOUT resolving a provider (pre-fix: line 1181's
        # eager `provider = provider or get_provider()` raised here).
        terms = _match(pid, "Enter your name and the count value.")
        assert set(terms) == {"name", "count"}, terms
        assert terms["name"]["target_term"] == "nombre", terms
        assert calls["n"] == 0, "a single-sense match set must NOT resolve a provider"
    finally:
        set_pieces("app.services.glossary", "get_provider", real)
    print("  7b. Q4: single-sense match set resolves NO provider (lazy) OK")


def test_lazy_provider_resolution_multisense_still_resolves():
    """When a multi-sense term IS present, the provider resolves EXACTLY as before --
    once per segment, at the disambiguation point -- so behaviour is byte-identical for
    the path that actually needs an LLM."""
    from app.services.providers import MockProvider as _MP

    pid = _project("lazy-prov-ms")
    _entry(pid, "name", "nombre")  # single-sense
    _entry(
        pid,
        "check",
        "comprobar",
        [("banking", BANK_CUE, "cheque"), ("general", GENERAL_CUE, "revisar")],
    )

    calls = {"n": 0}
    real = glossary.get_provider

    def _spy():
        calls["n"] += 1
        return _MP()

    set_pieces("app.services.glossary", "get_provider", _spy)
    try:
        # zero-cue segment: check -> BASE, name -> nombre; but a multi-sense term is in
        # the match set, so the provider WAS resolved (lazily) to run disambiguation.
        terms = _match(pid, "Please check the name field before you continue.")
        assert terms["check"]["target_term"] == "comprobar", terms
        assert terms["name"]["target_term"] == "nombre", terms
        assert calls["n"] == 1, (
            "a multi-sense match set must resolve the provider exactly once",
            calls,
        )
    finally:
        set_pieces("app.services.glossary", "get_provider", real)
    print("  7c. Q4: multi-sense match set resolves the provider once (unchanged) OK")


# ---------------------------------------------------------------------------
# 8. placeholder interiors are not term matches (BUG-02, surgical part)
# ---------------------------------------------------------------------------
def test_placeholder_interior_excluded():
    assert (
        glossary.mask_protected_spans("Welcome back, {name}.")
        == "Welcome back, " + " " * len("{name}") + "."
    )
    for text, hidden in (
        ("Set ${name} now.", "name"),
        ("Set $name now.", "name"),
        ("Uploaded %s of %d files.", "s"),
    ):
        assert hidden not in glossary.mask_protected_spans(text).split(), text
    # length preserved -> no accidental token welding
    for text in ("Welcome back, {name}.", "Set ${name} now.", "a %1$s b", "$VAR x"):
        assert len(glossary.mask_protected_spans(text)) == len(text), text

    pid = _project("ph-terms")
    _entry(pid, "name", "nombre")
    _entry(pid, "count", "recuento")

    src = "Welcome back, {name}."
    terms = _match(pid, src)
    assert "name" not in terms, terms
    # END TO END: the adherence gate consumes exactly these dicts, so the
    # unsatisfiable MAJOR flag is gone at the seam.
    assert (
        quality.glossary_term_violations(src, "Bienvenido de nuevo, {name}.", list(terms.values()))
        == []
    )

    assert "count" not in _match(pid, "Set $count to zero.")
    assert "count" not in _match(pid, "The {count} value is required.")

    # a real occurrence OUTSIDE a placeholder still matches (and still gates)
    both = _match(pid, "Enter your name in the {name} field.")
    assert both["name"]["target_term"] == "nombre", both
    assert quality.glossary_term_violations(
        "Enter your name in the {name} field.",
        "Escriba su dato en el campo {name}.",
        list(both.values()),
    ) == [{"source_term": "name", "target_term": "nombre"}]
    print("  8. placeholder interiors excluded on both the prompt and gate sides OK")


# ---------------------------------------------------------------------------
# 8b. sense ORDER must be irrelevant (live evidence, runs 29/30)
# ---------------------------------------------------------------------------
def test_sense_order_is_irrelevant():
    """The reconciled production glossaries list the RISKY sense FIRST (banking before
    general, noun before verb) and sense rows cannot be reordered additively, so the
    fix must not depend on the data being re-sorted. Both orderings must behave the
    same: zero cue -> the base target; a general cue -> the general sense; and
    `cheque` must never be demanded of a cue-less or general segment."""
    risky_first = [("banking", BANK_CUE, "cheque"), ("general", GENERAL_CUE, "revisar")]
    safe_first = [("general", GENERAL_CUE, "revisar"), ("banking", BANK_CUE, "cheque")]
    cueless = "Please check it before you continue."
    general = "Check your email for the verification code."

    for label, order in (("banking-first", risky_first), ("general-first", safe_first)):
        pid = _project("order-" + label)
        _entry(pid, "check", "comprobar", order)

        blind = _match(pid, cueless)["check"]
        assert (blind["target_term"], blind["sense_label"]) == ("comprobar", None), (label, blind)
        gen = _match(pid, general)["check"]
        assert (gen["target_term"], gen["sense_label"]) == ("revisar", "general"), (label, gen)

        # the live false positive: a CORRECT general translation was flagged MAJOR
        # demanding `cheque`. The banking target is no longer demanded at all...
        # (surface forms, not lemmas: the gate has no lemmatizer — a DOCUMENTED,
        # accepted limitation and explicitly out of scope tonight.)
        for src, tgt in (
            (cueless, "Debe comprobar esto antes de continuar."),
            (general, "Debe revisar el correo para el codigo."),
        ):
            terms = list(_match(pid, src).values())
            assert all(t["target_term"] != "cheque" for t in terms), (label, terms)
            # ...and the demanded (base/general) term, when actually used, is clean.
            assert quality.glossary_term_violations(src, tgt, terms) == [], (label, tgt)
    print(
        "  8b. sense ORDER is irrelevant: cue-less -> base, general cue -> general, "
        "`cheque` never demanded OK"
    )


# ---------------------------------------------------------------------------
# 8c. a DNT span shadows a shorter term inside it (live evidence, runs 29/30)
# ---------------------------------------------------------------------------
def test_longer_dnt_span_shadows_inner_term():
    pid = _project("dnt-shadow")
    with Session(engine) as s:
        s.add(
            GlossaryEntry(
                project_id=pid,
                source_locale=SRC,
                target_locale=TGT,
                source_term="Acme Cloud",
                target_term="Acme Cloud",
                dnt=True,
            )
        )
        s.commit()
    _entry(pid, "cloud", "nube")

    src = "Welcome to Acme Cloud."
    terms = _match(pid, src)
    assert "Acme Cloud" in terms and terms["Acme Cloud"]["dnt"] is True, terms
    assert "cloud" not in terms, terms  # was flagged MAJOR in production
    assert (
        quality.glossary_term_violations(src, "Bienvenido a Acme Cloud.", list(terms.values()))
        == []
    )
    # the DNT gate is untouched by the shadowing
    assert quality.dnt_violations(src, "Bienvenido a Acme Nube.", list(terms.values())) == [
        "Acme Cloud"
    ]

    # an INDEPENDENT occurrence of the shorter term still counts
    both = _match(pid, "Move the cloud files to Acme Cloud.")
    assert both["cloud"]["target_term"] == "nube", both
    assert "Acme Cloud" in both, both
    print(
        "  8c. a satisfied DNT span shadows the shorter term inside it; an "
        "independent occurrence still matches OK"
    )


# ---------------------------------------------------------------------------
# 9. sense rows must not clobber the base entry's meta
# ---------------------------------------------------------------------------
_META_CSV = (
    "source_term,target_term,dnt,sense_label,context_cue,part_of_speech,definition\n"
    "bank,banco,,,,Noun,A financial institution\n"
    "bank,orilla,,river,river water,Noun-river,The side of a river\n"
    "bank,banco,,finance,money account,Noun-finance,A bank in the money sense\n"
    "tide,marea,,sea,ocean water,Noun-tide,Rise and fall of the sea\n"
)


def test_sense_rows_do_not_clobber_base_meta():
    pid = _project("meta-clobber")
    with Session(engine) as s:
        res = glossary.import_csv(s, pid, _META_CSV.encode("utf-8"), SRC, TGT)
        assert res == {"entries": 2, "senses": 3}, res
        by_term = {i["entry"].source_term: i["entry"] for i in glossary.list_entries(s, pid)}
    # the BASE row is authoritative; the two later sense rows do not overwrite it
    assert by_term["bank"].meta["part_of_speech"] == "Noun", by_term["bank"].meta
    assert by_term["bank"].meta["definition"] == "A financial institution"
    # a term whose ONLY row is a sense row still gets its meta (sense rows still fill
    # keys that are absent -- they just never overwrite)
    assert by_term["tide"].meta["part_of_speech"] == "Noun-tide", by_term["tide"].meta
    print("  9. base-row term meta survives later sense rows OK")


# ---------------------------------------------------------------------------
# 10. fail-before / pass-after against the pre-fix file (file copy, never stash)
# ---------------------------------------------------------------------------
def _head_module(rel_path: str, mod_name: str):
    """Import the HEAD (pre-fix) revision of a source file as a separate module, via
    a FILE COPY -- never `git stash` (the stash stack is shared across worktrees and
    concurrent agents mis-pop each other). None when git/HEAD is unavailable."""
    import importlib.util

    try:
        src = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"], cwd=_ROOT, capture_output=True, timeout=60
        )
    except Exception:
        return None
    if src.returncode != 0 or not src.stdout:
        return None
    path = os.path.join(_TMP, mod_name + ".py")
    with open(path, "wb") as fh:
        fh.write(src.stdout)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def test_fail_before_pass_after():
    old = _head_module("app/services/glossary.py", "_head_glossary")
    pid = _project("delta")
    _entry(
        pid,
        "check",
        "comprobar",
        [("banking", BANK_CUE, "cheque"), ("general", GENERAL_CUE, "revisar")],
    )
    _entry(pid, "name", "nombre")
    seg = "Please check it before you continue."
    ph = "Welcome back, {name}."

    if old is not None and not hasattr(old, "mask_protected_spans"):
        with Session(engine) as s:
            before = {t["source_term"]: t for t in old.match_terms(s, pid, seg, SRC, TGT)}
            before_ph = {t["source_term"]: t for t in old.match_terms(s, pid, ph, SRC, TGT)}
        # the two defects, reproduced on the pre-fix code
        assert before["check"]["target_term"] == "cheque", before
        assert "name" in before_ph, before_ph
        print("  10a. pre-fix code reproduced BOTH defects (first sense + placeholder interior) OK")
    else:
        print(
            "  10a. HEAD already carries the fix (or git unavailable) -- "
            "behavioural delta asserted below instead"
        )

    after = _match(pid, seg)
    assert after["check"]["target_term"] == "comprobar", after
    assert "name" not in _match(pid, ph)
    print("  10b. post-fix: base target chosen, placeholder interior not matched OK")


def main():
    test_zero_cue_falls_back_to_base()
    test_cue_match_picks_the_right_sense_both_ways()
    test_spend_cap_path()
    test_provider_error_path()
    test_prose_wrapped_json_is_parsed()
    test_out_of_range_index()
    test_single_sense_entry_does_not_force_itself()
    test_lazy_provider_resolution_single_sense()
    test_lazy_provider_resolution_multisense_still_resolves()
    test_placeholder_interior_excluded()
    test_sense_order_is_irrelevant()
    test_longer_dnt_span_shadows_inner_term()
    test_sense_rows_do_not_clobber_base_meta()
    test_fail_before_pass_after()
    print("GLOSSARY SENSE SELECTION OK")


if __name__ == "__main__":
    main()
