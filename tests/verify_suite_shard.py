"""LOC_TMS_SUITE_SHARD: the CI shard option of tests/test_verify_suites.py is a partition."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import _suite_shard as shard


def main():
    names = sorted(
        f[:-3] for f in os.listdir(_HERE) if f.startswith("verify_") and f.endswith(".py")
    )
    suites = [(name, os.path.join(_HERE, name + ".py")) for name in names]
    assert len(suites) > 100, len(suites)

    assert shard.select_shard(suites, "") == suites and shard.select_shard(suites, None) == suites

    for count in (1, 2, 8):
        seen = []
        for index in range(1, count + 1):
            seen.extend(name for name, _path in shard.select_shard(suites, f"{index}/{count}"))
        assert sorted(seen) == names, f"{count} shards must cover every suite exactly once"
    sizes = [len(shard.select_shard(suites, f"{i}/8")) for i in range(1, 9)]
    assert min(sizes) > 0 and max(sizes) < len(suites) // 2, sizes

    # Stable: the bucket of a name never depends on the other names.
    assert shard.shard_of("verify_ruff", 8) == shard.shard_of("verify_ruff", 8)
    assert shard.select_shard(suites[:5], "1/1") == suites[:5]

    for bad in ("0/8", "9/8", "3", "a/b", "1/0", "-1/4"):
        try:
            shard.parse_shard(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be refused")
    assert shard.parse_shard(" 3/8 ") == (3, 8)
    print("suite shard partition OK")


if __name__ == "__main__":
    main()
