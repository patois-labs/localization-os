"""One id-coercion law for every surface (web, CLI, /api).

A request-supplied id (a form field, query param, JSON value, or path token) can
be a malformed shape that only fails LATER, as an unhandled 500:

  * `int()` rejects a Unicode digit such as the superscript '2' (U+00B2 ->
    ValueError). `str.isdigit()` is True for it, so an `isdigit()`-then-`int()`
    gate lets it straight through to the `int()` that then raises.
  * the SQLite binder raises a raw OverflowError deep in the driver for any value
    wider than signed 64 bits, so a huge digit-string (a typo, a forged POST, a
    fuzzer) crashes the eventual `s.get(...)` / DB filter.

`int()` alone is NOT a validation: it happily returns 10**40. `coerce_id` is the
single funnel every surface routes through, so the bound lives HERE -- not once
per route, where copies drift. It promotes the "one bound, both surfaces" note
that used to live only in app/services/project_sources.py to the whole app.

2**31 is the ceiling: it comfortably exceeds any real row count and keeps ids
inside a signed 32-bit int on every backend we target. Booleans are rejected
(a stray True must never read as id 1).

Documented in: docs/architecture/data-and-migrations.md
"""

_MAX_ID = 2**31

# The single length cap for every user-facing NAME (customer / content-type /
# project / workflow). Names are display strings — even a very long real name is a
# few hundred chars — so a fixed, reversible bound keeps a 100k-char paste (typo,
# fuzzer, hostile POST) from blowing out every list surface that renders the name.
NAME_MAX_LEN = 512


class InvalidNameError(ValueError):
    """Raised by clean_name for a non-string or over-long name. A ValueError so a
    caller that already funnels ValueError -> 400 keeps working; the /api + web
    surfaces map it to a clean 422/error page rather than str()-coercing a list into
    its repr (BUG-A13) or storing a 100k-char name (BUG-A12)."""


def clean_name(value, *, field: str = "name", max_len: int = NAME_MAX_LEN) -> str:
    """Normalize + validate a user-facing name at the shared create/update seam.

    * None -> "" (the blank case every create seam already handles by no-op/redirect;
      this preserves that path byte-identically).
    * a NON-STRING JSON value (int / list / dict / bool) is REFUSED (InvalidNameError),
      never str()-coerced into its Python repr (BUG-A13) — the surfaces pass the RAW
      body value so the type is still visible here.
    * a string is trimmed, then REFUSED past `max_len` (BUG-A12).

    Returns the trimmed string for a valid name. The single funnel so the four name
    seams cannot drift, mirroring coerce_id's one-bound-both-surfaces design."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str):
        raise InvalidNameError(f"{field} must be text.")
    value = value.strip()
    if len(value) > max_len:
        raise InvalidNameError(f"{field} must be at most {max_len} characters (got {len(value)}).")
    return value


def coerce_id(value) -> int | None:
    """A positive, in-range row id, or None for anything else (blank, None, a
    boolean, a non-numeric or Unicode-digit string, zero/negative, or an
    absurdly large value). NEVER raises: a malformed id resolves to None so the
    caller takes its existing not-found / unset / stale-id path instead of a 500."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    else:
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            return None
    return n if 0 < n < _MAX_ID else None


def parse_finite_number(value, *, field: str = "value", integer: bool = False):
    """A FINITE number parsed from a form scalar or JSON value, or ValueError (never
    OverflowError, never inf/nan). GitHub #502: `int(float("inf"))` raises OverflowError and
    `float("nan")` slips through to a guard that never fires, both uncaught by the callers that
    only handle ValueError/TypeError. One funnel turns every such shape into the SAME ValueError
    a caller already maps to its normal error. `integer=True` truncates toward zero (the shape
    the old `int(float(x))` gave) and still refuses a value wider than a signed 64-bit int, so a
    later DB bind cannot fail. Booleans are refused."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number.")
    try:
        n = float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be a number (got {value!r}).") from None
    if n != n or n in (float("inf"), float("-inf")):
        raise ValueError(f"{field} must be a finite number (got {value!r}).")
    if integer:
        n = int(n)
        if not -(2**63) <= n < 2**63:
            raise ValueError(f"{field} is out of range (got {value!r}).")
    return n
