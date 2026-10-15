# Self-hosted UI fonts (Console design system)

Localization OS is **offline-first**: the app must render identically with no network,
so the two Console typefaces are bundled here as `woff2` and loaded via `@font-face`
in `app/static/style.css`. There is **no** Google Fonts `<link>` / `@import`
anywhere, because that would break offline rendering (and leak requests).

## Contents

| File | Size | Family | Role | Weights used | License |
|---|---|---|---|---|---|
| `public-sans-latin-wght-normal.woff2` | 25.7 KB | Public Sans (variable, wght 100-900) | UI and headings (`--font-body`, `--font-display`, `body`) | 400 / 500 / 600 / 700 | SIL OFL 1.1 (`OFL-PublicSans.txt`) |
| `jetbrains-mono-latin-wght-normal.woff2` | 40.6 KB | JetBrains Mono (variable, wght 100-800) | Data and code: locale codes, costs, counts, keys (`--mono`, `--data-font`) | 400 / 500 | SIL OFL 1.1 (`OFL-JetBrainsMono.txt`) |

Each file is a single **variable** `woff2` (one file covers every weight in its
axis, so the type scale resolves from one download). Both families are SIL OFL 1.1,
which explicitly permits bundling provided the license file ships alongside, hence the
two `OFL-*.txt` files.

The earlier display and skin faces (Space Grotesk, Hanken Grotesk, IBM Plex, Spectral) were
removed with the skin system on 2026-10-08.

## Source / provenance

Subset locally (see **Regenerating** below) from the official full variable
TTFs in the `google/fonts` repo (`main` branch):

- `https://raw.githubusercontent.com/google/fonts/main/ofl/publicsans/PublicSans[wght].ttf`
- `https://raw.githubusercontent.com/google/fonts/main/ofl/jetbrainsmono/JetBrainsMono[wght].ttf`

The OFL license texts are the upstream copies from the same repo (`ofl/<family>/OFL.txt`).

## Subset

The subset is the standard Google-Fonts/Fontsource **`latin`** unicode-range
**plus `U+2190-2194` (horizontal/vertical arrows)**. `latin` includes
`U+0152-0153`, so it fully covers the app's shipped chrome locales
(en / es / fr / de: all accented forms, the sharp s, n with tilde, oe and y with
diaeresis live in this range). Adding a locale that needs Central/Eastern-European glyphs
(Polish, Czech, Turkish, Romanian, ...) would mean also bundling a `latin-ext` subset per
family and adding a second `@font-face` with the `latin-ext` unicode-range.

**The U+2192 lesson:** the UI writes locale pairs as `en-US -> de-DE` with a real arrow
(Data role, JetBrains Mono). The stock Fontsource `latin` range covers only `U+2191` and
`U+2193`, not the left, right and left-right arrows, so with the stock subsets the arrow fell
back to a system font mid-mono-run (wrong advance width), visibly breaking the column rhythm.
Any re-subsetting MUST keep `U+2190-2194` in the range. **Public Sans carries no arrow glyphs
upstream**, so `--font-body` lists JetBrains Mono right after it and an arrow in running text
falls back to the bundled mono face rather than a system font.

## Regenerating

Never install fonttools into the app venv/requirements: use a throwaway `--target` dir.
From any scratch directory:

```
pip install --target fonttools_env fonttools brotli
curl -LO "https://raw.githubusercontent.com/google/fonts/main/ofl/publicsans/PublicSans%5Bwght%5D.ttf"   # + jetbrainsmono
UNI="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2190-2194,U+2212,U+2215,U+FEFF,U+FFFD"
PYTHONPATH=fonttools_env python -m fontTools.subset "PublicSans[wght].ttf" \
  --output-file=public-sans-latin-wght-normal.woff2 \
  --flavor=woff2 --layout-features='*' --unicodes="$UNI"
# repeat for JetBrains Mono; keep the EXACT output filenames (base.html preloads and
# style.css @font-face reference them bare)
```

`--layout-features='*'` keeps every OpenType feature (kerning, ligatures, fractions, `tnum`
where the family has it). After regenerating, verify with fontTools that the output starts
with the `wOF2` magic and that **`src_arrows == out_arrows`** (subsetting must not DROP an arrow
the source had; JetBrains Mono carries all five, Public Sans none).
