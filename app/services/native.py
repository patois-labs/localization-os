"""Optional native (Rust) hot paths: the loader, with Python as the reference and the fallback.

`locos_native` is a PyO3 extension built from `native/locos_native` (ADR 0091). It is OPTIONAL: an
install without it runs the Python functions exactly as before, byte for byte. A module that owns a
hot path keeps its Python function as the reference (`<name>_py`) and rebinds the public name at
the bottom of the module:

    normalize_py = normalize
    normalize = native.bind("normalize", normalize_py)

`bind` returns the native function only when ALL of these hold, else the Python function:

  * `locos_native` imports and `LOCOS_NATIVE` is not "0" / "off" (the kill switch);
  * the module's Unicode tables were generated from the same `unicodedata` version this Python runs
    (Python `re` character classes are measured into the crate, never Rust's own);
  * the fingerprint of the Python reference (its source, and every helper and pattern it reads, as
    listed in SPECS) equals the stamp compiled into the module. Editing a reference without porting
    the edit to Rust therefore switches that one native function OFF (with a warning) instead of
    letting the two drift.

A native function takes a fast path for plain `str` / None arguments and calls the registered
Python reference for anything else (str subclasses, lone surrogates, other types), so even its
exceptions are the Python ones. tests/verify_native_parity.py proves byte parity on 20,000
generated inputs per function when the module is present and skips cleanly when it is absent.

Documented in: docs/architecture/native-module.md
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import re
import sys
import unicodedata

log = logging.getLogger(__name__)

ENV_SWITCH = "LOCOS_NATIVE"

# name -> ((module, (attribute, ...)), ...): the Python reference of each native function and
# every helper, constant and pattern it reads. The fingerprint covers all of them, so a change to
# any one disables the native function until it is ported and re-stamped (gen_tables.py --stamps).
SPECS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "normalize": (("app.services.tm_common", ("normalize_py",)),),
    "hash_text": (("app.services.tm_common", ("normalize_py", "hash_text_py")),),
    "tag_damage": (
        (
            "app.services.quality_common",
            ("tag_damage_py", "_residue_excerpt", "_RESIDUE_EXCERPT_CAP"),
        ),
        ("app.services.ai_tagging", ("has_residual_sentinel", "_GEN_RESIDUAL_RE")),
        (
            "app.services.segment",
            (
                "_PLACEHOLDER_RE",
                "MARKER_CHARS",
                "POINT_KINDS",
                "FREE_POINT_KIND",
                "find_placeholders",
                "strip_placeholders",
                "is_point_kind",
                "is_free_token",
                "order_significant",
                "same_token_order",
                "placeholders_well_formed",
                "paired_span_bodies",
                "marker_residue",
                "has_marker_chars",
            ),
        ),
    ),
    "split_sentences": (
        (
            "app.services.segment",
            (
                "_legacy_split_py",
                "_SENT_SPLIT",
                "_CJK_SPLIT",
                "_ABBREV_WINDOW",
                "_ABBREV_RULES",
                "_ABBREV_COMPILED",
                "_abbrev_holds",
                "_PLACEHOLDER_RE",
                "_protected_spans",
                "_in_protected_span",
            ),
        ),
    ),
}


def _switched_off() -> bool:
    return os.environ.get(ENV_SWITCH, "").strip().lower() in ("0", "off", "false", "no")


def _load():
    if _switched_off():
        return None, f"disabled by {ENV_SWITCH}"
    try:
        import locos_native
    except ImportError:
        return None, "not installed"
    except Exception as e:  # a broken build must never stop the app
        log.warning("locos_native failed to import (%s); using the Python functions", e)
        return None, f"import failed: {type(e).__name__}"
    have = getattr(locos_native, "UNIDATA_VERSION", None)
    if have != unicodedata.unidata_version:
        log.warning(
            "locos_native tables are Unicode %s but this Python is %s; using the Python functions",
            have,
            unicodedata.unidata_version,
        )
        return None, f"unicode {have} != {unicodedata.unidata_version}"
    return locos_native, "loaded"


_MODULE, _REASON = _load()
_BOUND: dict[str, str] = {}


def _render(obj) -> str:
    if inspect.isfunction(obj):
        return inspect.getsource(obj)
    if isinstance(obj, re.Pattern):
        return f"re({obj.pattern!r}, {int(obj.flags)})"
    if isinstance(obj, (set, frozenset)):
        return "set(" + repr(sorted(obj)) + ")"
    if isinstance(obj, (tuple, list)):  # item by item: repr() truncates long compiled patterns
        return "(" + ", ".join(_render(x) for x in obj) + ")"
    return repr(obj)


def fingerprint(name: str) -> str:
    """sha256 over the Python reference of native function `name` (see SPECS)."""
    h = hashlib.sha256()
    for modname, attrs in SPECS[name]:
        mod = sys.modules[modname]
        for attr in attrs:
            h.update(f"{modname}.{attr}\n".encode())
            h.update(_render(getattr(mod, attr)).encode("utf-8"))
            h.update(b"\n\x00")
    return h.hexdigest()


def bind(name: str, py_fn):
    """The native `name` when it is available and stamped for this exact reference, else `py_fn`."""
    if _MODULE is None:
        _BOUND[name] = "python: " + _REASON
        return py_fn
    stamp = dict(getattr(_MODULE, "STAMPS", ())).get(name)
    try:
        fp = fingerprint(name)
    except Exception as e:  # source unavailable (frozen app, stripped .py): stay on Python
        fp = f"error: {type(e).__name__}"
    if stamp is None or fp != stamp:
        log.warning(
            "locos_native.%s is stamped for a different Python reference; using the Python one "
            "(port the change to native/locos_native, then re-stamp)",
            name,
        )
        _BOUND[name] = "python: stale stamp"
        return py_fn
    _MODULE._set_reference(name, py_fn)
    _BOUND[name] = "native"
    return getattr(_MODULE, name)


def status() -> dict:
    """What the loader decided, for diagnostics and the parity suite."""
    return {
        "module": getattr(_MODULE, "__version__", None) if _MODULE is not None else None,
        "reason": _REASON,
        "bound": dict(_BOUND),
    }


def module():
    """The loaded `locos_native` module, or None."""
    return _MODULE
