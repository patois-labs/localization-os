"""Shared checks for the key-value resource filter suites (file-format Batch 1):
verify_properties_filter, verify_strings_filter, verify_ini_filter, verify_resjson_filter.

Import AFTER tests._bootstrap.init_env (it imports app)."""

from app import config
from app.filters import get_filter
from app.filters.base import InputLimitError


def units(fmt: str, data: bytes) -> list[str]:
    return [u.text for u in get_filter(fmt).parse(data).units]


def assert_identity(fmt: str, data: bytes) -> None:
    """Build with no translations and with every unit echoed: byte-identical both ways."""
    filt = get_filter(fmt)
    texts = units(fmt, data)
    assert filt.build(data, {}) == data, (fmt, "empty build must be byte-identical")
    echo = {str(i): t for i, t in enumerate(texts)}
    out = filt.build(data, echo)
    assert out == data, (fmt, "echo build must be byte-identical", out, data)


def assert_round_trip(fmt: str, data: bytes, mapping: dict, expected: bytes) -> bytes:
    """Build with `mapping`, compare to the exact expected bytes, re-parse the result and
    check every translated unit reads back as the translation."""
    filt = get_filter(fmt)
    out = filt.build(data, mapping)
    assert out == expected, (fmt, "rebuilt bytes", out, expected)
    back = units(fmt, out)
    before = units(fmt, data)
    assert len(back) == len(before), (fmt, back, before)
    for k, v in mapping.items():
        assert back[int(k)] == v.strip(), (fmt, k, back[int(k)], v)
    return out


def assert_refused(fmt: str, data: bytes, exc=(ValueError,), needle: str = "") -> None:
    try:
        get_filter(fmt).parse(data)
    except exc as e:
        assert needle.lower() in str(e).lower(), (fmt, needle, str(e))
        return
    raise AssertionError((fmt, "must be refused", data[:80]))


def assert_segment_cap(fmt: str, one_entry, n: int = 30) -> None:
    """Lower MAX_DOCUMENT_SEGMENTS and feed more entries than it allows: refused."""
    saved = config.MAX_DOCUMENT_SEGMENTS
    config.MAX_DOCUMENT_SEGMENTS = 10
    try:
        data = b"".join(one_entry(i) for i in range(n))
        try:
            get_filter(fmt).parse(data)
        except InputLimitError:
            return
        raise AssertionError((fmt, "over-cap file must be refused"))
    finally:
        config.MAX_DOCUMENT_SEGMENTS = saved


def assert_placeholder_dialect(source: str, good: str, bad: str) -> None:
    """The shared placeholder gate must see this format's placeholders."""
    from app.services.quality import placeholder_violation

    assert placeholder_violation(source, good) == "", (source, good)
    assert placeholder_violation(source, bad) != "", (source, bad)
