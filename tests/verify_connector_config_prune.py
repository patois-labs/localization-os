"""Connector config-map ORPHAN PRUNING -- recorded chip ("connector config-map pruning"),
REDESIGNED per adversarial review (H2/M3): an EXPLICIT operator verb, DRY-RUN by default,
restricted to an allow-list of pure content-hash maps, protecting child-id / written /
field-keyed maps and the primary/source locale.

  A  DRY-RUN default: dead-locale entries are REPORTED but NOTHING is mutated.
  B  apply=True removes exactly the reported dead entries; LIVE-locale entries preserved.
  C  ALLOW-LIST: non-allow-listed maps are NEVER touched, even with apply=True --
     `airtable_written_hashes` (keyed "<ext_id>:src|url", which LOOK like locales) and
     `confluence_children` (id-recording) are left byte-identical (the M3 data-loss proofs).
  D  PRIMARY/SOURCE locale is protected: an "<id>:<source>" entry is kept (keep = configured
     UNION primary_locale), so a source-locale hash is never swept.
  E  IDEMPOTENT: a second apply removes nothing.
  F  prune_all_orphaned_config aggregates dry-run + apply across connections.

Hermetic: throwaway LOC_TMS_DATA_DIR + cleared provider keys; ASCII-only prints.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_cfgprune_")

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Connection
from app.services.connectors import store as conn_store

_SEQ = [0]


def _seq():
    _SEQ[0] += 1
    return _SEQ[0]


def _mk_conn(cfg):
    with Session(engine) as s:
        c = Connection(kind="zendesk", name="cfgprune-%d" % _seq(), config=cfg)  # noqa: UP031
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _cfg(cid):
    with Session(engine) as s:
        return dict(s.get(Connection, cid).config or {})


def test_A_dry_run_default_mutates_nothing():
    cid = _mk_conn(
        {
            "target_locales": "fr-FR,de-DE",
            "zendesk_hashes": {"1:fr-fr": "h1", "1:es-es": "h2", "2:de-de": "h3"},
        }
    )
    rep = conn_store.prune_orphaned_config(cid)  # dry-run (apply defaults False)
    assert rep["applied"] is False, rep
    assert rep["total"] == 1, rep
    assert rep["removed"] == [{"map": "zendesk_hashes", "keys": ["1:es-es"], "count": 1}], rep
    # NOTHING removed on disk.
    assert _cfg(cid)["zendesk_hashes"] == {"1:fr-fr": "h1", "1:es-es": "h2", "2:de-de": "h3"}
    print("A ok: dry-run default reports dead entries but mutates nothing")


def test_B_apply_removes_dead_keeps_live():
    cid = _mk_conn(
        {
            "target_locales": "fr-FR,de-DE",
            "zendesk_hashes": {"1:fr-fr": "h1", "1:es-es": "h2", "2:de-de": "h3", "2:es-es": "h4"},
        }
    )
    rep = conn_store.prune_orphaned_config(cid, apply=True)
    assert rep["applied"] is True and rep["total"] == 2, rep
    assert _cfg(cid)["zendesk_hashes"] == {"1:fr-fr": "h1", "2:de-de": "h3"}
    print("B ok: apply removes dead-locale entries; live-locale entries preserved")


def test_C_allow_list_protects_field_and_child_maps():
    cid = _mk_conn(
        {
            "target_locales": "fr-FR",
            # field-keyed map whose "field" suffixes LOOK like locales -> must be untouched
            "airtable_written_hashes": {"rec1:src": "x", "rec2:url": "y"},
            # id-recording child map -> pruning would duplicate child pages -> untouched
            "confluence_children": {"9:es-es": "wid-1"},
            # allow-listed content-hash map WITH a real orphan (es-es dropped)
            "zendesk_hashes": {"9:fr-fr": "h", "9:es-es": "dead"},
        }
    )
    rep = conn_store.prune_orphaned_config(cid, apply=True)
    # only the allow-listed map's orphan is touched
    assert rep["total"] == 1, rep
    assert {m["map"] for m in rep["removed"]} == {"zendesk_hashes"}, rep
    cfg = _cfg(cid)
    assert cfg["airtable_written_hashes"] == {"rec1:src": "x", "rec2:url": "y"}, "field map swept!"
    assert cfg["confluence_children"] == {"9:es-es": "wid-1"}, "child-id map swept!"
    assert cfg["zendesk_hashes"] == {"9:fr-fr": "h"}
    print("C ok: allow-list protects airtable_written_hashes + confluence_children")


def test_D_primary_source_locale_protected():
    # explicit source-ish primary (target_locale) + real targets; a source-locale hash is kept.
    cid = _mk_conn(
        {
            "target_locale": "en-US",
            "target_locales": "fr-FR",
            "zendesk_hashes": {"1:en-us": "src", "1:fr-fr": "h", "1:es-es": "dead"},
        }
    )
    rep = conn_store.prune_orphaned_config(cid, apply=True)
    assert rep["total"] == 1, rep  # only es-es
    assert _cfg(cid)["zendesk_hashes"] == {"1:en-us": "src", "1:fr-fr": "h"}

    # R2: the config SOURCE locale (not a target) is ALSO in the keep set -- an "<id>:<source>"
    # hash survives even when source_locale is not among the target locales.
    cid2 = _mk_conn(
        {
            "source_locale": "en-GB",
            "target_locales": "fr-FR",
            "zendesk_hashes": {"1:en-gb": "src", "1:fr-fr": "h", "1:es-es": "dead"},
        }
    )
    rep = conn_store.prune_orphaned_config(cid2, apply=True)
    assert rep["total"] == 1, rep  # only es-es
    assert _cfg(cid2)["zendesk_hashes"] == {"1:en-gb": "src", "1:fr-fr": "h"}
    print("D ok: primary + SOURCE locale entries protected (keep = configured U primary U source)")


def test_E_idempotent():
    cid = _mk_conn(
        {"target_locales": "fr-FR", "zendesk_hashes": {"1:fr-fr": "h", "1:es-es": "dead"}}
    )
    assert conn_store.prune_orphaned_config(cid, apply=True)["total"] == 1
    assert conn_store.prune_orphaned_config(cid, apply=True)["total"] == 0
    print("E ok: idempotent (second apply removes nothing)")


def test_F_prune_all():
    # isolate: wipe connections left by earlier tests so the aggregate is deterministic.
    with Session(engine) as s:
        for row in s.exec(select(Connection)).all():
            s.delete(row)
        s.commit()
    a = _mk_conn({"target_locales": "fr-FR", "zendesk_hashes": {"1:fr-fr": "h", "1:es-es": "dead"}})
    b = _mk_conn({"target_locales": "de-DE", "zendesk_hashes": {"2:de-de": "h", "2:it-it": "dead"}})
    dry = conn_store.prune_all_orphaned_config()  # dry-run
    assert dry["applied"] is False and dry["total"] == 2, dry
    assert _cfg(a)["zendesk_hashes"] == {"1:fr-fr": "h", "1:es-es": "dead"}, "dry-run mutated!"
    done = conn_store.prune_all_orphaned_config(apply=True)
    assert done["applied"] is True and done["total"] == 2, done
    assert _cfg(a)["zendesk_hashes"] == {"1:fr-fr": "h"}
    assert _cfg(b)["zendesk_hashes"] == {"2:de-de": "h"}
    print("F ok: prune_all aggregates dry-run + apply across connections")


def main():
    init_db()
    test_A_dry_run_default_mutates_nothing()
    test_B_apply_removes_dead_keeps_live()
    test_C_allow_list_protects_field_and_child_maps()
    test_D_primary_source_locale_protected()
    test_E_idempotent()
    test_F_prune_all()
    print("verify_connector_config_prune OK")


if __name__ == "__main__":
    main()
