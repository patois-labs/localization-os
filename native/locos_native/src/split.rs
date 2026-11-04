//! segment._legacy_split_py: the built-in sentence splitter, versions 1 and 2, over code points
//! (indices are Python's string indices, so the 64-character abbreviation windows are plain
//! slices).
//!
//! Python patterns mirrored here (app/services/segment.py):
//!   _SENT_SPLIT   (?<=[.!?])\s+(?=["'(\[A-Z0-9])
//!   _CJK_SPLIT    (?<=[。！？])
//!   _ABBREV_RULES (version 2): `(?:before)\Z` searched in text[pos-64:pos] and `after` matched
//!   at the start of text[pos:pos+64]; `_WB` = `(?<![\w.])`.

use crate::tables::ci_eq;
use crate::text::{find_tokens, is_space, is_word, strip};

const ABBREV_WINDOW: usize = 64;

/// One pattern character of a `before` alternative.
#[derive(Clone, Copy)]
enum P {
    /// Literal (case-sensitive) character.
    L(char),
    /// `(?i)` letter (sre case-insensitive equality, tables::ci_eq).
    I(char),
    /// `\s`.
    S,
    /// `[A-HJ-Z]`.
    Initial,
}

#[derive(Clone, Copy)]
enum After {
    /// `\s`
    Space,
    /// `\s+[0-9]`
    SpacesDigit,
    /// `\s+(?!OPENERS)[A-Z]`
    SpacesName,
    /// `\s?B\.` / `\s?UU\.`
    OptSpaceThen(&'static str),
}

struct Rule {
    befores: Vec<Vec<P>>,
    after: After,
    locs: Option<&'static [&'static str]>,
}

/// Alternatives from a compact spelling: a leading '~' makes the alternative `(?i)`; ' '
/// stands for `\s`, and " ?" for `\s?` (expanded into both alternatives). Every alternative
/// gets the rule's trailing `\.`.
fn alts(spec: &[&str]) -> Vec<Vec<P>> {
    let mut out = Vec::new();
    for s in spec {
        let (ci, body) = match s.strip_prefix('~') {
            Some(b) => (true, b),
            None => (false, *s),
        };
        let mut variants: Vec<Vec<P>> = vec![Vec::new()];
        let chars: Vec<char> = body.chars().collect();
        let mut i = 0;
        while i < chars.len() {
            let c = chars[i];
            if c == ' ' {
                if chars.get(i + 1) == Some(&'?') {
                    let mut with = variants.clone();
                    for v in with.iter_mut() {
                        v.push(P::S);
                    }
                    variants.extend(with);
                    i += 2;
                    continue;
                }
                for v in variants.iter_mut() {
                    v.push(P::S);
                }
            } else {
                let p = if ci && c.is_alphabetic() {
                    P::I(c)
                } else {
                    P::L(c)
                };
                for v in variants.iter_mut() {
                    v.push(p);
                }
            }
            i += 1;
        }
        for mut v in variants {
            v.push(P::L('.'));
            out.push(v);
        }
    }
    out
}

fn rules() -> &'static [Rule] {
    use std::sync::OnceLock;
    static RULES: OnceLock<Vec<Rule>> = OnceLock::new();
    RULES.get_or_init(|| {
        let r = |befores: Vec<Vec<P>>, after, locs| Rule {
            befores,
            after,
            locs,
        };
        vec![
            // (?:(?i:e\.g|i\.e|incl|excl|ext|approx|appr|vs|cf|resp|esp|viz)|ca)\.   \s
            r(
                alts(&[
                    "~e.g", "~i.e", "~incl", "~excl", "~ext", "~approx", "~appr", "~vs", "~cf",
                    "~resp", "~esp", "~viz", "ca",
                ]),
                After::Space,
                None,
            ),
            // (?:Mr|Mrs|Ms|Mx|Dr|Prof|Rev|Hon|Gen|Capt|Sgt|Lt|Messrs)\.   \s
            r(
                alts(&[
                    "Mr", "Mrs", "Ms", "Mx", "Dr", "Prof", "Rev", "Hon", "Gen", "Capt", "Sgt",
                    "Lt", "Messrs",
                ]),
                After::Space,
                None,
            ),
            // (?i:no|nos|nr|max|min|fig|figs|vol|vols|pp|p|ch|sec|art|tel|ph|jan|..|dec)\.  \s+[0-9]
            r(
                alts(&[
                    "~no", "~nos", "~nr", "~max", "~min", "~fig", "~figs", "~vol", "~vols", "~pp",
                    "~p", "~ch", "~sec", "~art", "~tel", "~ph", "~jan", "~feb", "~mar", "~apr",
                    "~jun", "~jul", "~aug", "~sep", "~sept", "~oct", "~nov", "~dec",
                ]),
                After::SpacesDigit,
                None,
            ),
            // [A-HJ-Z]\.   \s+(?!OPENERS)[A-Z]
            r(vec![vec![P::Initial, P::L('.')]], After::SpacesName, None),
            // de: z\.   \s?B\.
            r(alts(&["z"]), After::OptSpaceThen("B."), Some(&["de"])),
            // de: (?:(?i:z\.\s?B|d\.\s?h|u\.\s?a|bzw|ggf|evtl|inkl|zzgl|vgl|bspw|sog)|Hr|Fr)\.  \s
            r(
                alts(&[
                    "~z. ?B", "~d. ?h", "~u. ?a", "~bzw", "~ggf", "~evtl", "~inkl", "~zzgl",
                    "~vgl", "~bspw", "~sog", "Hr", "Fr",
                ]),
                After::Space,
                Some(&["de"]),
            ),
            // de: (?i:abs|s)\.   \s+[0-9]
            r(alts(&["~abs", "~s"]), After::SpacesDigit, Some(&["de"])),
            // fr: (?:(?i:p\.\s?ex|c\.-à-d|env)|MM)\.   \s
            r(
                alts(&["~p. ?ex", "~c.-à-d", "~env", "MM"]),
                After::Space,
                Some(&["fr"]),
            ),
            // es: EE\.   \s?UU\.
            r(alts(&["EE"]), After::OptSpaceThen("UU."), Some(&["es"])),
            // es, pt: (?:Sr|Sra|Srta|Dra|Ud|Uds|(?i:aprox|p\.\s?ej))\.   \s
            r(
                alts(&["Sr", "Sra", "Srta", "Dra", "Ud", "Uds", "~aprox", "~p. ?ej"]),
                After::Space,
                Some(&["es", "pt"]),
            ),
            // es, pt: (?i:pág|núm)\.   \s+[0-9]
            r(
                alts(&["~pág", "~núm"]),
                After::SpacesDigit,
                Some(&["es", "pt"]),
            ),
            // it: (?:Sig|Dott|Ing|Avv)\.   \s
            r(
                alts(&["Sig", "Dott", "Ing", "Avv"]),
                After::Space,
                Some(&["it"]),
            ),
            // it: (?i:pag)\.   \s+[0-9]
            r(alts(&["~pag"]), After::SpacesDigit, Some(&["it"])),
            // nl: (?i:bijv|bv|o\.a|d\.w\.z|i\.p\.v|m\.b\.t)\.   \s
            r(
                alts(&["~bijv", "~bv", "~o.a", "~d.w.z", "~i.p.v", "~m.b.t"]),
                After::Space,
                Some(&["nl"]),
            ),
            // nl: (?i:blz)\.   \s+[0-9]
            r(alts(&["~blz"]), After::SpacesDigit, Some(&["nl"])),
        ]
    })
}

/// _OPENERS: words that need `\b` after them (then the `[AI]\s` arm).
const OPENERS: &[&str] = &[
    "The", "This", "That", "These", "Those", "Then", "There", "They", "Their", "We", "Our", "It",
    "Its", "In", "If", "Is", "You", "Your", "He", "His", "Her", "She", "My", "And", "But", "So",
    "Or", "For", "When", "What", "Why", "How", "Where", "Who", "Which", "Please", "Next", "Now",
    "Also", "However", "After", "Before", "Once", "To", "On", "At", "By", "With", "As", "An",
    "All", "Each", "Some", "Click", "Select", "See", "Note", "Use", "Do", "Not", "No", "Yes",
    "Let", "Here", "Der", "Die", "Das", "Den", "Dem", "Ein", "Eine", "Es", "Er", "Sie", "Wir",
    "Ich", "Und", "Aber", "Dann", "Bitte", "Le", "Les", "Il", "Elle", "Ils", "Nous", "Vous", "Je",
    "Ce", "Cette", "Puis", "Ensuite", "Et", "Mais", "El", "Los", "Las", "Una", "Un", "Ella",
    "Luego", "Pero", "Os", "Um", "Uma", "Ele", "Gli", "Lo", "Poi", "Ma", "Het", "Een", "Dit",
    "Dat", "Wij", "Ik", "Dan", "Maar",
];

#[inline]
fn p_matches(p: P, c: u32) -> bool {
    match p {
        P::L(l) => l as u32 == c,
        P::I(l) => char::from_u32(c).is_some_and(|ch| ci_eq(l, ch)),
        P::S => is_space(c),
        P::Initial => matches!(c, 0x41..=0x48 | 0x4A..=0x5A),
    }
}

/// `(?<![\w.])(?:alt)\Z` searched in `left` (a window: its start is a string start).
fn before_matches(befores: &[Vec<P>], left: &[u32]) -> bool {
    'alt: for alt in befores {
        if alt.len() > left.len() {
            continue;
        }
        let at = left.len() - alt.len();
        for (k, &p) in alt.iter().enumerate() {
            if !p_matches(p, left[at + k]) {
                continue 'alt;
            }
        }
        if at > 0 {
            let prev = left[at - 1];
            if is_word(prev) || prev == 0x2E {
                continue;
            }
        }
        return true;
    }
    false
}

#[inline]
fn leading_spaces(right: &[u32]) -> usize {
    right.iter().take_while(|&&c| is_space(c)).count()
}

/// End index when right[at..] starts with `word`.
fn starts_with(right: &[u32], at: usize, word: &str) -> Option<usize> {
    let mut k = at;
    for w in word.chars() {
        if right.get(k) != Some(&(w as u32)) {
            return None;
        }
        k += 1;
    }
    Some(k)
}

/// `(?:OPENERS)\b|[AI]\s` matched at right[at..] (the window end is a string end).
fn opener_at(right: &[u32], at: usize) -> bool {
    for w in OPENERS {
        if let Some(end) = starts_with(right, at, w) {
            if end == right.len() || !is_word(right[end]) {
                return true;
            }
        }
    }
    matches!(right.get(at), Some(0x41) | Some(0x49))
        && right.get(at + 1).is_some_and(|&c| is_space(c))
}

fn after_matches(after: After, right: &[u32]) -> bool {
    match after {
        After::Space => right.first().is_some_and(|&c| is_space(c)),
        After::SpacesDigit => {
            let k = leading_spaces(right);
            k > 0 && right.get(k).is_some_and(|c| (0x30..=0x39).contains(c))
        }
        After::SpacesName => {
            let k = leading_spaces(right);
            k > 0 && !opener_at(right, k) && right.get(k).is_some_and(|c| (0x41..=0x5A).contains(c))
        }
        After::OptSpaceThen(seq) => {
            (right.first().is_some_and(|&c| is_space(c)) && starts_with(right, 1, seq).is_some())
                || starts_with(right, 0, seq).is_some()
        }
    }
}

/// Which rule languages `(locale or "").lower().replace("_", "-").split("-", 1)[0]` equals.
/// The rule languages are lower-case ASCII, and no non-ASCII character lower-cases into one of
/// their letters (gen_tables.py checks KELVIN SIGN, lower 'k', is the only one reaching ASCII),
/// so an ASCII case-insensitive compare of the first subtag is exact.
pub struct Lang<'a>(&'a [u32]);

impl<'a> Lang<'a> {
    pub fn of(locale: &'a [u32]) -> Self {
        let end = locale
            .iter()
            .position(|&c| c == 0x2D || c == 0x5F)
            .unwrap_or(locale.len());
        Lang(&locale[..end])
    }
    fn is(&self, code: &str) -> bool {
        self.0.len() == code.len()
            && self
                .0
                .iter()
                .zip(code.bytes())
                .all(|(&c, b)| c < 0x80 && (c as u8).to_ascii_lowercase() == b)
    }
    fn any(&self, codes: &[&str]) -> bool {
        codes.iter().any(|c| self.is(c))
    }
}

/// segment._abbrev_holds; `pos` is the index just after the punctuation.
fn abbrev_holds(text: &[u32], pos: usize, lang: &Lang) -> bool {
    let left = &text[pos.saturating_sub(ABBREV_WINDOW)..pos];
    let right = &text[pos..(pos + ABBREV_WINDOW).min(text.len())];
    for rule in rules() {
        if let Some(locs) = rule.locs {
            if !lang.any(locs) {
                continue;
            }
        }
        if before_matches(&rule.befores, left) && after_matches(rule.after, right) {
            return true;
        }
    }
    false
}

/// segment._protected_spans: (open start, close end) for each close whose id was open.
fn protected_spans(text: &[u32]) -> Vec<(usize, usize)> {
    let toks = find_tokens(text);
    let mut spans = Vec::new();
    if toks.is_empty() {
        return spans;
    }
    let mut opens: std::collections::HashMap<&[u32], usize> = std::collections::HashMap::new();
    for t in toks {
        if t.close {
            if let Some(start) = opens.remove(t.id(text)) {
                spans.push((start, t.end));
            }
        } else {
            opens.insert(t.id(text), t.start);
        }
    }
    spans
}

#[inline]
fn in_protected(idx: usize, spans: &[(usize, usize)]) -> bool {
    spans.iter().any(|&(s, e)| s < idx && idx < e)
}

/// The pieces as (start, end) code-point ranges of `text` (already stripped by the caller's
/// `text.strip()` mirror: see `split`).
pub fn split(text: &[u32], locale: &[u32], version: u8) -> Vec<(usize, usize)> {
    let (a, b) = strip(text, 0, text.len());
    if a == b {
        return Vec::new();
    }
    let t = &text[a..b];
    let spans = protected_spans(t);
    let lang = Lang::of(locale);
    let mut cuts: Vec<usize> = vec![0, t.len()];
    let n = t.len();
    let mut i = 1;
    while i < n {
        let c = t[i];
        // _SENT_SPLIT: the whitespace run right after . ! ? (greedy \s+, then a one-character
        // lookahead that is never whitespace: the whole run or nothing; never overlapping).
        if matches!(t[i - 1], 0x2E | 0x21 | 0x3F) && is_space(c) {
            let mut end = i + 1;
            while end < n && is_space(t[end]) {
                end += 1;
            }
            let opens =
                end < n && matches!(t[end], 0x22 | 0x27 | 0x28 | 0x5B | 0x41..=0x5A | 0x30..=0x39);
            if opens && !(version == 2 && abbrev_holds(t, i, &lang)) {
                cuts.push(end);
            }
            i = end;
            continue;
        }
        i += 1;
    }
    // _CJK_SPLIT: zero width after every CJK stop.
    for (k, &c) in t.iter().enumerate() {
        if matches!(c, 0x3002 | 0xFF01 | 0xFF1F) {
            cuts.push(k + 1);
        }
    }
    cuts.sort_unstable();
    cuts.dedup();
    cuts.retain(|&k| !in_protected(k, &spans));
    let mut out = Vec::new();
    for w in cuts.windows(2) {
        let (pa, pb) = strip(t, w[0], w[1]);
        if pa < pb {
            out.push((a + pa, a + pb));
        }
    }
    if out.is_empty() {
        out.push((a, b));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::text::{cps, to_string};

    fn run(s: &str, loc: &str, v: u8) -> Vec<String> {
        let t = cps(s);
        split(&t, &cps(loc), v)
            .into_iter()
            .map(|(a, b)| to_string(&t[a..b]).unwrap())
            .collect()
    }

    #[test]
    fn basics() {
        assert_eq!(
            run("Max. 5 files. Then wait.", "en", 2),
            ["Max. 5 files.", "Then wait."]
        );
        assert_eq!(
            run("Max. 5 files. Then wait.", "en", 1),
            ["Max.", "5 files.", "Then wait."]
        );
        assert_eq!(run("z. B. das. Und", "de-DE", 2), ["z. B. das.", "Und"]);
        assert_eq!(run(" a\u{3002}b ", "", 2), ["a\u{3002}", "b"]);
        assert_eq!(run("  ", "", 2), Vec::<String>::new());
    }
}
