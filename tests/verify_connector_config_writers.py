"""Reviewed config writers must not grow or change input handling without review."""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.connector_config_writers import inventory

# Full enclosing-function AST hashes cover the source of cfg, not just `row.config = cfg`.
REVIEWED = {
    "airtable_webhooks.py:_persist_webhook_health": (
        "93d0475a07f55e55ee3d6275a73f6f69f974384dbd031559318480bdf6ba75e6"
    ),
    "airtable_webhooks.py:_advance_airtable_cursor": (
        "77b33adb141040023aa298eb658f9060de6eb0d717c0dfef738ede97c1271bb1"
    ),
    "airtable_webhooks.py:_touch_webhook_ping": (
        "bce51c9a459d7a311163c2ec3e848b709d5481ea2a4d50e0fe82b0ba0231e10c"
    ),
    "airtable_webhooks.py:_add_pending_records": (
        "703ea166f6a16df354815de8cff3fc290f10e66470545cf9b91706902375de19"
    ),
    "airtable_webhooks.py:_remove_pending_records": (
        "49735f77d96492ce44abed048451b8d197b37deaeada380cc560be63855b4523"
    ),
    # Re-pinned by the 10-07 release sweep: #579 (66d6df5fd) added self.fetch_notes and
    # #587 (97006ab4d) added self._fetch_chars to Connector.__init__. Reviewed: neither
    # touches config or secret; self.config = config or {} and its input are unchanged, so
    # credential-host confinement holds.
    "base.py:__init__": ("1da3357c41f1fc1e4b792286b7d133fed24973e600202d713fd179289a0cb64d"),
    # Added by the next/connectors full sweep (2026-10-10), Plytix 9c3a6005b. Reviewed: writes
    # only `plytix_patch_semantics` (merge_proven / replace_detected) via the reviewed
    # store.patch_config_merge; Plytix is a fixed-host kind (pim/auth.plytix.com), no host,
    # URL or secret is read or written. An admin can still set the key via `connector update
    # --config`: accepted in the plytix review as an operator decision on their own data.
    "plytix.py:_persist_semantics": (
        "04e644a8a1f394ab6843a4e9beb709de065944bef245937990eecc853e0c89d6"
    ),
    "store.py:jira_translate_pending": (
        "09e82d6a338ba300c5c52c5bde579973013c11b3dcfcec3fdb88248f144f6a85"
    ),
    # Re-pinned by the next/connectors full sweep (2026-10-10): GitHub 77060bfdc added
    # `_drop_server_owned(cfg)` to create and update (strips caller-supplied `*_branch_state`
    # keys only a run may write) and Google Docs fbaf81e45 added `_normalize_secret` (validates
    # and minifies the gdocs service account key, token_uri pinned to Google). Reviewed: both
    # only NARROW input; `_validate_config_urls`, `_refuse_unreadable_destination` and
    # `_pin_secret_to_host` still see the same merged config, so credential-host confinement
    # holds.
    "store_part2.py:create": ("fb9a39d5999c3311876c923bd269d5d8bae5a6c60091c46d6789f43dd180733b"),
    # update and patch_config re-pinned by the FIX-config-writers gate (2026-10-14): the
    # Connected projects locales fix 62941d459 added `can_add_languages` and a language plan
    # (`_language_plan`, decided under the lock after `_validate_config_urls`, read-only on
    # copies of old/new config; a refusal raises before commit so nothing is saved) applied
    # after the commit outside the lock (`_apply_language_plan`, whose write-back goes through
    # patch_config again, so it is validated and host-pinned like any other write).
    # Reviewed: `_refuse_unreadable_destination`, `_drop_server_owned`, `_validate_config_urls`
    # and `_pin_secret_to_host` see the same merged config in the same order, secret clearing,
    # audit, customer re-scope (MED-3) and content type are unchanged; the plan touches only
    # target_locales / target_locale / language_set_id, so credential-host confinement holds.
    "store_part2.py:update": ("03c784cd5ee13d1a8d625c89b356e1f36ef3670f406f023f636ae16c391810ff"),
    "store_part2.py:patch_config": (
        "c9742f37039287c6d902486452d77eeff5e15b2c8911bef614c803070c4d6ff0"
    ),
    # Re-pinned by #666 (fix/666-stale-hash): `hashes_remove` / `remove_casefold` pop keys
    # from the named hash map before the delta merge. Reviewed: only entries of the given
    # hashes_key are removed; no host, URL or secret is read or written, and the merged
    # config still passes _validate_config_urls.
    "store_part2.py:patch_config_merge": (
        "168fe180f4d2c7a04caa27f777284a2197c0dac56c612ffd48c2edc848a75c3e"
    ),
    "store_part2.py:patch_config_nested": (
        "311b4808c7ab68e2c604f3572dd8bc2d4b8774035c83e0965fc7585d1bf564d1"
    ),
    "store_part2.py:jira_record_pending": (
        "9fc30426515697f782af1ce04d74ae6f881f11d09a7f5036b15321d587990ea2"
    ),
    # Re-pinned by the shop-1010 gate: #659 (53e5da5c2) added `source_language` as a
    # fallback for the source locale kept by the prune. Reviewed: it only widens which
    # locale keys survive; no host, URL or secret is read or written.
    "store_part2.py:prune_orphaned_config": (
        "a950a8622cec324add5b0a8f81418a8276c0ed2ea28eab77290f90331b4db45a"
    ),
    "wordpress.py:persist_mode": (
        "264e91ebda7bca8456764cf054b67e492882811aca9064bce53b4e76f44690aa"
    ),
    "wordpress.py:_persist_written": (
        "cf9f15981b9452ba3691fb78d8dee4e381486104ab3a58031a86fd7376883ed3"
    ),
}


def main():
    assert "verify_connector_config_writers" in (ROOT / "scripts/pregate.py").read_text(
        encoding="utf-8"
    ), "Pregate must fail if this suite is absent"
    expected = {key: [digest] for key, digest in REVIEWED.items()}
    assert inventory(ROOT) == expected, "Config writer changed: review credential-host confinement"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        directory = root / "app/services/connectors"
        directory.mkdir(parents=True)
        assert inventory(root) != expected, "Missing writers must fail"
        (directory / "new.py").write_bytes(b"def new(row, data):\n    row.config = data\n")
        assert "new.py:new" in inventory(root), "New writers must be found"
        for statement in (
            "row.config['host'] = data",
            "row.config.update(data)",
            "query.values(config=data)",
            "setattr(row, 'config', data)",
        ):
            (directory / "new.py").write_bytes(f"def new(row, data):\n    {statement}\n".encode())
            assert "new.py:new" in inventory(root), statement
    print("CONNECTOR CONFIG WRITERS OK")


if __name__ == "__main__":
    main()
