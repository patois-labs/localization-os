"""Engine-level tests for ConnectorTranslateFlow's invariant stages.

The parity harness (verify_flow_engine_parity) proves the four migrated flows are
byte-identical to their pre-refactor selves. THIS suite instead targets the shared
engine's owned mechanics directly, so a regression in the engine is caught ONCE
(here) rather than having to be re-discovered per connector — and so the NEXT flow
built on the engine (Intercom, Salesforce, ...) inherits proven coverage:

  * stage 7  WriteBudget ceiling-trim: build-time cap, ceiling_hit latch, and a
             ceiling-trimmed article writing exactly the remaining budget.
  * stage 10 FINDING-1 watermark clamp: a fully-settled NEWER article must not
             leapfrog a ceiling-trimmed OLDER one (silent-data-loss fix), and the
             held article re-surfaces on the next run.
  * stage 6  hash-merge concurrency discipline: patch_config_merge keeps each run's
             delta (never clobbers) and the watermark is monotonic (never regresses).
  * no-watermark flows never have watermark state introduced by the engine.

Uses the shared Fake tenants from _flow_parity_fixtures (Zendesk is the reference
consumer and the only watermark flow). ASCII-only prints; final line
'verify_flow_engine OK'.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _flow_parity_fixtures as fx

from app.services.connectors import store as cstore
from app.services.connectors.store import WriteBudget, patch_config_merge

# epoch("2026-07-01T00:00:00Z") — the reference edited_at used below (see goldens).
_EPOCH_JUL1 = 1782864000


def _t_writebudget():
    """stage 7: the ONE ceiling implementation the engine trims pending locales with."""
    b = WriteBudget(3)
    assert b.remaining() == 3 and not b.ceiling_hit
    assert b.trim(2) == 2 and b.remaining() == 1 and not b.ceiling_hit, "partial fits, no latch"
    assert b.trim(5) == 1 and b.remaining() == 0 and b.ceiling_hit, "clamp latches ceiling_hit"
    assert b.trim(1) == 0 and b.ceiling_hit, "nothing left"
    # A deliberate 0 must mean zero writes (operator kill-switch), never fail-open to 500.
    z = WriteBudget(0)
    assert z.trim(4) == 0 and z.ceiling_hit and z.remaining() == 0, "explicit 0 = no writes"
    print("  stage 7: WriteBudget.trim caps + latches ceiling_hit; explicit 0 = no writes")


def _t_ceiling_trim_flow():
    """stage 7 at the flow level: a run capped mid-batch writes EXACTLY the budget."""
    fz = fx.FakeZendesk(
        articles=[fx._zd_article("100"), fx._zd_article("101"), fx._zd_article("102")]
    )  # 3 x 2 locales = 6 wanted
    conn = fx._zd_conn("ENG ceiling flow")
    with fx._CeilingPatch(3):
        ok, _msg, res = cstore.zendesk_translate(conn.id, actor="auto", transport=fz.transport())
    assert ok and res["ceiling_hit"] is True and res["write_ceiling"] == 3, res
    assert (res["created"] + res["updated"]) == 3, ("exactly the budget written", res)
    assert len(fz.creates) == 3, ("no extra external writes past the ceiling", fz.creates)
    print("  stage 7: a ceiling-capped run performs EXACTLY the budgeted external writes")


def _t_watermark_clamp():
    """stage 10 (FINDING-1): a settled NEWER article must not advance the watermark
    past a ceiling-trimmed OLDER one, and the held article re-surfaces next run."""
    newer = fx._zd_article("2", edited_at="2026-07-03T00:00:00Z")
    older = fx._zd_article("1", edited_at="2026-07-01T00:00:00Z")
    # Discover order = insertion order: newer first. Budget = 2 = exactly newer's pair.
    fz = fx.FakeZendesk(articles=[newer, older])
    conn = fx._zd_conn("ENG clamp")
    with fx._CeilingPatch(2):
        _ok, _msg, res = cstore.zendesk_translate(conn.id, actor="auto", transport=fz.transport())
    assert res["ceiling_hit"] is True, res
    # Scalar max would be epoch(Jul-3); the clamp holds it to just BELOW the trimmed
    # older article (epoch(Jul-1) - 1) so the older one is never silently skipped.
    assert res["watermark"] == _EPOCH_JUL1 - 1, (
        "clamped below oldest incomplete",
        res["watermark"],
    )
    cfg = cstore.get(conn.id).config
    assert cfg["zendesk_watermark"] == _EPOCH_JUL1 - 1, cfg
    assert "1:de" not in (cfg.get("zendesk_hashes") or {}), ("older art never written", cfg)
    # Re-surface: a second run (budget restored) writes the previously-trimmed older art.
    fz2 = fx.FakeZendesk(articles=[older])
    _ok2, _msg2, res2 = cstore.zendesk_translate(conn.id, actor="auto", transport=fz2.transport())
    assert res2["created"] == 2, ("held older article re-surfaced + written", res2)
    print(
        "  stage 10: FINDING-1 clamp holds the watermark below a trimmed older "
        "article; it re-surfaces next run"
    )


def _t_hash_merge():
    """stage 6/10 persistence: patch_config_merge merges deltas (no clobber) and the
    watermark is monotonic — the discipline that makes two concurrent runs commutative."""
    conn = fx._zd_conn("ENG merge")
    patch_config_merge(
        conn.id,
        "zendesk_hashes",
        {"a:de": "h1"},
        watermark_key="zendesk_watermark",
        watermark_at_least=100,
    )
    # A concurrently-finishing run persists a DIFFERENT delta + a LOWER watermark.
    patch_config_merge(
        conn.id,
        "zendesk_hashes",
        {"b:fr": "h2"},
        watermark_key="zendesk_watermark",
        watermark_at_least=50,
    )
    cfg = cstore.get(conn.id).config
    assert cfg["zendesk_hashes"] == {"a:de": "h1", "b:fr": "h2"}, ("both deltas kept", cfg)
    assert cfg["zendesk_watermark"] == 100, ("watermark takes max, never regresses", cfg)
    # An overlapping key updates in place; a still-lower watermark can't regress it.
    patch_config_merge(
        conn.id,
        "zendesk_hashes",
        {"a:de": "h1b"},
        watermark_key="zendesk_watermark",
        watermark_at_least=30,
    )
    cfg = cstore.get(conn.id).config
    assert cfg["zendesk_hashes"]["a:de"] == "h1b" and cfg["zendesk_watermark"] == 100, cfg
    print("  stage 6/10: patch_config_merge keeps every run's delta + monotonic watermark")


def _t_no_watermark_introduced():
    """A poll-only flow (no watermark_key) must NOT get watermark state written by the
    engine (spec acceptance: don't accidentally introduce incremental behavior)."""
    for label, run, fake, mk in (
        (
            "document360",
            cstore.document360_translate,
            fx.FakeD360(articles=[fx._d360_md_article("100")]),
            fx._d360_conn,
        ),
        (
            "freshdesk",
            cstore.freshdesk_translate,
            fx.FakeFreshdesk(articles=[fx._fd_article("100")]),
            fx._fd_conn,
        ),
        ("zoho", cstore.zoho_translate, fx.FakeZoho(articles=[fx._zo_article("100")]), fx._zo_conn),
    ):
        conn = mk(f"ENG nowm {label}")
        run(conn.id, actor="auto", transport=fake.transport())
        cfg = cstore.get(conn.id).config or {}
        assert not any("watermark" in k for k in cfg), (label, "no watermark introduced", cfg)
    print("  no-watermark flows (d360/freshdesk/zoho) stay watermark-free through the engine")


def main() -> None:
    fx.init_db()
    _t_writebudget()
    _t_ceiling_trim_flow()
    _t_watermark_clamp()
    _t_hash_merge()
    _t_no_watermark_introduced()
    print("verify_flow_engine OK")


if __name__ == "__main__":
    main()
