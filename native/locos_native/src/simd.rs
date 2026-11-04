//! `" ".join(s.split())` support: find the next code unit that `split`/`join` would change.
//!
//! A unit is BAD when it is whitespace (tables::SPACE, Python's str.isspace) and either is not a
//! plain U+0020 or follows another whitespace unit (or starts the string: `prev_space` starts true).
//! Everything before the first bad unit is already in joined form, so callers copy those runs in
//! bulk. On x86-64 the scan looks at 16 bytes at a time with SSE2 (part of the x86-64 baseline, so
//! no runtime detection): a chunk whose only whitespace is plain single spaces is skipped whole,
//! and any chunk holding a rarer candidate (U+0085, U+00A0, U+1680..U+3000, controls) is rescanned
//! exactly by the scalar rule. Other targets use the scalar rule throughout.

use crate::text::is_space;

/// A storage width of a CPython str (PEP 393).
pub trait Unit: Copy + Into<u32> + Eq {
    const SPACE: Self;
    /// Index of the first bad unit at or after `i`, or `u.len()`.
    fn next_bad(u: &[Self], i: usize, prev_space: bool) -> usize;
}

/// The exact rule, one unit at a time.
#[inline]
pub fn next_bad_scalar<T: Copy + Into<u32>>(u: &[T], mut i: usize, mut prev_space: bool) -> usize {
    while i < u.len() {
        let c: u32 = u[i].into();
        let s = is_space(c);
        if s && (c != 0x20 || prev_space) {
            return i;
        }
        prev_space = s;
        i += 1;
    }
    u.len()
}

#[cfg(target_arch = "x86_64")]
mod sse2 {
    use std::arch::x86_64::*;

    /// One chunk's verdict: Some(bitmask of plain spaces) when the chunk holds no rarer
    /// whitespace candidate, None when it must be rescanned by the scalar rule.
    pub type Chunk = Option<u32>;

    #[inline(always)]
    pub unsafe fn chunk_u8(p: *const u8) -> Chunk {
        let c = _mm_loadu_si128(p as *const __m128i);
        let le = |k: i8| {
            let kv = _mm_set1_epi8(k);
            _mm_cmpeq_epi8(_mm_max_epu8(c, kv), kv) // c <= k (unsigned)
        };
        let is20 = _mm_cmpeq_epi8(c, _mm_set1_epi8(0x20));
        let maybe = _mm_or_si128(
            le(0x20),
            _mm_or_si128(
                _mm_cmpeq_epi8(c, _mm_set1_epi8(0x85u8 as i8)),
                _mm_cmpeq_epi8(c, _mm_set1_epi8(0xA0u8 as i8)),
            ),
        );
        if _mm_movemask_epi8(_mm_andnot_si128(is20, maybe)) != 0 {
            return None;
        }
        Some(_mm_movemask_epi8(is20) as u32)
    }

    #[inline(always)]
    pub unsafe fn chunk_u16(p: *const u16) -> Chunk {
        let c = _mm_loadu_si128(p as *const __m128i);
        let zero = _mm_setzero_si128();
        let le = |x: __m128i, k: i16| _mm_cmpeq_epi16(_mm_subs_epu16(x, _mm_set1_epi16(k)), zero);
        let is20 = _mm_cmpeq_epi16(c, _mm_set1_epi16(0x20));
        let block = le(_mm_sub_epi16(c, _mm_set1_epi16(0x1680)), 0x3000 - 0x1680);
        let maybe = _mm_or_si128(
            _mm_or_si128(le(c, 0x20), block),
            _mm_or_si128(
                _mm_cmpeq_epi16(c, _mm_set1_epi16(0x85)),
                _mm_cmpeq_epi16(c, _mm_set1_epi16(0xA0)),
            ),
        );
        if _mm_movemask_epi8(_mm_andnot_si128(is20, maybe)) != 0 {
            return None;
        }
        Some(_mm_movemask_epi8(_mm_packs_epi16(is20, zero)) as u32)
    }

    #[inline(always)]
    pub unsafe fn chunk_u32(p: *const u32) -> Chunk {
        // Code points are below 0x110000, so signed 32-bit compares are exact.
        let c = _mm_loadu_si128(p as *const __m128i);
        let lt = |k: i32| _mm_cmplt_epi32(c, _mm_set1_epi32(k));
        let is20 = _mm_cmpeq_epi32(c, _mm_set1_epi32(0x20));
        let block = _mm_andnot_si128(lt(0x1680), lt(0x3001));
        let maybe = _mm_or_si128(
            _mm_or_si128(lt(0x21), block),
            _mm_or_si128(
                _mm_cmpeq_epi32(c, _mm_set1_epi32(0x85)),
                _mm_cmpeq_epi32(c, _mm_set1_epi32(0xA0)),
            ),
        );
        if _mm_movemask_epi8(_mm_andnot_si128(is20, maybe)) != 0 {
            return None;
        }
        Some(_mm_movemask_ps(_mm_castsi128_ps(is20)) as u32)
    }
}

/// Chunked scan shared by the three widths: `LANES` units per 16-byte chunk.
#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn next_bad_chunked<T: Copy + Into<u32>, const LANES: usize>(
    u: &[T],
    mut i: usize,
    mut prev_space: bool,
    chunk: unsafe fn(*const T) -> sse2::Chunk,
) -> usize {
    let full: u32 = if LANES == 32 {
        u32::MAX
    } else {
        (1u32 << LANES) - 1
    };
    while i + LANES <= u.len() {
        // SAFETY: i + LANES <= len, so the 16-byte unaligned load stays inside the slice.
        match unsafe { chunk(u.as_ptr().add(i)) } {
            Some(spaces) => {
                let bad = spaces & ((spaces << 1) | prev_space as u32) & full;
                if bad != 0 {
                    return i + bad.trailing_zeros() as usize;
                }
                prev_space = spaces & (1 << (LANES - 1)) != 0;
            }
            None => {
                let end = next_bad_scalar(&u[..i + LANES], i, prev_space);
                if end < i + LANES {
                    return end;
                }
                prev_space = is_space(u[i + LANES - 1].into());
            }
        }
        i += LANES;
    }
    next_bad_scalar(u, i, prev_space)
}

impl Unit for u8 {
    const SPACE: Self = 0x20;
    #[inline]
    fn next_bad(u: &[Self], i: usize, prev_space: bool) -> usize {
        #[cfg(target_arch = "x86_64")]
        return next_bad_chunked::<u8, 16>(u, i, prev_space, sse2::chunk_u8);
        #[cfg(not(target_arch = "x86_64"))]
        return next_bad_scalar(u, i, prev_space);
    }
}

impl Unit for u16 {
    const SPACE: Self = 0x20;
    #[inline]
    fn next_bad(u: &[Self], i: usize, prev_space: bool) -> usize {
        #[cfg(target_arch = "x86_64")]
        return next_bad_chunked::<u16, 8>(u, i, prev_space, sse2::chunk_u16);
        #[cfg(not(target_arch = "x86_64"))]
        return next_bad_scalar(u, i, prev_space);
    }
}

impl Unit for u32 {
    const SPACE: Self = 0x20;
    #[inline]
    fn next_bad(u: &[Self], i: usize, prev_space: bool) -> usize {
        #[cfg(target_arch = "x86_64")]
        return next_bad_chunked::<u32, 4>(u, i, prev_space, sse2::chunk_u32);
        #[cfg(not(target_arch = "x86_64"))]
        return next_bad_scalar(u, i, prev_space);
    }
}

/// True when `" ".join(u.split()) == u`.
#[inline]
pub fn is_joined<T: Unit>(u: &[T]) -> bool {
    T::next_bad(u, 0, true) == u.len() && u.last().is_none_or(|&c| !is_space(c.into()))
}

/// Where join_into writes: one unit, or a run copied whole.
pub trait Sink<T> {
    fn push(&mut self, v: T);
    fn push_slice(&mut self, s: &[T]);
}

impl<T: Copy> Sink<T> for Vec<T> {
    fn push(&mut self, v: T) {
        Vec::push(self, v);
    }
    fn push_slice(&mut self, s: &[T]) {
        self.extend_from_slice(s);
    }
}

/// Append `" ".join(u.split())` to `out` (eager form: a space is written when whitespace follows
/// a kept unit, and a trailing one is dropped at the end). Runs between bad units are copied whole.
pub fn join_into<T: Unit>(u: &[T], out: &mut impl Sink<T>, len: &mut usize, last_space: &mut bool) {
    let mut i = 0;
    let mut prev_space = true;
    while i < u.len() {
        let j = T::next_bad(u, i, prev_space);
        if j > i {
            out.push_slice(&u[i..j]);
            *len += j - i;
            *last_space = u[j - 1] == T::SPACE;
        }
        if j == u.len() {
            break;
        }
        // u[j] is bad whitespace: one separator at most, and only after a kept unit.
        if *len > 0 && !*last_space {
            out.push(T::SPACE);
            *len += 1;
            *last_space = true;
        }
        prev_space = true;
        i = j + 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reference(s: &[u32]) -> Vec<u32> {
        let mut out: Vec<u32> = Vec::new();
        for w in s.split(|&c| is_space(c)).filter(|w| !w.is_empty()) {
            if !out.is_empty() {
                out.push(0x20);
            }
            out.extend_from_slice(w);
        }
        out
    }

    fn join<T: Unit>(u: &[T]) -> Vec<u32> {
        let mut out: Vec<T> = Vec::new();
        let (mut len, mut last) = (0, false);
        join_into(u, &mut out, &mut len, &mut last);
        if last {
            out.pop();
        }
        out.into_iter().map(|c| c.into()).collect()
    }

    #[test]
    fn chunked_matches_scalar() {
        // Deterministic xorshift over an alphabet dense in whitespace and near-misses.
        let alpha: [u32; 24] = [
            0x20, 0x20, 0x20, 0x61, 0x62, 0x09, 0x0A, 0x1C, 0x1F, 0x21, 0x1B, 0x85, 0xA0, 0xE9,
            0x1680, 0x2000, 0x200A, 0x200B, 0x2028, 0x202F, 0x205F, 0x3000, 0x3001, 0x1F392,
        ];
        let mut x: u64 = 0x9E3779B97F4A7C15;
        for n in 0..4000usize {
            let len = n % 70;
            let mut s32 = Vec::with_capacity(len);
            for _ in 0..len {
                x ^= x << 13;
                x ^= x >> 7;
                x ^= x << 17;
                s32.push(alpha[(x % alpha.len() as u64) as usize]);
            }
            let want = reference(&s32);
            assert_eq!(join(&s32), want, "u32 {s32:x?}");
            assert_eq!(is_joined(&s32), want == s32, "u32 clean {s32:x?}");
            let s16: Vec<u16> = s32
                .iter()
                .filter(|&&c| c < 0x10000)
                .map(|&c| c as u16)
                .collect();
            let w16: Vec<u32> = s16.iter().map(|&c| c as u32).collect();
            assert_eq!(join(&s16), reference(&w16), "u16 {s16:x?}");
            assert_eq!(
                is_joined(&s16),
                reference(&w16) == w16,
                "u16 clean {s16:x?}"
            );
            let s8: Vec<u8> = s32
                .iter()
                .filter(|&&c| c < 0x100)
                .map(|&c| c as u8)
                .collect();
            let w8: Vec<u32> = s8.iter().map(|&c| c as u32).collect();
            assert_eq!(join(&s8), reference(&w8), "u8 {s8:x?}");
            assert_eq!(is_joined(&s8), reference(&w8) == w8, "u8 clean {s8:x?}");
        }
    }
}
