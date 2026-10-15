"""Synthetic connector used by the SRP executor fixtures.

Documented in: docs/architecture/sample.md
"""

from __future__ import annotations

from functools import cache
from math import sqrt

LIMIT = 7


def keep_read():
    """Read a constant which remains in the shim."""
    return LIMIT


def other_read():
    """A second reader of the constant."""
    return LIMIT + 1


# ================= Leaf calculations =================
# Preserve this section note and its spacing.
@cache
def alpha(value: int) -> float:
    """Crème brûlée — retain every UTF-8 byte."""
    return sqrt(value)


class Token:
    """Small top-level class."""

    def value(self):
        return alpha(9)


# ================= Serialization =================
def beta(value):
    """The local import travels with this function."""
    import json

    return json.dumps(alpha(value))


def register():
    """Keep registration intact."""
    return beta(LIMIT)


def long_helper():
    """Long documented operation for compact inventory testing.
    Fixture documentation line 000.
    Fixture documentation line 001.
    Fixture documentation line 002.
    Fixture documentation line 003.
    Fixture documentation line 004.
    Fixture documentation line 005.
    Fixture documentation line 006.
    Fixture documentation line 007.
    Fixture documentation line 008.
    Fixture documentation line 009.
    Fixture documentation line 010.
    Fixture documentation line 011.
    Fixture documentation line 012.
    Fixture documentation line 013.
    Fixture documentation line 014.
    Fixture documentation line 015.
    Fixture documentation line 016.
    Fixture documentation line 017.
    Fixture documentation line 018.
    Fixture documentation line 019.
    Fixture documentation line 020.
    Fixture documentation line 021.
    Fixture documentation line 022.
    Fixture documentation line 023.
    Fixture documentation line 024.
    Fixture documentation line 025.
    Fixture documentation line 026.
    Fixture documentation line 027.
    Fixture documentation line 028.
    Fixture documentation line 029.
    Fixture documentation line 030.
    Fixture documentation line 031.
    Fixture documentation line 032.
    Fixture documentation line 033.
    Fixture documentation line 034.
    Fixture documentation line 035.
    Fixture documentation line 036.
    Fixture documentation line 037.
    Fixture documentation line 038.
    Fixture documentation line 039.
    Fixture documentation line 040.
    Fixture documentation line 041.
    Fixture documentation line 042.
    Fixture documentation line 043.
    Fixture documentation line 044.
    Fixture documentation line 045.
    Fixture documentation line 046.
    Fixture documentation line 047.
    Fixture documentation line 048.
    Fixture documentation line 049.
    Fixture documentation line 050.
    Fixture documentation line 051.
    Fixture documentation line 052.
    Fixture documentation line 053.
    Fixture documentation line 054.
    Fixture documentation line 055.
    Fixture documentation line 056.
    Fixture documentation line 057.
    Fixture documentation line 058.
    Fixture documentation line 059.
    Fixture documentation line 060.
    Fixture documentation line 061.
    Fixture documentation line 062.
    Fixture documentation line 063.
    Fixture documentation line 064.
    Fixture documentation line 065.
    Fixture documentation line 066.
    Fixture documentation line 067.
    Fixture documentation line 068.
    Fixture documentation line 069.
    Fixture documentation line 070.
    Fixture documentation line 071.
    Fixture documentation line 072.
    Fixture documentation line 073.
    Fixture documentation line 074.
    Fixture documentation line 075.
    Fixture documentation line 076.
    Fixture documentation line 077.
    Fixture documentation line 078.
    Fixture documentation line 079.
    Fixture documentation line 080.
    Fixture documentation line 081.
    Fixture documentation line 082.
    Fixture documentation line 083.
    Fixture documentation line 084.
    Fixture documentation line 085.
    Fixture documentation line 086.
    Fixture documentation line 087.
    Fixture documentation line 088.
    Fixture documentation line 089.
    Fixture documentation line 090.
    Fixture documentation line 091.
    Fixture documentation line 092.
    Fixture documentation line 093.
    Fixture documentation line 094.
    Fixture documentation line 095.
    Fixture documentation line 096.
    Fixture documentation line 097.
    Fixture documentation line 098.
    Fixture documentation line 099.
    Fixture documentation line 100.
    Fixture documentation line 101.
    Fixture documentation line 102.
    Fixture documentation line 103.
    Fixture documentation line 104.
    Fixture documentation line 105.
    Fixture documentation line 106.
    Fixture documentation line 107.
    Fixture documentation line 108.
    Fixture documentation line 109.
    Fixture documentation line 110.
    Fixture documentation line 111.
    Fixture documentation line 112.
    Fixture documentation line 113.
    Fixture documentation line 114.
    Fixture documentation line 115.
    Fixture documentation line 116.
    Fixture documentation line 117.
    Fixture documentation line 118.
    Fixture documentation line 119.
    Fixture documentation line 120.
    Fixture documentation line 121.
    Fixture documentation line 122.
    Fixture documentation line 123.
    Fixture documentation line 124.
    Fixture documentation line 125.
    Fixture documentation line 126.
    Fixture documentation line 127.
    Fixture documentation line 128.
    Fixture documentation line 129.
    Fixture documentation line 130.
    Fixture documentation line 131.
    Fixture documentation line 132.
    Fixture documentation line 133.
    Fixture documentation line 134.
    Fixture documentation line 135.
    Fixture documentation line 136.
    Fixture documentation line 137.
    Fixture documentation line 138.
    Fixture documentation line 139.
    Fixture documentation line 140.
    Fixture documentation line 141.
    Fixture documentation line 142.
    Fixture documentation line 143.
    Fixture documentation line 144.
    Fixture documentation line 145.
    Fixture documentation line 146.
    Fixture documentation line 147.
    Fixture documentation line 148.
    Fixture documentation line 149.
    Fixture documentation line 150.
    Fixture documentation line 151.
    Fixture documentation line 152.
    Fixture documentation line 153.
    Fixture documentation line 154.
    Fixture documentation line 155.
    Fixture documentation line 156.
    Fixture documentation line 157.
    Fixture documentation line 158.
    Fixture documentation line 159.
    Fixture documentation line 160.
    Fixture documentation line 161.
    Fixture documentation line 162.
    Fixture documentation line 163.
    Fixture documentation line 164.
    Fixture documentation line 165.
    Fixture documentation line 166.
    Fixture documentation line 167.
    Fixture documentation line 168.
    Fixture documentation line 169.
    Fixture documentation line 170.
    Fixture documentation line 171.
    Fixture documentation line 172.
    Fixture documentation line 173.
    Fixture documentation line 174.
    Fixture documentation line 175.
    Fixture documentation line 176.
    Fixture documentation line 177.
    Fixture documentation line 178.
    Fixture documentation line 179.
    Fixture documentation line 180.
    Fixture documentation line 181.
    Fixture documentation line 182.
    Fixture documentation line 183.
    Fixture documentation line 184.
    Fixture documentation line 185.
    Fixture documentation line 186.
    Fixture documentation line 187.
    Fixture documentation line 188.
    Fixture documentation line 189.
    Fixture documentation line 190.
    Fixture documentation line 191.
    Fixture documentation line 192.
    Fixture documentation line 193.
    Fixture documentation line 194.
    Fixture documentation line 195.
    Fixture documentation line 196.
    Fixture documentation line 197.
    Fixture documentation line 198.
    Fixture documentation line 199.
    Fixture documentation line 200.
    Fixture documentation line 201.
    Fixture documentation line 202.
    Fixture documentation line 203.
    Fixture documentation line 204.
    Fixture documentation line 205.
    Fixture documentation line 206.
    Fixture documentation line 207.
    Fixture documentation line 208.
    Fixture documentation line 209.
    Fixture documentation line 210.
    Fixture documentation line 211.
    Fixture documentation line 212.
    Fixture documentation line 213.
    Fixture documentation line 214.
    Fixture documentation line 215.
    Fixture documentation line 216.
    Fixture documentation line 217.
    Fixture documentation line 218.
    Fixture documentation line 219.
    Fixture documentation line 220.
    Fixture documentation line 221.
    Fixture documentation line 222.
    Fixture documentation line 223.
    Fixture documentation line 224.
    Fixture documentation line 225.
    Fixture documentation line 226.
    Fixture documentation line 227.
    Fixture documentation line 228.
    Fixture documentation line 229.
    Fixture documentation line 230.
    Fixture documentation line 231.
    Fixture documentation line 232.
    Fixture documentation line 233.
    Fixture documentation line 234.
    Fixture documentation line 235.
    Fixture documentation line 236.
    Fixture documentation line 237.
    Fixture documentation line 238.
    Fixture documentation line 239.
    Fixture documentation line 240.
    Fixture documentation line 241.
    Fixture documentation line 242.
    Fixture documentation line 243.
    Fixture documentation line 244.
    """
    return 1
