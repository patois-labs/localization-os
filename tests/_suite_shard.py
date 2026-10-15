"""Deterministic suite sharding for tests/test_verify_suites.py.

LOC_TMS_SUITE_SHARD=i/n (1-based, for example "3/8") keeps only the suites whose
name hashes into bucket i of n. The bucket depends on the suite NAME alone, so
adding or removing a suite never moves the others, and the n shards together
cover every suite exactly once. Unset or empty means no sharding.
"""

import zlib


def parse_shard(spec):
    """Return (index, count) from "i/n", or None when spec is empty. Raises ValueError."""
    spec = (spec or "").strip()
    if not spec:
        return None
    try:
        left, right = spec.split("/", 1)
        index, count = int(left), int(right)
    except ValueError:
        raise ValueError(f"LOC_TMS_SUITE_SHARD must look like 3/8, got {spec!r}") from None
    if count < 1 or not 1 <= index <= count:
        raise ValueError(f"LOC_TMS_SUITE_SHARD index must be 1..{count}, got {spec!r}")
    return index, count


def shard_of(name, count):
    """The 1-based bucket a suite name belongs to."""
    return zlib.crc32(name.encode("utf-8")) % count + 1


def select_shard(suites, spec):
    """Filter (name, path) pairs down to the requested shard; all of them when no spec."""
    parsed = parse_shard(spec)
    if parsed is None:
        return list(suites)
    index, count = parsed
    return [pair for pair in suites if shard_of(pair[0], count) == index]
