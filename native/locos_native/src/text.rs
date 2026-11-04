//! Python-exact text primitives over CODE POINTS (u32), the unit Python indexes strings by:
//! whitespace, `\w`, `\d`, strip, and the inline-fidelity token scanner (`⟦/?[a-z]?\d+⟧`).
//! Every class comes from tables.rs, measured from Python `re`; Rust's own Unicode classes are
//! never used. Lone surrogates are ordinary values here (never space, word or digit, as in
//! Python), so no input needs a UTF-8 round trip.

use crate::tables::{DIGIT, SPACE, WORD};

#[inline]
fn in_ranges(c: u32, ranges: &[(u32, u32)]) -> bool {
    ranges
        .binary_search_by(|&(lo, hi)| {
            if hi < c {
                std::cmp::Ordering::Less
            } else if lo > c {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

const LATIN1_SPACE: [bool; 256] = {
    let mut t = [false; 256];
    let mut c = 0;
    while c < 256 {
        t[c] = matches!(c, 0x09..=0x0D | 0x1C..=0x20 | 0x85 | 0xA0);
        c += 1;
    }
    t
};

/// re `\s` == str.isspace(). The non-ASCII members are U+0085, U+00A0, U+1680 and some of
/// U+2000..U+3000 (tables::SPACE; a unit test pins this shortcut to the table).
#[inline]
pub fn is_space(c: u32) -> bool {
    if c < 0x100 {
        LATIN1_SPACE[c as usize]
    } else if c < 0x1680 {
        c == 0x85 || c == 0xA0
    } else if c > 0x3000 {
        false
    } else {
        in_ranges(c, SPACE)
    }
}

/// re `\w`.
#[inline]
pub fn is_word(c: u32) -> bool {
    if c < 0x80 {
        matches!(c, 0x30..=0x39 | 0x41..=0x5A | 0x61..=0x7A | 0x5F)
    } else {
        in_ranges(c, WORD)
    }
}

/// re `\d`.
#[inline]
pub fn is_digit(c: u32) -> bool {
    if c < 0x80 {
        (0x30..=0x39).contains(&c)
    } else {
        in_ranges(c, DIGIT)
    }
}

/// str.strip() of t[a..b], as a range.
pub fn strip(t: &[u32], mut a: usize, mut b: usize) -> (usize, usize) {
    while a < b && is_space(t[a]) {
        a += 1;
    }
    while b > a && is_space(t[b - 1]) {
        b -= 1;
    }
    (a, b)
}

pub const OPEN: u32 = 0x27E6; // ⟦
pub const CLOSE: u32 = 0x27E7; // ⟧

/// One well-formed inline-fidelity token: indices into the scanned text.
#[derive(Clone, Copy, Debug)]
pub struct Tok {
    pub start: usize,
    pub end: usize,
    pub close: bool,
    /// The optional `[a-z]` kind letter.
    pub kind: Option<u32>,
    /// The `\d+` id (the pairing key), as a range.
    pub digits: (usize, usize),
}

impl Tok {
    /// The whole token, `⟦...⟧`.
    #[inline]
    pub fn text<'a>(&self, t: &'a [u32]) -> &'a [u32] {
        &t[self.start..self.end]
    }
    /// Between the brackets (`/g1`, `r2`).
    #[inline]
    pub fn inner<'a>(&self, t: &'a [u32]) -> &'a [u32] {
        &t[self.start + 1..self.end - 1]
    }
    #[inline]
    pub fn id<'a>(&self, t: &'a [u32]) -> &'a [u32] {
        &t[self.digits.0..self.digits.1]
    }
    /// segment.is_point_kind(kind): `kind in frozenset("xp")` ("" is not a point kind).
    #[inline]
    pub fn is_point(&self) -> bool {
        matches!(self.kind, Some(0x78) | Some(0x70))
    }
    /// segment.is_free_token: `tok[1:2] == "p"` (a close token has "/" there).
    #[inline]
    pub fn is_free(&self) -> bool {
        !self.close && self.kind == Some(0x70)
    }
}

/// `_PLACEHOLDER_RE.finditer(t)`: non-overlapping, left to right. The pattern has no real
/// backtracking choice (`/?` and `[a-z]?` cannot be skipped and still let `\d+` match a `/` or
/// a letter; greedy `\d+` must end at `⟧`), so a direct scan is exact.
pub fn find_tokens(t: &[u32]) -> Vec<Tok> {
    let mut out = Vec::new();
    let n = t.len();
    let mut i = 0;
    while i < n {
        if t[i] != OPEN {
            i += 1;
            continue;
        }
        let start = i;
        let mut j = i + 1;
        let close = j < n && t[j] == 0x2F;
        if close {
            j += 1;
        }
        let mut kind = None;
        if j < n && (0x61..=0x7A).contains(&t[j]) {
            kind = Some(t[j]);
            j += 1;
        }
        let ds = j;
        while j < n && is_digit(t[j]) {
            j += 1;
        }
        if j > ds && j < n && t[j] == CLOSE {
            out.push(Tok {
                start,
                end: j + 1,
                close,
                kind,
                digits: (ds, j),
            });
            i = j + 1;
        } else {
            i = start + 1;
        }
    }
    out
}

/// Code points to a String; None if one is a lone surrogate.
pub fn to_string(t: &[u32]) -> Option<String> {
    t.iter().map(|&c| char::from_u32(c)).collect()
}

/// Encode a &str as code points (tests and constant patterns).
#[cfg(test)]
pub fn cps(s: &str) -> Vec<u32> {
    s.chars().map(|c| c as u32).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn space_shortcut_matches_table() {
        for c in 0..0x110000u32 {
            assert_eq!(is_space(c), in_ranges(c, SPACE), "U+{c:04X}");
        }
        for c in 0..0x80u32 {
            assert_eq!(is_word(c), in_ranges(c, WORD), "U+{c:04X}");
            assert_eq!(is_digit(c), in_ranges(c, DIGIT), "U+{c:04X}");
        }
    }

    #[test]
    fn tokens() {
        let t = cps(
            "x \u{27E6}g1\u{27E7}a\u{27E6}/g1\u{27E7} \u{27E6}x\u{27E7} \u{27E6}\u{27E6}12\u{27E7}",
        );
        let names: Vec<String> = find_tokens(&t)
            .iter()
            .map(|k| to_string(k.inner(&t)).unwrap())
            .collect();
        assert_eq!(names, ["g1", "/g1", "12"]);
    }
}
