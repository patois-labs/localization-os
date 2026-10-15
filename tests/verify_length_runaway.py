"""K4 regression lock: the LENGTH-RUNAWAY gate at the sole MT write seam.

Live QA campaign, the planning notes finding K4. The 9-char
UI source "Start now" came back from the local model as ~245 characters of Russian
conversational preamble ("Privet! Ya gotov perevesti UI-stroku ...", markdown bold and
backticks included). It stored `status=mt` with ZERO qa_flags, was TM-eligible, and
exported verbatim into the ru build file where a build would render it as a button label.

IT IS NOT A KEY BUG. The investigation reproduced a byte-identical row for a DOCUMENT
segment with the same input, so the gap is at the SHARED commit seam. Section D pins BOTH
shapes so nobody can re-scope this to the key surfaces.

WHY EVERY EXISTING CHECK ABSTAINED (all four verified in section B against the LIVE
modules, and the pre-fix world is reproduced end to end in section A):
  * `refusal.is_refusal` -- `_TARGET_STEMS` listed this very hedge in es/fr/de/it/pt and
    NOTHING in Russian; `_TARGET_PREAMBLES` had hola/bonjour/hallo/ciao/ola but no
    "privet", so the courtesy opener pushed the hedge past the 80-char anchor with
    nothing to peel it. Controls: the identical output in Spanish IS caught.
  * `quality.meta_commentary` -- span-bounded to parentheticals / brackets / note-marker
    tails; this output is one unbroken block with no note marker, and the first-person /
    meta-vocabulary lists carry no Cyrillic.
  * `refusal.looks_untranslated` -- correctly abstains (the output really IS Russian),
    and it is opt-in via `lang_guard`, which the workflow path does not pass.
  * `number_violation` (no numbers in the source) and `tag_damage` (no tags).
So NO length / ratio / expansion check existed anywhere on the write path.

THE FIX HAS TWO PARTS, and this suite proves them SEPARATELY because they were committed
separately and either can be reverted alone:
  PART 1 (primary, structural, language-independent): `quality.length_runaway` + its wiring
    at `commit_mt_output`. TWO locks that must BOTH hold -- absolute surplus >= 120 chars
    AND ratio >= 5.0x, for EVERY source script (the first cut's 15.0x CJK floor was
    invented and is gone; C2 carries the evidence and the recorded zh->de residual).
    Section C proves the calibration
    on the REAL false-positive corpus; section D proves the seam behaviour with the
    pre-Part-2 refusal module bound in, i.e. Part 1 catching K4 ON ITS OWN.
  PART 2 (phrase parity): the Russian tier in refusal's four target-language lists.
    Section G proves it, INCLUDING its deliberate consequence -- `is_refusal` WITHHOLDS
    the write, so the exact K4 value now lands EMPTY at needs_review rather than
    flagged-with-text. That is exactly what already happens for es/de today; the maintainer's call.

FAIL-BEFORE is proven by FILE COPY, never `git stash` (the stash stack is shared across
worktrees): `git show <pre-fix>:app/services/{quality,refusal}.py`
are written to a temp dir, imported under private module names, WIRED TO EACH OTHER (so
the pre-fix seam sees the pre-fix refusal module and the pre-fix world is faithful), and
the K4 pair is driven through the pre-fix seam -- where it commits `status=mt`, zero
qa_flags, TM-ELIGIBLE. That is the shipped bug, reproduced.

Hermetic (throwaway LOC_TMS_DATA_DIR, MockProvider, no keys, no network). ASCII-only
FAILURE MESSAGES (the fixtures carry Cyrillic/CJK and a Windows console would raise
UnicodeEncodeError on the report instead of showing the real failure); final line
'verify_length_runaway OK'.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_k4_")

from sqlmodel import Session

from app import config
from app.db import engine, init_db
from app.models import (
    Document,
    Project,
    ProjectType,
    Segment,
    SegStatus,
    StepType,
    StringKey,
    Workflow,
    WorkflowStep,
)
from app.services import orchestrator, quality, refusal

# ---------------------------------------------------------------------------
# THE K4 PAIR.
#
# The byte-exact production value is not in this repo or the dev DB (it lived in the
# live hosted install's key rows), so this is a FAITHFUL RECONSTRUCTION of the shape the
# audit records: the same source, the same opening hedge ("Privet! Ya gotov perevesti
# UI-stroku"), the same markdown bold + backticks, and the same length band (245 chars
# here; the audit measured 248). Nothing in the gate is sensitive to the difference --
# both clear the surplus lock by ~2x and the ratio lock by ~5.4x.
# ---------------------------------------------------------------------------
K4_SRC = "Start now"
K4_TGT = (
    "Привет! Я готов "
    "перевести UI-"
    "строку для вас. "
    "Вот перевод "
    "строки `Start now` на "
    "русский язык: "
    "**Начать "
    "сейчас**. "
    "Обратите "
    "внимание, что "
    "для кнопки "
    "интерфейса "
    "лучше "
    "использовать "
    "краткую форму. "
    "Дайте знать, "
    "если нужен "
    "другой вариант "
    "этого перевода."
)

# The same runaway with the Russian REFUSAL HEDGE REMOVED -- structurally identical
# (9 -> 214 chars) but carrying nothing any phrase list in any language covers. This is
# what proves the STRUCTURAL gate is doing the work rather than the Part-2 phrases, and
# it is the shape the next model in the next locale will produce.
K4_TGT_NO_HEDGE = K4_TGT.split("! ", 1)[1].split(". ", 1)[1]

# run-190 Item D overlap: the classic segmented llm_agent path now HARD-rejects a page-dump
# candidate (config.OVER_TRANSLATION_MAX_RATIO, floor max(160, len(src)*8+60)) BEFORE the
# write seam, WITHHOLDING it (NEEDS_REVIEW, target withheld) rather than soft-committing it.
# For the 9-char K4 source that floor is 160 chars, so the 214-char K4_TGT_NO_HEDGE would be
# withheld by Item D and never reach length_runaway's soft commit. Section F below is the
# BUG-06 regression lock for length_runaway's SOFT gate + the status=MT re-assertion, a band
# Item D deliberately leaves to length_runaway (short source, 129-160 chars: surplus>=120 and
# ratio>=5 trip length_runaway, but len<=160 is under Item D's floor). So section F uses a
# runaway sized INTO that band; it exercises the identical soft-gate code path (length verdict
# -> status=MT reset -> family-wide re-assertion) without colliding with Item D's hard reject.
K4_WF_RUNAWAY = K4_TGT_NO_HEDGE[:150]

# L2 (gate round 1): PIN the relationship between this soft-gate fixture and Item D's HARD
# floor so a future change to OVER_TRANSLATION_MAX_RATIO cannot SILENTLY convert this
# length_runaway lock into an Item-D reject (which would make the suite pass for the wrong
# reason). Item D's floor for the K4 source is max(160, len(src)*ratio + 60); K4_WF_RUNAWAY
# must stay STRICTLY under it (so length_runaway's soft gate, not Item D, is exercised) AND
# in length_runaway's own trip band (surplus >= 120, ratio >= 5).
_K4_D_FLOOR = max(160, int(len(K4_SRC) * config.OVER_TRANSLATION_MAX_RATIO) + 60)
assert len(K4_WF_RUNAWAY) < _K4_D_FLOOR, (
    f"K4_WF_RUNAWAY ({len(K4_WF_RUNAWAY)} chars) is no longer under Item D's over-translation "
    f"floor ({_K4_D_FLOOR}); the length_runaway soft-gate lock would become an Item-D HARD "
    "reject. Resize the fixture or re-derive the band."
)
assert len(K4_WF_RUNAWAY) - len(K4_SRC) >= 120 and len(K4_WF_RUNAWAY) >= len(K4_SRC) * 5, (
    "K4_WF_RUNAWAY no longer trips length_runaway's soft gate (surplus>=120 and ratio>=5)"
)

# A second runaway, different language and different shape: a chatty English preamble
# wrapped around a short German UI label (the model narrating instead of translating).
EN_RUNAWAY_SRC = "Save changes"
EN_RUNAWAY_TGT = (
    "Sure thing! Here is a natural German rendering of that button label for your "
    "product interface, taking the surrounding UI context into account and keeping it "
    "short enough for a narrow button: **Aenderungen speichern**. Let me know if you "
    "would prefer a more formal register instead."
)


def _fail(msg):
    raise AssertionError(msg)


def _ok(label):
    print("  ok - " + label)


# ---------------------------------------------------------------------------
# A. FAIL-BEFORE, by file copy of the PRE-FIX modules (never git stash).
#
# `fc0e100` is the audit commit -- the tip this branch was cut from, i.e. the exact tree
# that shipped the K4 defect. Pinning (rather than HEAD:) keeps the proof from inverting
# once the fix becomes HEAD -- the technique verify_number_meta_integrity._PRE_FIX_COMMIT
# and verify_export_tag_policy._BASE_COMMIT already use.
# ---------------------------------------------------------------------------
_PRE_FIX_COMMIT = "fc0e100"


def _load_pre_fix(rel_path, mod_name):
    """Import `git show <_PRE_FIX_COMMIT>:<rel_path>` under `mod_name` from a throwaway
    file. Returns None when git history is unavailable (shallow/detached checkout)."""
    blob = subprocess.run(
        ["git", "show", _PRE_FIX_COMMIT + ":" + rel_path], cwd=_ROOT, capture_output=True
    )
    if blob.returncode != 0 or not blob.stdout:
        return None
    tmpdir = tempfile.mkdtemp(prefix="loctms_k4_pre_")
    path = os.path.join(tmpdir, mod_name + ".py")
    with open(path, "wb") as fh:
        fh.write(blob.stdout)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


PRE_QUALITY = None
PRE_REFUSAL = None


def check_fail_before(session, doc_id):
    """Drive the K4 pair through the PRE-FIX seam and watch it commit clean."""
    global PRE_QUALITY, PRE_REFUSAL
    print("A. fail-before (pre-fix modules, loaded by file copy, wired to each other)")
    PRE_QUALITY = _load_pre_fix("app/services/quality.py", "_k4_pre_quality")
    PRE_REFUSAL = _load_pre_fix("app/services/refusal.py", "_k4_pre_refusal")
    if PRE_QUALITY is None or PRE_REFUSAL is None:
        print("  skip - no git history available (shallow/detached checkout)")
        return
    # run-190 round-3: the LIVE quality.commit_mt_output (which this suite binds to the pre-fix
    # refusal stub below to isolate the LENGTH clear against a pre-fix is_refusal) now reads
    # refusal.OVER_TRANSLATION_REASON_PREFIX on its clean-commit clear. The pre-fix refusal
    # predates that constant, so backfill the REAL value onto the stub -- this keeps the length
    # isolation honest (only is_refusal differs) rather than AttributeError-ing on a new name.
    # LOW-4 (round 4): resolve the constant via getattr with the literal default so this backfill
    # never AttributeErrors on a tree whose LIVE refusal predates the constant (the counterfactual
    # copy-onto-pre-fix case) -- it reaches the behaviour instead of dying on a missing name.
    if not hasattr(PRE_REFUSAL, "OVER_TRANSLATION_REASON_PREFIX"):
        PRE_REFUSAL.OVER_TRANSLATION_REASON_PREFIX = getattr(
            refusal, "OVER_TRANSLATION_REASON_PREFIX", "over-translation:"
        )
    # #683 added ENGINE_LIMIT_REASON_PREFIX, which the live clean-commit clear
    # (quality_hub.clear_over_translation_flag) now also reads: same backfill.
    if not hasattr(PRE_REFUSAL, "ENGINE_LIMIT_REASON_PREFIX") and hasattr(
        refusal, "ENGINE_LIMIT_REASON_PREFIX"
    ):
        PRE_REFUSAL.ENGINE_LIMIT_REASON_PREFIX = refusal.ENGINE_LIMIT_REASON_PREFIX
    # The pre-fix quality module's `from app.services import refusal` resolved to the
    # LIVE module, which now carries the Part-2 Russian tier and would withhold the K4
    # write -- masking the pre-fix behaviour. Bind the PRE-FIX refusal into it so the
    # reproduction is the real pre-fix world, not a hybrid.
    PRE_QUALITY.refusal = PRE_REFUSAL

    for absent in (
        "length_runaway",
        "LENGTH_MIN_SURPLUS",
        "LENGTH_MIN_RATIO",
        "length_runaway_flag",
        "clear_length_runaway_flag",
    ):
        if hasattr(PRE_QUALITY, absent):
            _fail(f"pre-fix quality.py already exposes {absent!r} -- the pin is not pre-fix")
    _ok("pre-fix quality.py has no length gate at all (5 names absent)")

    # THE SHIPPED BUG, reproduced at the pre-fix seam: status=mt, no flags, TM-eligible.
    seg = _seg(session, doc_id, K4_SRC, 900, locale="ru-RU")
    out = PRE_QUALITY.commit_mt_output(session, seg, K4_TGT, source="workflow")
    session.commit()
    session.refresh(seg)
    if not out.committed or out.flagged:
        _fail(f"pre-fix: expected a plain clean commit, got {out!r}")
    if seg.status != SegStatus.MT:
        _fail(f"pre-fix: the K4 runaway must land status=mt (the defect), got {seg.status!r}")
    if seg.qa_flags:
        _fail(
            f"pre-fix: the K4 runaway must land with ZERO qa_flags (the defect), got {seg.qa_flags!r}"  # noqa: E501
        )
    if not out.tm_eligible:
        _fail("pre-fix: the K4 runaway was TM-eligible (the defect); got tm_eligible=False")
    _ok("pre-fix seam commits the 9->245 runaway as status=mt, qa_flags=None, TM-ELIGIBLE")

    # ...and it was equally silent for a DOCUMENT segment -- the general-gap proof, at the
    # pre-fix tip. (Same code path; asserted here so the "it was a key bug" reading is
    # closed off with evidence rather than an argument.)
    seg_d = _seg(session, doc_id, K4_SRC, 901, locale="ru-RU")
    PRE_QUALITY.commit_mt_output(session, seg_d, K4_TGT, source="mt")
    session.commit()
    session.refresh(seg_d)
    if seg_d.status != SegStatus.MT or seg_d.qa_flags:
        _fail(
            f"pre-fix: the DOCUMENT shape must be equally silent, got {seg_d.status!r} / {seg_d.qa_flags!r}"  # noqa: E501
        )
    _ok("pre-fix: a DOCUMENT segment with the same input is byte-identical (not key-specific)")


# ---------------------------------------------------------------------------
# B. ROOT CAUSE, against the LIVE modules for the checks Part 1 does not touch.
# ---------------------------------------------------------------------------
def check_root_cause():
    print("B. root cause of the miss (live modules)")
    if quality.meta_commentary(K4_SRC, K4_TGT):
        _fail("meta_commentary unexpectedly fires -- the root-cause claim is wrong")
    _ok("meta_commentary abstains (no paren / bracket / note-marker span, no Cyrillic vocab)")
    if refusal.looks_untranslated(K4_TGT, "ru-RU"):
        _fail("looks_untranslated unexpectedly fires on real Russian")
    _ok("looks_untranslated abstains (the output really IS Russian) and is opt-in anyway")
    if quality.number_violation(K4_SRC, K4_TGT, "ru-RU"):
        _fail("number_violation unexpectedly fires (there are no numbers in the source)")
    if quality.tag_damage(K4_SRC, K4_TGT):
        _fail("tag_damage unexpectedly fires (there are no inline tags)")
    _ok("number_violation + tag_damage abstain (no numbers, no tags)")
    # The PRE-FIX refusal module is the honest witness for the phrase-list gap: the live
    # one now carries the Part-2 Russian tier.
    if PRE_REFUSAL is not None:
        if PRE_REFUSAL.is_refusal(K4_TGT):
            _fail("pre-fix is_refusal unexpectedly catches the Russian hedge")
        _ok("pre-fix is_refusal missed it (no Russian tier; 'privet' not a known preamble)")
        # ...and the CONTROL that makes the gap a per-language asymmetry rather than a
        # design choice: the same hedge in Spanish WAS caught, pre-fix.
        es_control = (
            "¡Hola! Estoy listo para traducir la cadena de interfaz que me has enviado al espanol."
        )
        if not PRE_REFUSAL.is_refusal(es_control):
            _fail("control failed: the es form of the same hedge should have been caught")
        _ok("control: the IDENTICAL hedge in Spanish was already caught pre-fix (asymmetry)")


# ---------------------------------------------------------------------------
# C. The detector itself: recall on K4, and the two-lock calibration.
# ---------------------------------------------------------------------------
def check_detector():
    print("C. quality.length_runaway (the two locks)")
    if not quality.length_runaway(K4_SRC, K4_TGT):
        _fail("the K4 pair must be detected by the structural gate")
    if not quality.length_runaway(K4_SRC, K4_TGT_NO_HEDGE):
        _fail("the hedge-free K4 shape must be detected too (structure, not phrases)")
    if not quality.length_runaway(EN_RUNAWAY_SRC, EN_RUNAWAY_TGT):
        _fail("the second-language runaway must be detected")
    _ok("K4, the hedge-free K4 shape, and an en/de runaway are all detected")

    # BOTH LOCKS ARE REQUIRED -- neither alone, and neither is redundant.
    # (i) huge RATIO, small surplus: a real acronym glossary expansion.
    if quality.length_runaway("DoS", "ataque por denegacion de servicio"):
        _fail("11x with a 30-char surplus must NOT fire (the surplus lock)")
    # (ii) huge SURPLUS, ordinary ratio: real prose expansion.
    if quality.length_runaway("x" * 400, "y" * 700):
        _fail("+300 chars at 1.75x must NOT fire (the ratio lock)")
    _ok("neither lock alone fires: 11x/+30 and 1.75x/+300 both pass")

    # The exact boundaries, so a later edit to either constant is a visible test change.
    if quality.LENGTH_MIN_SURPLUS != 120 or quality.LENGTH_MIN_RATIO != 5.0:
        _fail(
            f"the calibrated thresholds changed: {quality.LENGTH_MIN_SURPLUS!r} / {quality.LENGTH_MIN_RATIO!r}"  # noqa: E501
        )
    src = "x" * 30
    if quality.length_runaway(src, "y" * 149):
        _fail("+119 (one short of the surplus lock) must not fire")
    if not quality.length_runaway(src, "y" * 150):
        _fail("+120 at 5.0x is exactly both locks and MUST fire")
    if quality.length_runaway("x" * 40, "y" * 199):
        _fail("4.98x (one short of the ratio lock) must not fire even at +159")
    _ok("boundaries exact: +119 passes, +120 at 5.0x fires, 4.98x at +159 passes")

    # Empty / whitespace on either side -> "" (never a crash, never a verdict).
    for s, t in (
        (None, K4_TGT),
        ("", K4_TGT),
        ("   \n ", K4_TGT),
        (K4_SRC, None),
        (K4_SRC, ""),
        (K4_SRC, "   \n "),
        (None, None),
    ):
        if quality.length_runaway(s, t):
            _fail(
                "empty/whitespace input must never produce a verdict: {!r} / {!r}".format(
                    s, (t or "")[:20]
                )
            )
    _ok("empty / whitespace / None on either side -> no verdict")


def check_no_cjk_carveout():
    """FIX-ROUND F1: there is NO CJK carve-out, the evidence for removing it, AND
    (FINAL-ROUND F8) the false-positive class that removal really did introduce.

    The first cut of this gate gave a CJK-dominant source a 15.0x ratio floor, justified
    in a comment by "a real ja->en pair in the corpus measured +119 chars at exactly
    5.00x". No such pair exists: the dev-DB corpus is en-US -> de/es/fr/ja/zh/es-419, so
    0 of its 8,695 SOURCES are CJK-dominant. The only CJK-side evidence available is the
    244 CJK-dominant TARGETS read backwards (200 segmenttm + 44 glossaryentry), and it is
    TWO SEPARATE ROWS, not one (the earlier "worst pair +39 at 6.25x" welded a surplus
    from one row onto a ratio from another):
        worst SURPLUS  +39 at 4.00x   -- a 13-char zh row -> a 52-char en target
        worst RATIO   6.25x at +21    -- a 4-char zh row  -> a 25-char en target
    ZERO of the 244 clear the +120 surplus lock; ZERO clear both.

    WHAT THAT EARNS: every one of the 244 is a CJK source against an ENGLISH target, so
    the supported claim is "the plain locks spare every faithful CJK->en pair" -- NOT
    "every faithful CJK->Latin pair". It does not reach CJK->de/ru, and no reversal of a
    target can reach it (backwards, these rows are short en->zh sources whose Latin side
    is en). Authoring the missing case finds a real fire: a faithful zh->de legal clause
    at +125 / 5.46x, RECORDED below as a KNOWN LIMITATION rather than patched (0
    CJK-dominant sources exist in this install, and the gate is SOFT -- text kept,
    needs_review, TM-ineligible). Deriving a source-script-aware floor on REAL CJK-source
    pairs is a PREREQUISITE before any install enables a CJK source locale, and it may not
    be done by restoring the invented `LENGTH_MIN_RATIO_CJK` / `LENGTH_CJK_SHARE`.

    This section pins that reading with fixtures on BOTH sides."""
    print("C2. no CJK carve-out (F1) -- the Latin locks judge every script")
    for gone in ("LENGTH_MIN_RATIO_CJK", "LENGTH_CJK_SHARE", "_CJK_RANGES", "_cjk_share"):
        if hasattr(quality, gone):
            _fail(
                f"the CJK carve-out is back as {gone!r} -- it may only return WITH a "
                "calibration measured on real CJK-SOURCE pairs"
            )
    _ok("the four uncalibrated CJK names are gone (floor, share, ranges, classifier)")

    # MUST NOT FLAG (1): a FAITHFUL ja -> en pair. Not an embellished one -- the fixture
    # the carve-out used to justify itself padded the English with clauses the Japanese
    # never contained. The surplus lock is what spares this, and it does so comfortably.
    # NOTE THE SCOPE: ja -> EN. That is the direction the 244 reversed rows evidence, and
    # the only direction this fixture licenses a claim about (see the F8 record below).
    ja_src = "この設定を変更するとすべてのチームメンバーに通知が送信されます。"
    en_tgt = "Changing this setting notifies every member of your team."
    if quality.length_runaway(ja_src, en_tgt):
        _fail(
            "FALSE POSITIVE on a faithful ja->en pair (+%d, %.2fx)"  # noqa: UP031
            % (len(en_tgt) - len(ja_src), len(en_tgt) / len(ja_src))
        )
    _ok(
        "a faithful ja->en pair stays silent (+%d at %.2fx -- the SURPLUS lock spares it)"  # noqa: UP031
        % (len(en_tgt) - len(ja_src), len(en_tgt) / len(ja_src))
    )

    # MUST NOT FLAG (2 and 3): the two EXTREME rows of the reversed-CJK evidence, each at
    # its own measured shape. They are different rows and are pinned separately, because
    # stating them as one "+39 at 6.25x" pair describes a row that does not exist.
    #   worst RATIO  : 4 chars -> 25 chars, 6.25x, +21   (双重认证 -> Two-factor ...)
    #   worst SURPLUS: 13 chars -> 52 chars, 4.00x, +39  (a zh security-settings string)
    for label, s_len, t_len in (
        ("worst RATIO (6.25x at +21)", 4, 25),
        ("worst SURPLUS (+39 at 4.00x)", 13, 52),
    ):
        if quality.length_runaway("設定変更する内容を確認する"[:s_len], "z" * t_len):
            _fail(f"FALSE POSITIVE on the {label} reversed-CJK row in the corpus")
    _ok("both extreme reversed-CJK rows stay silent (6.25x at +21; +39 at 4.00x)")

    # KNOWN LIMITATION, RECORDED (final-round F8) -- this pair FIRES, and it should not.
    # It is asserted in its CURRENT (wrong) state deliberately: the suite's convention is
    # plain asserts with no skip/xfail machinery, so a characterisation assert is the only
    # way to keep the reproducing pair VERBATIM and CHECKED in the tree. Do not read it as
    # an endorsement -- if a source-script-aware floor is ever derived on real CJK-source
    # pairs, this assert goes RED and whoever does that work must come here and delete it.
    zh_legal_src = "您有权随时要求访问、更正或删除我们所持有的您的个人信息。"
    de_legal_tgt = (
        "Sie haben jederzeit das Recht, Zugang zu Ihren bei uns gespeicherten "
        "personenbezogenen Daten zu verlangen sowie diese berichtigen oder "
        "löschen zu lassen."
    )
    _surplus = len(de_legal_tgt) - len(zh_legal_src)
    _ratio = len(de_legal_tgt) / len(zh_legal_src)
    if (_surplus, round(_ratio, 2)) != (125, 5.46):
        _fail(
            "fixture drift: the recorded zh->de limitation pair must measure +125 at "  # noqa: UP031
            "5.46x, got +%d at %.2fx" % (_surplus, _ratio)
        )
    if not quality.length_runaway(zh_legal_src, de_legal_tgt):
        _fail(
            "KNOWN LIMITATION F8 no longer reproduces -- a faithful zh->de legal clause "
            "used to fire at +125/5.46x. If that was FIXED, delete this assert and the "
            "F8 record in quality.py and the audit; if it was masked, find out how"
        )
    _ok(
        "KNOWN LIMITATION recorded (NOT desired): a faithful zh->de legal clause fires at "  # noqa: UP031
        "+%d / %.2fx -- CJK->de/ru has no margin under the 5.0 lock, unlike CJK->en. "
        "Blast radius 0 today (0 CJK sources) and the gate is SOFT" % (_surplus, _ratio)
    )

    # MUST FLAG: the RECALL the carve-out was costing. A CJK source that runs away at
    # 6.25x / +168 fired under NO threshold while the 15.0x floor stood; it fires now.
    cjk_runaway_src = "この設定を変更するとすべてのチームメンバーに通知が送信されます。"
    cjk_runaway_tgt = "z" * 200
    ratio = len(cjk_runaway_tgt) / len(cjk_runaway_src)
    surplus = len(cjk_runaway_tgt) - len(cjk_runaway_src)
    # Pinned EXACTLY, so the numbers in the comment above cannot drift from the fixture
    # again (the earlier text said "6.7x / +170" for what is a 6.25x / +168 pair).
    if (surplus, round(ratio, 2)) != (168, 6.25):
        _fail(
            "fixture drift: the recall demo must measure +168 at 6.25x, got +%d at %.2fx"  # noqa: UP031
            % (surplus, ratio)
        )
    if not (5.0 <= ratio < 15.0) or surplus < 120:
        _fail(
            "fixture drift: this pair must clear both Latin locks and sit BELOW the "  # noqa: UP031
            "retired 15.0x floor, got +%d at %.2fx" % (surplus, ratio)
        )
    if not quality.length_runaway(cjk_runaway_src, cjk_runaway_tgt):
        _fail("a CJK source in the 5.0x-15.0x band must fire (the old floor let it pass)")
    _ok(f"a {ratio:.2f}x CJK-source runaway now fires -- exactly the recall the 15.0x floor cost")
    if not quality.length_runaway("今すぐ開始", "z" * 200):
        _fail("a 5-char CJK source -> 200 chars (40x) must still fire")
    _ok("the extreme CJK runaway (40x) still fires, as it always did")

    # The old classifier's RANGE GAPS (halfwidth katakana U+FF61-FF9F, fullwidth forms,
    # Ext G) are moot rather than fixed: no script classification happens at all now, so
    # these sources are judged by the same two locks as everything else.
    for label, src in (("halfwidth katakana", "ｾｯﾃｲﾍﾝｺｳ"), ("fullwidth forms", "設定変更")):
        if not quality.length_runaway(src, "z" * 200):
            _fail(f"a runaway from a {label} source must fire")
    _ok("halfwidth-katakana and fullwidth sources are judged identically (F7 moot)")


def check_cost_is_constant():
    """FIX-ROUND F5 + FINAL-ROUND F9/F10: the gate must not WALK the source, and the
    assertions that say so must have TEETH.

    `fulltext_paragraphs.save_paragraph -> _commit_body` reaches this seam on every
    paragraph save with the WHOLE document body as source_text. The first cut computed an
    O(n) CJK share over that body BEFORE the O(1) ratio test, so a 200 KB FULL_TEXT
    document paid ~0.3s per save to reach a ratio of ~1.1 and return "". Measured on the
    pre-fix module at 1,000,000 chars: 3.1s uninstrumented, 6.5s under tracemalloc, and an
    8.4 MB allocation peak.

    F9 -- WHY THE PRIMARY ASSERTION IS NOW SCALING, NOT AN ABSOLUTE. The 8.4 MB figure is
    an artefact of the OLD `_cjk_share` MATERIALISING a list comprehension. Re-add the same
    per-character scan as a GENERATOR -- identical O(n) time -- and it peaks at 424 bytes
    and takes 0.048s at 1M: an absolute 100 KB / 0.15s bound passes it clean. So the shape
    of the cost is asserted directly: the gate is O(1) in time, so t(1,000,000) must not
    exceed a small multiple of t(10,000). Measured honestly here at ~0.4us MEDIAN at BOTH
    sizes; a generator scan is ~100x apart. The allocation bound stays as a SECONDARY --
    it still kills the materialising shape outright.

    F10 -- AND THE ALLOCATION IS MEASURED IN BOTH CASES, not just the flattering one.
    `str.strip()` returns the SAME object only when there is nothing to strip, so "56 bytes
    at 1M" is the BEST case. A document body reaching `_commit_body` almost always carries
    a trailing newline, and then the gate makes exactly ONE ~n-byte copy (measured
    1,000,097 B). That is still O(1) in TIME and one copy instead of a per-character scan,
    which is the actual claim; both fixtures are asserted so the claim cannot quietly
    become the best case again."""
    print("C2b. cost: O(1) in time (F5), asserted by SCALING (F9), both strip cases (F10)")
    import time
    import tracemalloc

    def _median_call(n, reps):
        """Median wall time of one `length_runaway` on an n-char source, cheap path."""
        src = "x" * n  # no surrounding whitespace: strip() is a no-op
        tgt = "y" * (n + 200)  # +200 surplus clears lock 1; ratio ~1.0 passes
        samples = []
        for _ in range(reps):
            t0 = time.perf_counter()
            verdict = quality.length_runaway(src, tgt)
            samples.append(time.perf_counter() - t0)
            if verdict:
                _fail("setup: a ~1.0x pair must not fire -- this fixture measures the CHEAP path")
        samples.sort()
        return samples[len(samples) // 2]

    # PRIMARY: SCALING. A 100x larger source may not cost meaningfully more time. The
    # 0.005s floor keeps a genuinely O(1) gate from tripping on clock resolution (t(10k)
    # is sub-microsecond, so 20 * t(10k) alone is inside the timer's noise); it is still
    # ~10x below a generator-shaped per-character scan of a 1,000,000-char source.
    t_small = _median_call(10000, 2001)
    t_big = _median_call(1000000, 201)
    bound = max(20 * t_small, 0.005)
    if t_big > bound:
        _fail(
            f"the gate is NOT O(1) in time: t(1,000,000)={t_big:.6f}s vs t(10,000)={t_small:.6f}s "
            f"(bound {bound:.6f}s) -- something is walking the source again"
        )
    _ok(
        f"time is O(1): median {t_small:.7f}s at 10,000 chars vs {t_big:.7f}s at 1,000,000 (bound {bound:.6f}s) "  # noqa: E501
        "-- a per-character scan is ~100x apart even as a generator"
    )

    # SECONDARY: ALLOCATION, in BOTH strip cases. Case 1 is the best case (no stripping
    # needed -> strip() returns the same object). Case 2 is the NORMAL one for a document
    # body -- one trailing newline -> exactly ONE ~n-byte copy of the source, and no more.
    def _peak(src, tgt):
        tracemalloc.start()
        verdict = quality.length_runaway(src, tgt)
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if verdict:
            _fail("setup: a ~1.0x pair must not fire -- this fixture measures the CHEAP path")
        return peak

    peak_clean = _peak("x" * 1000000, "y" * 1000200)
    # A per-character scan that MATERIALISES (the old `_cjk_share`: 8,449,624 bytes here)
    # cannot hide under 100 KB. A generator-shaped scan can -- that is what the scaling
    # assertion above is for.
    if peak_clean > 100000:
        _fail(
            "the gate allocated %d bytes for a 1,000,000-char source that needs no "  # noqa: UP031
            "stripping -- something is materialising a walk over it" % peak_clean
        )
    peak_stripped = _peak("x" * 1000000 + "\n", "y" * 1000200)
    # Exactly ONE copy: at least ~half the source (so the copy is real and the "56 bytes"
    # claim can never be restated as universal) and at most 1.5x it (so a SECOND copy --
    # a newline normalise bolted on after the strip, say -- is caught rather than waved
    # through as "still O(1)").
    if not (500000 <= peak_stripped <= 1500000):
        _fail(
            "a 1,000,000-char source with ONE trailing newline should cost exactly one "  # noqa: UP031
            "~n-byte strip() copy, got %d bytes" % peak_stripped
        )
    _ok(
        "allocation at 1,000,000 chars: %d bytes with nothing to strip, %d bytes (one "  # noqa: UP031
        "strip copy) with a trailing newline -- the normal case for a document body"
        % (peak_clean, peak_stripped)
    )

    # ...and the ORDERING that makes it cheap: the surplus lock is tested first and
    # returns before anything else runs. A source that fails it is judged on two len()s.
    if quality.length_runaway("x" * 1000000, "y" * 1000050):
        _fail("a +50 surplus must return on the FIRST lock, whatever the source size")
    _ok("the O(1) surplus lock is the first test, so the common path is two len() calls")


# ---------------------------------------------------------------------------
# D. FALSE-POSITIVE LOCKS -- every one of these is REAL delivered content taken from
#    this install's own dev DB (or its exact shape), and every one must stay silent.
#
#    Calibration evidence, re-derived in the fix round on a READ-ONLY COPY of
#    data/loc_tms.db and stated with its SCOPE (finding F2 -- the first framing called all
#    8,695 rows "the corpus", and 93.5% of them can never reach this seam):
#      IN SCOPE -- 562 pairs (500 segmenttm + 62 non-empty segment). `commit_mt_output`'s
#                  only callers are translate, orchestrator x2, keys.set_value and
#                  fulltext_paragraphs._commit_body; a glossaryentry row NEVER passes
#                  through it.
#        ratio-only >= 5.0   ->  0 fires
#        surplus-only >= 120 ->  4 fires (worst: a 422-char source expanding +273 at an
#                                ordinary 1.65x -- the real pair pinned below)
#        BOTH LOCKS          ->  0 fires
#      OUT OF SCOPE, carried deliberately as a CONSERVATIVE PROXY -- 8,133 glossaryentry
#                  rows, the hardest false-positive class in the install (acronym entries
#                  expand to 17.25x).
#        ratio-only >= 5.0   -> 95 fires (DTaP 17.25x, Hib 16.0, iSNS 12.25, DoS 11.0)
#        surplus-only >= 120 ->  0 fires
#        BOTH LOCKS          ->  0 fires
#      Targets across both: de/es/fr/ja/zh/es-419 -- EXACT, and re-verified in the final
#      round against a challenge that the list omitted ru-RU. The install does hold one
#      ru-RU `segment` row, but its target is EMPTY, so it is not one of the 62 in-scope
#      PAIRS (es-419 x58 + es-ES x4) and contributes to no count above.
#      0 sources are CJK-dominant (see C2), all 8,695 sources are en-US.
# ---------------------------------------------------------------------------
REAL_ACRONYM_EXPANSIONS = [
    (
        "DTaP",
        "vaccin antidiphtérique, antitétanique et anticoquelucheux acellulaire",
        "fr glossary, 17.25x",
    ),
    ("Hib", "vaccin contre l'haemophilus influenzae de type B", "fr glossary, 16.0x"),
    ("iSNS", "Servicio de nombres de almacenamiento de Internet", "es glossary, 12.25x"),
    ("DoS", "ataque por denegación de servicio", "es glossary, 11.0x"),
]

# The real +273-char surplus pair from the corpus (es-419 prose, 1.65x). Trimmed to its
# opening so the fixture stays readable; the lengths are what matter and are re-derived
# below rather than hard-coded.
REAL_PROSE_SRC = (
    "New sales engineer working with Account managers for existing "
    "customers focusing on Account Expansions etc. Pay for role: Base: "
    "$115,000. OTE: $140,000 ish. Notes go to hiring manager: next steps "
    "if I go through will be a video interview, 30 minutes with the "
    "Hiring Manager, then an on-site interview."
)
REAL_PROSE_TGT = (
    "Ingeniero de ventas nuevo trabajando con gerentes de cuenta para "
    "clientes existentes enfocandose en expansiones de cuenta, etc. "
    "Sueldo para el papel: Base: $115,000. OTE: $140,000 mas o menos. "
    "Notas enviadas al administrador de contratacion: los pasos "
    "siguientes si avanzo seran una entrevista por video de 30 minutos "
    "con el Administrador de Contratacion, y luego una entrevista "
    "presencial en las oficinas de la empresa con el equipo completo."
)

SHORT_UI_EXPANSIONS = [
    ("OK", "Einverstanden", "de short-UI"),
    ("Settings", "Asetukset ja määritykset", "fi short-UI"),
    ("Save", "Сохранить изменения", "ru short-UI"),
    ("Undo", "Rückgängig machen", "de short-UI"),
    ("Next", "Seuraava vaihe", "fi short-UI"),
]


def check_false_positive_locks():
    print("D. false-positive locks (real delivered content -- must stay silent)")
    for src, tgt, label in REAL_ACRONYM_EXPANSIONS:
        if quality.length_runaway(src, tgt):
            _fail("FALSE POSITIVE on a real acronym glossary expansion: " + label)
    _ok("the four real acronym expansions (DTaP / Hib / iSNS / DoS) stay silent")

    surplus = len(REAL_PROSE_TGT) - len(REAL_PROSE_SRC)
    if surplus < 120:
        _fail("fixture drift: the prose pair must carry a >=120-char surplus, got %d" % surplus)  # noqa: UP031
    if quality.length_runaway(REAL_PROSE_SRC, REAL_PROSE_TGT):
        _fail("FALSE POSITIVE on the real +%d-char es prose expansion" % surplus)  # noqa: UP031
    _ok(
        "the real es prose pair (+%d chars at %.2fx) stays silent"  # noqa: UP031
        % (surplus, len(REAL_PROSE_TGT) / len(REAL_PROSE_SRC))
    )

    for src, tgt, label in SHORT_UI_EXPANSIONS:
        if quality.length_runaway(src, tgt):
            _fail("FALSE POSITIVE on a short-UI expansion: " + label)
    _ok("de / fi / ru short-UI expansions stay silent")

    # The offline MockProvider echo is `"[<locale>] " + source` -- it can never clear
    # either lock, at any source length. Asserted for the real format AND the reversed
    # one, and for a long source too.
    for src in (K4_SRC, "Save changes", "x" * 400):
        for echo in ("[ru-RU] " + src, src + " [ru-RU]"):
            if quality.length_runaway(src, echo):
                _fail("FALSE POSITIVE on the MockProvider echo shape (src len %d)" % len(src))  # noqa: UP031
    _ok("the MockProvider echo never trips the gate (offline installs are unaffected)")


# ---------------------------------------------------------------------------
# E. THE SEAM. Key shape, document shape, orchestrator shape, kill switch, staleness.
# ---------------------------------------------------------------------------
_KEY_N = 0


def _new_key(session, project_id):
    """A FRESH StringKey per key-shaped fixture: `ux_segment_key_locale_form` is a partial
    unique index over (key_id, target_locale, plural_category), so two key rows for the
    same locale must belong to two different keys (exactly as they do in production)."""
    global _KEY_N
    _KEY_N += 1
    sk = StringKey(project_id=project_id, namespace="common", name="cta.k%d" % _KEY_N)  # noqa: UP031
    session.add(sk)
    session.commit()
    session.refresh(sk)
    return sk.id


def _seg(session, doc_id, source_text, order, locale="ru-RU", key_id=None):
    s = Segment(
        document_id=doc_id,
        target_locale=locale,
        order_index=order,
        source_text=source_text,
        status=SegStatus.NEW,
        key_id=key_id,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _assert_length_flagged(seg, out, label, session=None):
    """The full must-flag battery for one runaway commit."""
    if not out.committed or out.flagged:
        _fail(f"{label}: the gate must be SOFT (committed, not a refusal-withhold), got {out!r}")
    if seg.status != SegStatus.NEEDS_REVIEW:
        _fail(f"{label}: must route to NEEDS_REVIEW, got {seg.status!r}")
    if out.tm_eligible:
        _fail(f"{label}: a runaway must be TM-INELIGIBLE")
    if not out.length_runaway:
        _fail(f"{label}: CommitOutcome.length_runaway must name the divergence")
    nf = [f for f in (seg.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]
    if len(nf) != 1:
        _fail("%s: expected exactly ONE nontranslation-family flag, got %d" % (label, len(nf)))  # noqa: UP031
    if nf[0].get("severity") != "major":
        _fail("{}: the flag must be MAJOR, got {!r}".format(label, nf[0].get("severity")))
    if not str(nf[0].get("reason") or "").startswith(quality.LENGTH_RUNAWAY_REASON):
        _fail(f"{label}: the flag must carry the distinct LENGTH reason")
    for other in (refusal.NONTRANSLATION_REASON, quality.META_COMMENTARY_REASON):
        if nf[0].get("reason") == other:
            _fail(f"{label}: the length verdict must be DISTINGUISHABLE from the other two")


def check_seam_key_and_document(session, pid, key_doc_id, doc_id):
    """MUST-FLAG (1) and (2): the K4 pair at the seam, on a KEY row and on a DOCUMENT
    segment, with the PRE-PART-2 refusal module bound in.

    Binding the pre-Part-2 refusal is the point, not a workaround: it proves PART 1
    ALONE closes K4. With the live (Part-2) refusal, `is_refusal` withholds the write
    before the length gate is ever reached -- a stronger verdict, asserted in section G.
    So Part 2 can be reverted on its own and K4 stays closed."""
    print("E. the write seam -- MUST-FLAG (part 1 standing alone)")
    if PRE_REFUSAL is None:
        print("  skip - no git history available")
        return
    live_refusal = quality.refusal
    set_pieces(
        "app.services.quality", "refusal", PRE_REFUSAL
    )  # only is_refusal differs; the flag helpers
    try:  # and the family constant are identical
        # (1) THE KEY ROW -- the exact live shape.
        s1 = _seg(session, key_doc_id, K4_SRC, 0, key_id=_new_key(session, pid))
        out1 = quality.commit_mt_output(session, s1, K4_TGT, source="workflow")
        session.commit()
        session.refresh(s1)
        if s1.target_text != K4_TGT:
            _fail("the gate must NEVER rewrite or withhold the target text")
        _assert_length_flagged(s1, out1, "K4 key row")
        _ok("K4 KEY row: committed + text UNCHANGED + NEEDS_REVIEW + length flag + TM-ineligible")

        # (2) THE DOCUMENT SEGMENT -- the general-gap lock. Same input, no key_id.
        s2 = _seg(session, doc_id, K4_SRC, 1)
        out2 = quality.commit_mt_output(session, s2, K4_TGT, source="mt")
        session.commit()
        session.refresh(s2)
        if s2.target_text != K4_TGT:
            _fail("the document shape must also keep the text")
        _assert_length_flagged(s2, out2, "K4 document segment")
        if s2.key_id is not None:
            _fail("fixture error: shape (2) must NOT be a key row")
        _ok("K4 DOCUMENT segment: identical verdict (this is NOT a key-specific gate)")

        # (4) A SECOND RUNAWAY, another language and shape -- and one that no phrase list
        #     covers, so only the structural gate can catch it.
        s3 = _seg(session, doc_id, EN_RUNAWAY_SRC, 2, locale="de-DE")
        out3 = quality.commit_mt_output(session, s3, EN_RUNAWAY_TGT, source="workflow")
        session.commit()
        session.refresh(s3)
        _assert_length_flagged(s3, out3, "en/de chatty runaway")
        _ok("a second runaway (en preamble around a de label) is caught the same way")

        # ...and the hedge-free K4 shape, which is what the NEXT locale will look like.
        s4 = _seg(session, doc_id, K4_SRC, 3)
        out4 = quality.commit_mt_output(session, s4, K4_TGT_NO_HEDGE, source="workflow")
        session.commit()
        session.refresh(s4)
        _assert_length_flagged(s4, out4, "hedge-free K4 shape")
        _ok("the hedge-free K4 shape is caught with no phrase coverage of any kind")

        # SURFACE PARITY: reusing the established family is what makes the finding
        # visible headlessly with no edit to api_cli's allowlist or the editor's set.
        from app.web import api_cli

        row = api_cli._segment_detail_row(s1)
        if not any(f["type"] == refusal.NONTRANSLATION_FLAG_TYPE for f in row["qa_flags"]):
            _fail("the length flag must survive the headless surfaced-flag filter")
        _ok("the flag is surfaced by the /api + CLI detail row (no allowlist edit needed)")
    finally:
        set_pieces("app.services.quality", "refusal", live_refusal)


class _CannedCompletion:
    """Swap `orchestrator.run_completion` for one that returns a fixed text -- the model
    that produced the K4 runaway, replayed deterministically (no provider, no network)."""

    def __init__(self, text):
        self.text = text
        self._orig = None

    def __enter__(self):
        self._orig = orchestrator.run_completion

        def fake(spec, prompt, system=None, task=None, payload=None, **kw):
            return {
                "text": self.text,
                "provider_used": "mock",
                "model_used": "mock",
                "attempts": 1,
                "error": None,
                "usage": {},
            }

        set_pieces("app.services.orchestrator", "run_completion", fake)
        return self

    def __exit__(self, *exc):
        set_pieces("app.services.orchestrator", "run_completion", self._orig)
        return False


def check_orchestrator_shape(session, pid):
    """MUST-FLAG (3): the BUG-06 regression class, driven through a REAL WORKFLOW RUN.

    The orchestrator RESETS status to MT after the seam returns, then re-asserts
    NEEDS_REVIEW for the outcomes it knows about; a flag in a NEW family would be silently
    wiped there. The first cut of this section REPLAYED that block inside the test file and
    then grepped orchestrator.py for one identifying substring -- which is not a lock:
    prefixing the real re-assertion with `if False and ` leaves the grepped text intact and
    the suite green (fix-round finding F6). So this now runs `orchestrator.run_workflow`
    end to end against a canned model output and asserts the verdict survives IN THE REAL
    CODE PATH. The text-level check that remains is a diagnostic hint on failure, not the
    lock."""
    print("F. REAL orchestrator run + the status=MT reset (BUG-06 regression class)")
    # A dedicated document so the run cannot disturb the segments other sections own.
    doc = Document(project_id=pid, filename="k4-wf.md", fmt="md", mode="segmented")
    session.add(doc)
    session.commit()
    session.refresh(doc)
    seg = _seg(session, doc.id, K4_SRC, 0)
    wf = Workflow(name="k4-wf-%d" % doc.id, scope="global")  # noqa: UP031
    session.add(wf)
    session.commit()
    session.refresh(wf)
    session.add(
        WorkflowStep(
            workflow_id=wf.id,
            order_index=0,
            name="Step0",
            step_type=StepType.LLM_AGENT,
            provider="mock",
            prompt_text="",
            chunk_policy="none",
            retry_max=1,
            backoff_base=0.0,
        )
    )
    session.commit()
    doc_id, wf_id, seg_id = doc.id, wf.id, seg.id
    # The HEDGE-FREE runaway: `is_refusal` has no verdict on it, so the LENGTH gate is the
    # only thing standing between this and a clean `status=mt` row -- which is exactly the
    # condition the orchestrator's re-assertion has to survive.
    with _CannedCompletion(K4_WF_RUNAWAY):
        for _ in orchestrator.run_workflow_stream(session, wf_id, doc_id, "ru-RU"):
            pass
    session.expire_all()
    seg = session.get(Segment, seg_id)
    if seg.target_text != K4_WF_RUNAWAY:
        _fail(
            "the real workflow run must COMMIT the text (soft gate), got {!r}".format(
                (seg.target_text or "")[:40]
            )
        )
    if seg.status != SegStatus.NEEDS_REVIEW:
        _fail(
            "a REAL workflow run let the length verdict be wiped by the status=MT "
            f"reset -- the BUG-06 class is back (got {seg.status!r})"
        )
    fam = [
        f
        for f in (seg.qa_flags or [])
        if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
        and str(f.get("reason") or "").startswith(quality.LENGTH_RUNAWAY_REASON)
    ]
    if len(fam) != 1:
        _fail(f"the real run must leave exactly one length verdict, got {seg.qa_flags!r}")
    if fam[0].get("source") != "workflow":
        _fail(f"the real run must record the workflow provenance, got {fam[0]!r}")
    _ok(
        "REAL run_workflow_stream: text committed, NEEDS_REVIEW survives the status=MT "
        "reset, one length flag tagged source=workflow"
    )

    # DIAGNOSTIC (not the lock -- the run above is): if a refactor turns the family-wide
    # re-assertion into per-outcome field checks, the run fails first and this names why.
    with open(os.path.join(_ROOT, "app", "services", "orchestrator.py"), encoding="utf-8") as fh:
        orch = fh.read()
    if "id(f) not in _nt_before" not in orch or "NONTRANSLATION_FLAG_TYPE" not in orch:
        _fail(
            "the orchestrator's family-wide nontranslation re-assertion is gone -- "
            "the length verdict is no longer protected from the status=MT reset"
        )
    _ok("(diagnostic) the family-wide re-assertion is still the mechanism in the source")


def check_kill_switch(session, doc_id):
    print("G. REFUSAL_GUARD_ENABLED=0 -> byte-identical to pre-fix")
    prev = config.REFUSAL_GUARD_ENABLED
    try:
        config.REFUSAL_GUARD_ENABLED = False
        seg = _seg(session, doc_id, K4_SRC, 20)
        out = quality.commit_mt_output(session, seg, K4_TGT, source="mt")
        session.commit()
        session.refresh(seg)
        if out.length_runaway:
            _fail("the kill switch must disable the length gate")
        if seg.status != SegStatus.MT or seg.qa_flags or not out.tm_eligible:
            _fail(
                f"guard OFF must be byte-identical to pre-fix, got {seg.status!r} / {seg.qa_flags!r} / {out.tm_eligible!r}"  # noqa: E501
            )
        if seg.target_text != K4_TGT:
            _fail("guard OFF must still write the text")
        # ...and with the guard off, a stale length flag is NOT cleared either (the whole
        # gate is inert, not half-on).
        seg2 = _seg(session, doc_id, "Hello world.", 21)
        seg2.qa_flags = [quality.length_runaway_flag(quality.LENGTH_RUNAWAY_REASON + ": stale")]
        session.add(seg2)
        session.commit()
        quality.commit_mt_output(session, seg2, "Privet mir.", source="mt")
        session.commit()
        session.refresh(seg2)
        if not seg2.qa_flags:
            _fail("guard OFF must not run the clearing half either")
        _ok("guard OFF: no flag, status mt, TM-eligible, and the clearing half is inert too")
    finally:
        config.REFUSAL_GUARD_ENABLED = prev


def check_stale_clearing(session, doc_id):
    """A flag that is only ever ADDED records a verdict about text that no longer
    exists. A later CLEAN commit must clear the LENGTH badge and NOTHING else."""
    print("H. stale-badge clearing (scoped to the length verdict only)")
    if PRE_REFUSAL is None:
        print("  skip - no git history available")
        return
    live_refusal = quality.refusal
    set_pieces("app.services.quality", "refusal", PRE_REFUSAL)
    try:
        seg = _seg(session, doc_id, K4_SRC, 30)
        quality.commit_mt_output(session, seg, K4_TGT, source="workflow")
        session.commit()
        session.refresh(seg)
        if not [
            f for f in (seg.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
        ]:
            _fail("setup: the runaway commit must set a nontranslation-family flag")
        # A proper short translation lands -> the badge must go.
        quality.commit_mt_output(session, seg, "Начать", source="workflow")
        session.commit()
        session.refresh(seg)
        if [f for f in (seg.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]:
            _fail(f"a clean re-commit must clear the length badge, got {seg.qa_flags!r}")
        if seg.status != SegStatus.MT:
            _fail(f"a clean re-commit must return the segment to MT, got {seg.status!r}")
        _ok("a clean re-commit clears the length badge and restores status=mt")

        # SCOPING: a clean commit clears ONLY the length verdict and leaves every other
        # family -- including the LANGUAGE verdict that shares its flag type -- intact.
        seg2 = _seg(session, doc_id, "Hello world.", 31)
        seg2.qa_flags = [
            quality.length_runaway_flag(quality.LENGTH_RUNAWAY_REASON + ": stale"),
            refusal.nontranslation_flag("mt"),
            {"type": "qe", "severity": "minor", "message": "keep me"},
            {"type": "dnt_violation", "severity": "major", "message": "keep me too"},
            {
                "type": quality.TAG_FLAG_TYPE,
                "severity": "major",
                "message": "keep me three",
                "reason": "tag",
            },
        ]
        session.add(seg2)
        session.commit()
        # `clear_length_runaway_flag` is the unit under test here -- the seam's tag gate
        # would independently clear the tag flag on a real commit, which is correct but
        # would blur what this assertion is proving.
        left = quality.clear_length_runaway_flag(seg2.qa_flags)
        reasons = [
            f.get("reason") for f in left if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
        ]
        if reasons != [refusal.NONTRANSLATION_REASON]:
            _fail(f"the LANGUAGE verdict must survive the length clear, got {left!r}")
        types = [f.get("type") for f in left]
        for kept in ("qe", "dnt_violation", quality.TAG_FLAG_TYPE):
            if kept not in types:
                _fail(f"clearing the length badge must not touch {kept!r}, got {left!r}")
        _ok("clearing is scoped: qe / dnt / tag_integrity / the LANGUAGE verdict all survive")
        if quality.clear_length_runaway_flag(None) is not None:
            _fail("an untouched column must stay None (byte-identity)")
        if (
            quality.clear_length_runaway_flag(
                [quality.length_runaway_flag(quality.LENGTH_RUNAWAY_REASON)]
            )
            is not None
        ):
            _fail("clearing the only flag must yield None, never []")
        _ok("an unflagged / fully cleared column reads as None, never []")
    finally:
        set_pieces("app.services.quality", "refusal", live_refusal)


def check_subordination(session, doc_id):
    """Language > meta > length. The family holds ONE entry, so the stronger, more
    specific verdict must be the one a reviewer sees."""
    print("I. verdict subordination within the shared family")
    # A runaway that ALSO carries a translator note: the META verdict must win.
    note_tgt = (
        "El equipo de soporte revisara su solicitud y le respondera dentro de "
        "dos dias habiles con toda la informacion sobre la cuenta.\n\n"
        "*(Note: I corrected all issues identified in the feedback to comply "
        "with the es-ES guidelines and the project glossary.)*"
    )
    seg = _seg(session, doc_id, "Ask us.", 40, locale="es-ES")
    out = quality.commit_mt_output(session, seg, note_tgt, source="workflow")
    session.commit()
    session.refresh(seg)
    if not quality.length_runaway("Ask us.", note_tgt):
        _fail("fixture drift: this pair must ALSO be a length runaway")
    if not out.meta_commentary:
        _fail("the meta verdict must fire on this pair")
    nf = [f for f in (seg.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]
    if len(nf) != 1:
        _fail("the shared family must hold exactly ONE entry, got %d" % len(nf))  # noqa: UP031
    if quality.META_COMMENTARY_REASON not in (nf[0].get("reason") or ""):
        _fail(
            "META must win over LENGTH (it is the more specific verdict), got {!r}".format(
                nf[0].get("reason")
            )
        )
    # SUBORDINATION IS ABOUT THE FLAG, NOT THE FIELD. `CommitOutcome.length_runaway` still
    # reports the detector's verdict -- exactly as `meta_commentary` is still reported when
    # the LANGUAGE verdict outranks it -- so a caller can see every gate that fired. Only
    # the single shared qa_flag entry is subordinated.
    if not out.length_runaway:
        _fail("the length reason must still be REPORTED on the outcome (as meta is)")
    if seg.status != SegStatus.NEEDS_REVIEW or out.tm_eligible:
        _fail("either verdict must route to review and block TM")
    _ok("meta beats length: one flag, the meta reason, still NEEDS_REVIEW + TM-ineligible")

    # A runaway with lang_guard ON and an English-for-ru target: the LANGUAGE verdict wins.
    en_for_ru = (
        "The customer support team will review your ticket and respond to you "
        "within two business days regarding the account you asked about today."
    )
    seg2 = _seg(session, doc_id, "Ask us.", 41)
    out2 = quality.commit_mt_output(session, seg2, en_for_ru, source="mt", lang_guard=True)
    session.commit()
    session.refresh(seg2)
    if not quality.length_runaway("Ask us.", en_for_ru):
        _fail("fixture drift: this pair must ALSO be a length runaway")
    nf2 = [f for f in (seg2.qa_flags or []) if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE]
    if len(nf2) != 1 or nf2[0].get("reason") != refusal.NONTRANSLATION_REASON:
        _fail(f"LANGUAGE must win over LENGTH, got {seg2.qa_flags!r}")
    if not out2.length_runaway:
        _fail("the length reason must still be REPORTED on the outcome")
    _ok("language beats length: one flag, the language reason (both fields still reported)")


# ---------------------------------------------------------------------------
# J. PART 2 -- Russian phrase parity, and its DELIBERATE consequence.
# ---------------------------------------------------------------------------
def check_part2_russian_parity(session, pid, key_doc_id):
    print("J. part 2: Russian parity in the target-language phrase tiers")
    # Recall: the four tiers now cover ru the way they already covered es/fr/de/it/pt.
    for tgt, label in [
        ("Привет! Я готов перевести этот текст.", "preamble + ready-to-translate stem"),
        ("Я не могу перевести этот текст.", "cannot-translate stem"),
        ("Текст для перевода не предоставлен.", "framed refusal, explicit verb (position 0)"),
        ("Я — языковая модель, обученная переводу.", "em-dash model self-description"),
        ("Я языковая модель.", "copula-less model self-description"),
    ]:
        if not refusal.is_refusal(tgt):
            _fail("Russian recall MISS: " + label)
    _ok("all four ru tiers hit (stems / leading / meta / preamble-then-stem)")

    # Precision: ordinary Russian product copy must stay clean, including the shapes the
    # new entries deliberately do NOT cover.
    for tgt, label in [
        ("Начать сейчас", "the CORRECT translation of the K4 source"),
        ("Настройки конфиденциальности", "a UI label"),
        ("Текст для перевода.", "a HEADING ('Text for translation') -- never at risk"),
        ("Я не могу открыть файл.", "first person, but about the PRODUCT"),
        (
            "Я переводчик в этой компании.",
            "'I am a translator' -- a PERSON, deliberately not listed",
        ),
        (
            "Нет текста в этом поле.",
            "'no text in this field' -- an empty-state label, no translation verb",
        ),
        ("Здравствуйте! Добро пожаловать в Acme Cloud.", "a greeting in real product copy"),
        ("Хорошо, изменения сохранены.", "a preamble word leading ordinary content"),
    ]:
        if refusal.is_refusal(tgt):
            _fail("Russian FALSE POSITIVE: " + label)
    _ok("ordinary ru product copy, bare labels and the correct K4 translation stay clean")

    # -----------------------------------------------------------------------
    # PARITY, DEFENDED BY TEST RATHER THAN BY COMMENT (fix-round finding F4).
    #
    # The first cut of this tier listed "нечего переводить" and "нет текста для перевода"
    # -- the two UNMARKED Russian EMPTY-STATE UI LABELS -- while claiming in its comment to
    # honour "the same copula/frame discipline as fr/de/it/nl". It did not: every peer
    # language SPARES its bare empty-state label and flags only the framed refusal, so
    # Russian alone was destroying real strings. `is_refusal` WITHHOLDS the write, so the
    # cost of that asymmetry was a LOST TRANSLATION on the HARD path, not a stray badge.
    # The rule is now asserted across all seven languages at once, in both directions.
    # -----------------------------------------------------------------------
    spare_flag_pairs = [
        ("en", "Nothing to translate.", "There is nothing to translate."),
        ("de", "Kein Text zum Übersetzen.", "Es gibt keinen Text zu übersetzen."),
        ("fr", "Rien à traduire.", "Il n'y a rien à traduire."),
        ("it", "Niente da tradurre.", "Non c'è niente da tradurre."),
        ("nl", "Niets om te vertalen.", "Er is niets om te vertalen."),
        ("es", "Nada que traducir.", "No hay nada que traducir."),
        # Portuguese: the audit names pt among the sparing languages, so it is ASSERTED
        # here rather than only claimed -- seven languages became eight (final-round F12).
        ("pt", "Nada para traduzir.", "Não há nada para traduzir."),
        ("pt", "Nenhum texto para traduzir.", "Não há texto para traduzir."),
        # Russian: the label shape IS the "нет ..." existential (the language has no bare
        # NP alternative), so parity puts BOTH of these on the spared side and keeps only
        # the verb-carrying framed refusals on the flagged side.
        ("ru", "Нет текста для перевода.", "Текст для перевода не предоставлен."),
        ("ru", "Нечего переводить.", "Текст для перевода отсутствует."),
    ]
    for lang, bare, framed in spare_flag_pairs:
        if refusal.is_refusal(bare):
            _fail(
                f"PARITY BREAK: the bare {lang} empty-state LABEL is flagged -- is_refusal "
                "withholds the write, so this DESTROYS a real translation"
            )
        if not refusal.is_refusal(framed):
            _fail(f"PARITY BREAK: the framed {lang} refusal is no longer caught")
    _ok(
        "bare empty-state labels spared and framed refusals flagged in all of "
        "en/de/fr/it/nl/es/pt/ru -- ru is no longer stricter than its peers"
    )

    # ...and dropping the two ru label forms cost NOTHING on K4: the live value is caught
    # by the preamble strip + `_TARGET_STEMS`, and by the structural gate independently.
    if not refusal.is_refusal(K4_TGT):
        _fail("dropping the ru label forms cost K4 recall in the phrase tier")
    if not quality.length_runaway(K4_SRC, K4_TGT):
        _fail("dropping the ru label forms cost K4 recall in the structural gate")
    _ok("K4 recall is unaffected by the F4 removal (preamble+stem, and part 1 on its own)")

    # THE DELIBERATE CONSEQUENCE (the maintainer's call, recorded in the part-2 commit message and
    # in the audit doc): `is_refusal` WITHHOLDS the write, so the exact K4 value now lands
    # EMPTY at needs_review rather than flagged-with-text -- exactly what already happens
    # for Spanish and German today. This is the HARD path and it is asserted, not implied.
    seg = _seg(session, key_doc_id, K4_SRC, 50, key_id=_new_key(session, pid))
    out = quality.commit_mt_output(session, seg, K4_TGT, source="workflow")
    session.commit()
    session.refresh(seg)
    if out.committed or not out.flagged:
        _fail(f"with part 2 in, the exact K4 value must be WITHHELD by is_refusal, got {out!r}")
    if (seg.target_text or "").strip():
        _fail("a refusal withholds the write -- the value must land EMPTY")
    if seg.status != SegStatus.NEEDS_REVIEW or out.tm_eligible:
        _fail(
            f"a withheld refusal must be NEEDS_REVIEW + TM-ineligible, got {seg.status!r} / {out.tm_eligible!r}"  # noqa: E501
        )
    if not [f for f in (seg.qa_flags or []) if f.get("type") == "refusal"]:
        _fail(f"a withheld refusal must carry the refusal flag, got {seg.qa_flags!r}")
    _ok(
        "EXACT K4 value with part 2 in: WITHHELD (empty) + refusal flag + NEEDS_REVIEW "
        "-- the deliberate, documented consequence"
    )

    # ...and the structural gate is what still covers the hedge-free shape, which no
    # phrase list in any language reaches. This is why part 1 is the primary fix.
    seg2 = _seg(session, key_doc_id, K4_SRC, 51, key_id=_new_key(session, pid))
    out2 = quality.commit_mt_output(session, seg2, K4_TGT_NO_HEDGE, source="workflow")
    session.commit()
    session.refresh(seg2)
    if seg2.target_text != K4_TGT_NO_HEDGE:
        _fail("the hedge-free shape must still be COMMITTED with its text kept")
    _assert_length_flagged(seg2, out2, "hedge-free K4, full perimeter")
    _ok("with the FULL perimeter, the hedge-free runaway is still caught by part 1 alone")


# ---------------------------------------------------------------------------
# K. Neighbouring gates unchanged (no regression in the BUG-06 pairs or the fixtures).
# ---------------------------------------------------------------------------
def check_neighbours_unchanged(session, doc_id):
    print("K. the neighbouring gates are untouched")
    # BUG-06 (a): the number corruption still routes, and still under its OWN flag type.
    s = _seg(session, doc_id, "You have 25 items in your cart.", 60, locale="es-ES")
    out = quality.commit_mt_output(
        session, s, "Tienes 25.000 articulos en tu carrito.", source="workflow"
    )
    session.commit()
    session.refresh(s)
    if not out.number_violation or out.length_runaway:
        _fail(f"the number pair must fire the NUMBER gate only, got {out!r}")
    if quality.NUMBER_FLAG_TYPE not in [f.get("type") for f in (s.qa_flags or [])]:
        _fail("the number_integrity flag is gone")
    _ok("BUG-06 numbers: unchanged (number_integrity flag, no length verdict)")

    # BUG-06 (b): the live translator note still routes under the commentary reason.
    note = (
        "Revisa tu correo electronico para continuar con la subida.\n\n"
        "*(Note: I corrected all issues identified in the feedback to produce a "
        "final, polished translation that complies with es-ES conventions.)*"
    )
    s2 = _seg(session, doc_id, "Check your email to continue with the upload.", 61, locale="es-ES")
    out2 = quality.commit_mt_output(session, s2, note, source="workflow")
    session.commit()
    session.refresh(s2)
    if not out2.meta_commentary:
        _fail("the BUG-06 commentary pair stopped firing")
    reasons = [
        f.get("reason")
        for f in (s2.qa_flags or [])
        if f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
    ]
    if len(reasons) != 1 or quality.META_COMMENTARY_REASON not in (reasons[0] or ""):
        _fail(f"the commentary verdict changed shape, got {s2.qa_flags!r}")
    _ok("BUG-06 commentary: unchanged (one family entry, the commentary reason)")

    # The tag gate and a plain clean commit: byte-identical.
    s3 = _seg(session, doc_id, "⟦r1⟧Hello⟦/r1⟧ world.", 62, locale="es-ES")
    out3 = quality.commit_mt_output(session, s3, "⟦r1⟧Hola⟦/r1⟧ mundo.", source="mt")
    session.commit()
    session.refresh(s3)
    if s3.status != SegStatus.MT or s3.qa_flags or not out3.tm_eligible:
        _fail(f"a clean tagged commit must stay clean, got {s3.status!r} / {s3.qa_flags!r}")
    if s3.qa_flags is not None:
        _fail(f"an unflagged column must stay None, got {s3.qa_flags!r}")
    _ok("a clean tagged commit is byte-identical (status mt, qa_flags None, TM-eligible)")


def main():
    print("verify_length_runaway -- K4 (MT length-runaway gate + ru phrase parity)")
    init_db()
    with Session(engine) as session:
        p = Project(
            name="K4",
            source_locale="en-US",
            target_locales=["ru-RU"],
            default_provider="mock",
            project_type=ProjectType.KEY_BASED,
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        # The KEY shape: a system-owned internal document (fmt="keys") + a StringKey.
        kd = Document(project_id=p.id, filename="common.keys", fmt="keys", mode="segmented")
        session.add(kd)
        # The DOCUMENT shape: an ordinary uploaded document in the same install.
        d = Document(project_id=p.id, filename="k4.md", fmt="md", mode="segmented")
        session.add(d)
        session.commit()
        session.refresh(kd)
        session.refresh(d)
        check_fail_before(session, d.id)
        check_root_cause()
        check_detector()
        check_no_cjk_carveout()
        check_cost_is_constant()
        check_false_positive_locks()
        check_seam_key_and_document(session, p.id, kd.id, d.id)
        check_orchestrator_shape(session, p.id)
        check_kill_switch(session, d.id)
        check_stale_clearing(session, d.id)
        check_subordination(session, d.id)
        check_part2_russian_parity(session, p.id, kd.id)
        check_neighbours_unchanged(session, d.id)
    print("verify_length_runaway OK")


if __name__ == "__main__":
    main()
