//! quality_common.tag_damage_py and the segment / ai_tagging helpers it reads, over code points.
//! Token order: Python sorts str by code point, which is slice order on u32 here.

use std::collections::BTreeMap;

use crate::text::{find_tokens, is_space, to_string, Tok, CLOSE, OPEN};

const RESIDUE_EXCERPT_CAP: usize = 80;

#[inline]
fn is_hex(c: u32) -> bool {
    matches!(c, 0x30..=0x39 | 0x41..=0x46 | 0x61..=0x66)
}

fn contains(hay: &[u32], needle: &[u32]) -> bool {
    needle.is_empty() || hay.windows(needle.len()).any(|w| w == needle)
}

/// True when `t` holds "TAG" (needed by any sentinel match).
fn has_tag(t: &[u32]) -> bool {
    t.windows(3).any(|w| w == [0x54, 0x41, 0x47])
}

/// ai_tagging.has_residual_sentinel(text, source): a `TAG[0-9a-fA-F]{6,}_` match of `text`
/// that is not a substring of `source` (finditer: non-overlapping; greedy hex then `_`, so no
/// backtracking choice).
fn has_residual_sentinel(text: &[u32], source: &[u32]) -> bool {
    let n = text.len();
    let mut i = 0;
    while i + 3 <= n {
        if text[i..i + 3] == [0x54, 0x41, 0x47] {
            let mut j = i + 3;
            while j < n && is_hex(text[j]) {
                j += 1;
            }
            if j - (i + 3) >= 6 && j < n && text[j] == 0x5F {
                if !contains(source, &text[i..=j]) {
                    return true;
                }
                i = j + 1;
                continue;
            }
        }
        i += 1;
    }
    false
}

fn counts<'a>(t: &'a [u32], toks: &[Tok]) -> BTreeMap<&'a [u32], usize> {
    let mut m = BTreeMap::new();
    for k in toks {
        *m.entry(k.text(t)).or_insert(0) += 1;
    }
    m
}

fn order_significant<'a>(t: &'a [u32], toks: &[Tok]) -> Vec<&'a [u32]> {
    toks.iter()
        .filter(|k| !k.is_free())
        .map(|k| k.text(t))
        .collect()
}

/// segment.placeholders_well_formed.
fn well_formed(t: &[u32], toks: &[Tok]) -> bool {
    let mut stack: Vec<&[u32]> = Vec::new();
    for k in toks {
        if k.close {
            if stack.last() != Some(&k.id(t)) {
                return false;
            }
            stack.pop();
        } else if !k.is_point() {
            stack.push(k.id(t));
        }
    }
    stack.is_empty()
}

/// segment.paired_span_bodies, reduced to what tag_damage reads: for each open-token name, is
/// its LAST body non-blank once nested tokens are stripped (`body.strip() != ""`)?
fn span_bodies<'a>(t: &'a [u32], toks: &[Tok]) -> BTreeMap<&'a [u32], bool> {
    // prefix[k]: how many of the gaps after tokens 0..k-1 hold a non-space character.
    let n = toks.len();
    let mut prefix = vec![0usize; n + 1];
    for k in 0..n {
        let gap_end = if k + 1 < n {
            toks[k + 1].start
        } else {
            t.len()
        };
        let nonblank = t[toks[k].end..gap_end].iter().any(|&c| !is_space(c));
        prefix[k + 1] = prefix[k] + nonblank as usize;
    }
    let mut stack: Vec<(&[u32], &[u32], usize)> = Vec::new(); // (id, open name, token index)
    let mut out = BTreeMap::new();
    for (k, tok) in toks.iter().enumerate() {
        if tok.close {
            match stack.last() {
                Some(&(id, _, _)) if id == tok.id(t) => {}
                _ => continue,
            }
            let (_, name, open_k) = stack.pop().unwrap();
            // The body is the gaps after tokens open_k .. k-1.
            out.insert(name, prefix[k] > prefix[open_k]);
        } else if !tok.is_point() {
            stack.push((tok.id(t), tok.inner(t), k));
        }
    }
    out
}

fn residue_len(t: &[u32], toks: &[Tok]) -> usize {
    t.iter().filter(|&&c| c == OPEN || c == CLOSE).count() - 2 * toks.len()
}

/// quality_common._residue_excerpt; None if the excerpt holds a lone surrogate.
fn residue_excerpt(t: &[u32]) -> Option<String> {
    // flat = " ".join(text.split())
    let mut flat: Vec<u32> = Vec::with_capacity(t.len());
    let mut gap = false;
    for &c in t {
        if is_space(c) {
            gap = !flat.is_empty();
        } else {
            if gap {
                flat.push(0x20);
                gap = false;
            }
            flat.push(c);
        }
    }
    let idx = flat
        .iter()
        .position(|&c| c == OPEN || c == CLOSE)
        .unwrap_or(0);
    let start = idx.saturating_sub(20);
    let stop = (start + RESIDUE_EXCERPT_CAP).min(flat.len());
    let mut out = String::new();
    if start > 0 {
        out.push_str("...");
    }
    out.push_str(&to_string(&flat[start.min(flat.len())..stop])?);
    if start + RESIDUE_EXCERPT_CAP < flat.len() {
        out.push_str("...");
    }
    Some(out)
}

fn join(items: &[&[u32]]) -> String {
    let mut out = String::new();
    for (i, it) in items.iter().enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        // Tokens and their names are `⟦`, `/`, [a-z], decimal digits and `⟧`: never surrogates.
        out.extend(it.iter().map(|&c| char::from_u32(c).unwrap_or('\u{FFFD}')));
    }
    out
}

/// The verdict; None only when a residue excerpt would hold a lone surrogate (the caller then
/// asks the Python reference).
pub fn tag_damage(source: &[u32], target: &[u32]) -> Option<String> {
    // Exact shortcut for tagless text: with no bracket character in either text, and no "TAG"
    // in the target, every rung below is clean (no sentinel, no tokens, no residue).
    let markers = |t: &[u32]| t.iter().any(|&c| c == OPEN || c == CLOSE);
    if !markers(source) && !markers(target) && !has_tag(target) {
        return Some(String::new());
    }
    let (src, tgt) = (source, target);
    if has_residual_sentinel(tgt, src) {
        return Some("residual masking sentinel in target".to_string());
    }
    let src_t = find_tokens(src);
    let tgt_t = find_tokens(tgt);
    let src_ph: Vec<&[u32]> = src_t.iter().map(|k| k.text(src)).collect();
    let tgt_ph: Vec<&[u32]> = tgt_t.iter().map(|k| k.text(tgt)).collect();
    let src_ct = counts(src, &src_t);
    let tgt_ct = counts(tgt, &tgt_t);
    let same_order = src_ph == tgt_ph
        || (src_ct == tgt_ct && order_significant(src, &src_t) == order_significant(tgt, &tgt_t));
    if !same_order {
        let mut dropped: Vec<&[u32]> = Vec::new();
        for (&tok, &c) in &src_ct {
            let have = tgt_ct.get(tok).copied().unwrap_or(0);
            for _ in have..c {
                dropped.push(tok);
            }
        }
        let mut added: Vec<&[u32]> = Vec::new();
        let mut duplicated: Vec<&[u32]> = Vec::new();
        for (&tok, &c) in &tgt_ct {
            let had = src_ct.get(tok).copied().unwrap_or(0);
            if c > had {
                if had == 0 {
                    for _ in 0..c {
                        added.push(tok);
                    }
                } else {
                    duplicated.push(tok);
                }
            }
        }
        let mut parts: Vec<String> = Vec::new();
        if !dropped.is_empty() {
            parts.push(format!("dropped {}", join(&dropped)));
        }
        if !added.is_empty() {
            parts.push(format!("added {}", join(&added)));
        }
        if !duplicated.is_empty() {
            parts.push(format!("duplicated {}", join(&duplicated)));
        }
        if parts.is_empty() && src_ct == tgt_ct {
            let mut sig = order_significant(src, &src_t);
            sig.sort_unstable();
            sig.dedup();
            parts.push(format!("reordered {}", join(&sig)));
        }
        return Some(format!("inline tag {}", parts.join("; ")));
    }
    let src_wf = well_formed(src, &src_t);
    if src_wf && !well_formed(tgt, &tgt_t) {
        return Some("malformed tag nesting".to_string());
    }
    if src_wf {
        let src_b = span_bodies(src, &src_t);
        let tgt_b = span_bodies(tgt, &tgt_t);
        let emptied: Vec<&[u32]> = src_b
            .iter()
            .filter(|&(name, &nonblank)| nonblank && !tgt_b.get(name).copied().unwrap_or(false))
            .map(|(&name, _)| name)
            .collect();
        if !emptied.is_empty() {
            return Some(format!("inline tag emptied span {}", join(&emptied)));
        }
    }
    if residue_len(tgt, &tgt_t) > residue_len(src, &src_t) {
        let why = if markers(src) {
            "more stray marker characters than the source"
        } else {
            "the source has none"
        };
        return Some(format!(
            "inline tag malformed marker residue in target ({why}): {}",
            residue_excerpt(tgt)?
        ));
    }
    Some(String::new())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::text::cps;

    fn td(a: &str, b: &str) -> String {
        let o = |s: &str| cps(&s.replace('[', "\u{27E6}").replace(']', "\u{27E7}"));
        tag_damage(&o(a), &o(b))
            .unwrap()
            .replace('\u{27E6}', "[")
            .replace('\u{27E7}', "]")
    }

    #[test]
    fn verdicts() {
        assert_eq!(td("a", "b"), "");
        assert_eq!(td("[g1]a[/g1]", "a"), "inline tag dropped [/g1], [g1]");
        assert_eq!(td("[g1]a[/g1]", "[g1][/g1]"), "inline tag emptied span g1");
        assert_eq!(
            td("[g1]a[/g1][x2]", "[x2][g1]a[/g1]"),
            "inline tag reordered [/g1], [g1], [x2]"
        );
        assert_eq!(
            td("a", "TAGabcdef12_1"),
            "residual masking sentinel in target"
        );
        assert_eq!(
            td("a", "b [r1"),
            "inline tag malformed marker residue in target (the source has none): b [r1"
        );
    }
}
