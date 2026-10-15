"""Key-file JSON export: collisions are refused in every order, Unicode digits round-trip (#508).

  1. `a::b` then `a` (first_added order) used to overwrite the branch with a leaf.
  2. plural_style=i18next_v4: a plural `items` and a plain `items_one` both became
     `items_one`; the last silently won.
  3. Flat mode and nested mode: `a:::0` (object key "0") and `a::0` (array index) both
     wrote `a::0` / collapsed to one member.
  4. A superscript or circled digit segment was escaped by flatten (str.isdigit) but never
     un-escaped (the regex is ASCII-digit-only for `\\d` on category No), so a re-export
     gained a stray `:` and a nested export of `f::<digit>` raised ValueError (a 500).

Hermetic + offline. Final line: "KEYS ROUNDTRIP 508 OK".
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_keys508_")

from app.services import key_files as kf


def _rec(name, value="v", **extra):
    rec = {"name": name, "value": value, "is_plural": False}
    rec.update(extra)
    return rec


def _refused(fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except kf.KeyCollisionError:
        return True
    return False


def test_leaf_and_branch_collide_in_either_order():
    for order in (("a::b", "a"), ("a", "a::b")):
        recs = [_rec(n) for n in order]
        assert _refused(kf.render_json_keys, recs), ("nested", order)
    print("  [508-1] a leaf and a branch on one path are refused in both orders OK")


def test_i18next_suffix_collision_is_refused():
    plural = {
        "name": "items",
        "is_plural": True,
        "plural_values": {"one": "item", "other": "items"},
    }
    plain = _rec("items_one", "single")
    for recs in ([plural, plain], [plain, plural]):
        assert _refused(kf.render_json_keys, recs, plural_style=kf.PLURAL_I18NEXT), recs
    # the object style has no suffixing, so the same pair stays valid there
    out = json.loads(kf.render_json_keys([plural, plain]).decode())
    assert out["items_one"] == "single" and out["items"]["other"] == "items", out
    print("  [508-2] a suffixed i18next name colliding with a plain key is refused OK")


def test_forced_numeric_and_index_collide():
    recs = [_rec("a:::0", "object key zero"), _rec("a::0", "array index zero")]
    for nested in (True, False):
        assert _refused(kf.render_json_keys, recs, nested=nested), nested
        assert _refused(kf.render_json_keys, list(reversed(recs)), nested=nested), nested
    print("  [508-3] ':::0' beside '::0' is refused in nested and flat mode OK")


def test_unicode_digit_segments_round_trip():
    for seg in ("²", "①", "٣"):
        body = {"a": {seg: "x"}, "f": {seg: "y"}}
        recs = kf.parse_json_keys(json.dumps(body).encode("utf-8"))
        out = json.loads(kf.render_json_keys(recs).decode("utf-8"))
        assert out == body, (seg, out)
        flat = json.loads(kf.render_json_keys(recs, nested=False).decode("utf-8"))
        assert flat == {"a::" + seg: "x", "f::" + seg: "y"}, (seg, flat)
    # the nested export of a bare `f::<digit>` key used to raise ValueError
    out = kf.unflatten_json([("f::²", "x")])
    assert out == {"f": {"²": "x"}}, out
    # ASCII numeric object keys keep their escape
    recs = kf.parse_json_keys(b'{"0": "zero", "1": "one"}')
    assert json.loads(kf.render_json_keys(recs).decode()) == {"0": "zero", "1": "one"}
    print("  [508-4] superscript, circled and Arabic-Indic digit segments round-trip OK")


def main():
    test_leaf_and_branch_collide_in_either_order()
    test_i18next_suffix_collision_is_refused()
    test_forced_numeric_and_index_collide()
    test_unicode_digit_segments_round_trip()
    print("KEYS ROUNDTRIP 508 OK")


if __name__ == "__main__":
    main()
