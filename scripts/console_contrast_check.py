"""Console contrast verifier.

Parses the ACTUAL token values from app/static/style.css (:root = dark,
html[data-theme="light"] = light), then computes WCAG 2.1 relative-contrast for
every text / background pair (4.5:1 for text) and every UI component boundary or
focus indicator (3:1) in both themes. Soft-tint badge backgrounds are
alpha-composited over their underlying surface (worst case) before the check, so
this reflects what the eye actually sees. Exit code 1 if any pair falls below its
floor. tests/verify_ui_contrast.py runs it as a suite.
"""

import re
import sys

# Optional argv[1]: path to an alternate stylesheet (used for fail-before /
# pass-after proofs against a reconstructed pre-fix copy).
CSS = sys.argv[1] if len(sys.argv) > 1 else "app/static/style.css"
css = open(CSS, encoding="utf-8").read()  # noqa: SIM115


def block(sel):
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
    return m.group(1)


def parse(sel):
    out = {}
    for name, val in re.findall(r"(--[A-Za-z0-9-]+)\s*:\s*([^;]+);", block(sel)):
        out[name] = val.strip()
    return out


def hex_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def rgba(val):
    if val.startswith("#"):
        return (*hex_rgb(val), 1.0)
    m = re.match(r"rgba?\(([^)]+)\)", val)
    parts = [p.strip() for p in m.group(1).split(",")]
    r, g, b = (int(p) for p in parts[:3])
    a = float(parts[3]) if len(parts) > 3 else 1.0
    return (r, g, b, a)


def composite(fg_rgba, bg_rgb):
    r, g, b, a = fg_rgba
    return tuple(round(a * c + (1 - a) * d) for c, d in zip((r, g, b), bg_rgb, strict=False))


def lin(c):
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def lum(rgb):
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg):
    l1, l2 = lum(fg), lum(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def brighten(rgb, factor):
    return tuple(min(255, round(c * factor)) for c in rgb)


def merged(*selectors):
    """Resolve the effective token map for one theme by replaying the CSS cascade:
    start from :root (dark) and layer each matching block's overrides in source order.
    The light block only redefines the raw colours, so every alias (--bg, --text, ...)
    resolves against the light values, exactly as the browser does on <html>."""
    out = {}
    for sel in selectors:
        out.update(parse(sel))

    def resolve(name, seen=()):
        if name in seen:
            raise ValueError(f"cyclic token alias: {name}")
        return re.sub(r"var\((--[\w-]+)\)", lambda m: resolve(m[1], (*seen, name)), out[name])

    return {name: resolve(name) for name in out}


# The two themes the app renders. Console is the only look.
THEMES = {
    "console-dark": merged(":root"),
    "console-light": merged(":root", 'html[data-theme="light"]'),
}

# (label, fg-token, bg-token-or-composite, floor). A composite bg is expressed as
# ("soft-token", "surface-token") -> tint over surface.
CHECKS = [
    ("input border / bg", "--input-border", "--bg", 3.0),
    # UI component boundaries and focus indicators (WCAG 1.4.11, 2.4.11): 3:1.
    ("input border / field (--elev)", "--input-border", "--elev", 3.0),
    ("focus ring / bg", "--ring", "--bg", 3.0),
    ("focus ring / panel", "--ring", "--panel", 3.0),
    ("focus ring / panel-2", "--ring", "--panel-2", 3.0),
    ("focus ring / field (--elev)", "--ring", "--elev", 3.0),
    ("accent button fill / bg", "--primary", "--bg", 3.0),
    # Console surfaces: menus, popovers and fields sit on --elev; the active nav item on --nav-on;
    # selected rows on --primary-soft.
    ("ink / field (--elev)", "--text", "--elev", 4.5),
    ("dim ink / field (--elev)", "--text-dim", "--elev", 4.5),
    ("ink / active nav (--nav-on)", "--text", "--nav-on", 4.5),
    ("dim ink / active nav (--nav-on)", "--text-dim", "--nav-on", 4.5),
    ("ink / selected (--primary-soft)", "--text", "--primary-soft", 4.5),
    ("success / field (--elev)", "--success", "--elev", 4.5),
    ("danger / field (--elev)", "--danger", "--elev", 4.5),
    ("danger on danger-soft", "--danger", "--danger-soft", 4.5),
    ("input border / bg-2", "--input-border", "--bg-2", 3.0),
    ("input border / panel", "--input-border", "--panel", 3.0),
    ("input border / panel-2", "--input-border", "--panel-2", 3.0),
    ("ink / bg", "--text", "--bg", 4.5),
    ("ink / surface", "--text", "--panel", 4.5),
    ("dim ink / surface", "--text-dim", "--panel", 4.5),
    ("dim ink / bg-2 (inputs)", "--text-dim", "--bg-2", 4.5),
    ("muted / surface", "--muted", "--panel", 4.5),
    ("muted / elev", "--muted", "--elev", 4.5),
    ("muted / bg-2 (rows/inputs)", "--muted", "--bg-2", 4.5),
    ("muted / bg", "--muted", "--bg", 4.5),
    # bare .badge base (wave-3 polish): muted text directly on --panel-2, the
    # exact token the neutral chip background uses — --elev/--bg-2 above happen
    # to alias --panel-2's VALUE in some theme but not all, so this
    # is the one check that actually covers the badge base itself.
    ("muted / panel-2 (badge base)", "--muted", "--panel-2", 4.5),
    ("accent text / surface", "--primary-2", "--panel", 4.5),
    ("accent text / bg", "--primary-2", "--bg", 4.5),
    ("accent text / bg-2 (rows)", "--primary-2", "--bg-2", 4.5),
    # Sidebar ACTIVE nav item: accent text on the --primary-soft wash composited
    # over the SIDEBAR surface (--bg-2), not --panel — the review blind spot.
    (
        "accent on primary-soft/bg-2 (sidebar active)",
        "--primary-2",
        ("--primary-soft", "--bg-2"),
        4.5,
    ),
    ("success text / surface", "--success", "--panel", 4.5),
    ("warn text / surface", "--warn", "--panel", 4.5),
    ("danger text / surface", "--danger", "--panel", 4.5),
    # run-view graph overlay: the 'running' label is the accent TEXT token on the node surface.
    ("accent text / surface (run-view status)", "--primary-2", "--panel", 4.5),
    # Signal-dot status indicators (status-plates.html): the badge/chip
    # label is now state-coloured TEXT sitting DIRECTLY on the surface (no soft-tint
    # pill behind it), and the dot is a solid state-colour mark. So each state hue
    # must clear AA on EVERY surface a badge lands on, in both themes.
    # --panel is already covered by the four checks above; add --panel-2 (cards +
    # the editor st-* chips) and --bg-2 (table rows + the sidebar Beta badge) for
    # the four state hues (primary-2 / --bg-2 is already covered as "accent / rows").
    ("success / panel-2 (signal label)", "--success", "--panel-2", 4.5),
    ("warn / panel-2 (signal label)", "--warn", "--panel-2", 4.5),
    ("danger / panel-2 (signal label)", "--danger", "--panel-2", 4.5),
    ("accent text / panel-2 (signal label)", "--primary-2", "--panel-2", 4.5),
    ("success / bg-2 (signal label)", "--success", "--bg-2", 4.5),
    ("warn / bg-2 (signal label)", "--warn", "--bg-2", 4.5),
    ("danger / bg-2 (signal label)", "--danger", "--bg-2", 4.5),
    # badge = state text on state-soft tint composited over the surface (worst case)
    ("success on success-soft/surface", "--success", ("--success-soft", "--panel"), 4.5),
    ("warn on warn-soft/surface", "--warn", ("--warn-soft", "--panel"), 4.5),
    ("danger on reauth-wash/surface", "--danger", ("_reauth", "--panel"), 4.5),
    ("accent on primary-soft/surface (chips)", "--primary-2", ("--primary-soft", "--panel"), 4.5),
    ("term-tag on success-soft/surface", "--term-tag", ("--success-soft", "--panel"), 4.5),
    # text ON the accent primary button (large-ish bold 14px -> 4.5 to be safe)
    ("primary-ink on primary button", "--primary-ink", "--primary", 4.5),
    # Editor row-state tints (dirty / save-failed, rest + hover) over the card
    # surface the table sits on: body text must stay AA on every state.
    ("ink on dirty row", "--text", ("--row-dirty", "--panel"), 4.5),
    ("ink on dirty row (hover)", "--text", ("--row-dirty-hover", "--panel"), 4.5),
    ("ink on save-failed row", "--text", ("--row-failed", "--panel"), 4.5),
    ("ink on save-failed row (hover)", "--text", ("--row-failed-hover", "--panel"), 4.5),
    ("danger text on save-failed row", "--danger", ("--row-failed", "--panel"), 4.5),
    # Onboarding spotlight popover (SPEC-onboarding-tour Phase 2, driver.js
    # re-themed in style.css). The popover surface is --panel (card-
    # identical), so these alias existing card pairs, but assert them explicitly
    # so the spotlight text/button legibility is gated across both themes.
    ("spotlight title / popover", "--text", "--panel", 4.5),
    ("spotlight description / popover", "--text-dim", "--panel", 4.5),
    ("spotlight progress+close / popover", "--muted", "--panel", 4.5),
    ("spotlight primary-ink on action btn", "--primary-ink", "--primary", 4.5),
    # Batch-matrix heatmap tint (VIZ-RESEARCH-2026-07-21 quick-win): td.bm-cell now
    # carries `background: color-mix(in srgb, var(--X) N%, transparent)` — mathematically
    # identical to alpha-compositing --X at N% over the cell's surface (--panel, the
    # .card background the matrix table sits in). This changes what the chip's own
    # (already-checked, see "signal label" pairs above) text color sits on FROM bare
    # --panel TO panel-plus-tint, so re-check the same fg colors against the new
    # composited surface. 3-element tuple = (color-token, surface-token, mix-fraction),
    # resolved as color-mix's exact equivalent below.
    ("success chip on bm-ok tint/panel", "--success", ("--success", "--panel", 0.12), 4.5),
    ("accent chip on bm-run tint/panel", "--primary-2", ("--primary-2", "--panel", 0.12), 4.5),
    ("warn chip on bm-rev/part tint/panel", "--warn", ("--warn", "--panel", 0.14), 4.5),
    ("danger chip on bm-fail tint/panel", "--danger", ("--danger", "--panel", 0.12), 4.5),
    ("muted chip on bm-skip/na tint/panel", "--muted", ("--muted", "--panel", 0.08), 4.5),
    ("text-dim chip on bm-pending tint/panel", "--text-dim", ("--muted", "--panel", 0.08), 4.5),
]

worst = 99.0
failures = []
for theme, tok in THEMES.items():
    print(f"\n=== {theme.upper()} ===")
    for label, fg_t, bg_spec, floor in CHECKS:
        needed = [fg_t] + (
            [bg_spec]
            if isinstance(bg_spec, str)
            else [t for t in bg_spec if isinstance(t, str) and t.startswith("--")]
        )
        if any(t not in tok for t in needed):
            print(f"  [SKIP] ----     {label} (token absent in this stylesheet)")
            continue
        fg = hex_rgb(tok[fg_t])
        if isinstance(bg_spec, tuple) and len(bg_spec) == 3:
            # color-mix(in srgb, var(color_tok) alpha%, transparent) equivalent.
            color_tok, surf, alpha = bg_spec
            r, g, b = hex_rgb(tok[color_tok])
            bg = composite((r, g, b, alpha), hex_rgb(tok[surf]))
        elif isinstance(bg_spec, tuple):
            soft, surf = bg_spec
            # .badge.reauth uses a literal rgba(240,121,107,.14) danger wash (same
            # in both themes) rather than a token, so resolve that name specially.
            soft_val = "rgba(240,121,107,.14)" if soft == "_reauth" else tok[soft]
            bg = composite(rgba(soft_val), hex_rgb(tok[surf]))
        else:
            bg = hex_rgb(tok[bg_spec])
        r = ratio(fg, bg)
        worst = min(worst, r)
        flag = "OK " if r >= floor else "FAIL"
        if r < floor:
            failures.append((theme, label, r))
        print(f"  [{flag}] {r:5.2f}:1  {label}")

# hovered primary button (filter: brightness(.92), a darkening) - verify AA survives the hover
print("\n=== hovered primary button (brightness .92) ===")
for theme, tok in THEMES.items():
    prim = brighten(hex_rgb(tok["--primary"]), 0.92)
    ink = brighten(hex_rgb(tok["--primary-ink"]), 0.92)  # the filter darkens the label too
    r = ratio(ink, prim)
    flag = "OK " if r >= 4.5 else "FAIL"
    if r < 4.5:
        failures.append((theme, "hover primary button", r))
    worst = min(worst, r)
    print(f"  [{flag}] {r:5.2f}:1  {theme}: primary-ink on hovered primary")

print(f"\nSystem-wide worst pair: {worst:.2f}:1")
if failures:
    print("\nFAILURES:")
    for t, l, r in failures:  # noqa: E741
        print(f"  {t}: {l} = {r:.2f}:1")
    sys.exit(1)
print("ALL CONTRAST CHECKS PASS (text floor 4.5:1; input borders 3.0:1)")
