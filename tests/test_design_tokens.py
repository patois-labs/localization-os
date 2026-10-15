"""Two-way (dark, light) Console token contract, including composited state backgrounds."""

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parents[1] / "app/static/style.css"
SELECTORS = [":root", 'html[data-theme="light"]']


def raw_tokens(selector, source=None):
    """The raw declarations of ONE top-level token plate (exactly one must exist)."""
    css = re.sub(
        r"/\*.*?\*/",
        "",
        source if source is not None else CSS.read_text(encoding="utf-8"),
        flags=re.S,
    )
    # Token plates are top-level; media overrides (including driver.js :root)
    # deliberately are not plates and have their own scoped tests below.
    matches = [
        m[1]
        for m in re.finditer(r"(?:^|(?<=[{}]))\s*" + re.escape(selector) + r"\s*\{([^{}]+)\}", css)
        if css[: m.start()].count("{") == css[: m.start()].count("}")
    ]
    assert len(matches) == 1, (selector, "expected exactly one token block", len(matches))
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", matches[0]))


def tokens(selector, source=None):
    """The effective tokens for a theme. The light plate only redefines the raw colors, so it is
    layered over :root exactly as the cascade does on <html> (the aliases then resolve against
    the light colors)."""
    raw = raw_tokens(":root", source)
    if selector != ":root":
        raw.update(raw_tokens(selector, source))

    def resolve(name, trail=()):
        assert name not in trail, f"cyclic alias: {trail} -> {name}"
        value = raw[name]

        def replacement(match):
            if match[1] in raw:
                return resolve(match[1], (*trail, name))
            assert match[2] is not None, f"undefined token: {match[1]}"
            return match[2]

        while "var(" in value:
            replaced = re.sub(
                r"var\((--[\w-]+)(?:,\s*([^()]*))?\)",
                replacement,
                value,
            )
            assert replaced != value, value
            value = replaced
        return value

    return {name: resolve(name) for name in raw}


def rgb(value, background=(1, 1, 1)):
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits)
        assert len(digits) in (6, 8), value
        alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1
        return tuple(
            int(digits[i : i + 2], 16) / 255 * alpha + b * (1 - alpha)
            for i, b in zip((0, 2, 4), background, strict=True)
        )
    parts = [float(n) for n in re.findall(r"[\d.]+", value)]
    assert len(parts) in (3, 4), value
    alpha = parts[3] if len(parts) == 4 else 1
    return tuple(
        n / 255 * alpha + b * (1 - alpha) for n, b in zip(parts[:3], background, strict=True)
    )


def contrast(a, b):
    def luminance(c):
        return sum(
            w * (v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
            for w, v in zip((0.2126, 0.7152, 0.0722), c, strict=True)
        )

    lo, hi = sorted((luminance(a), luminance(b)))
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("selector", SELECTORS)
def test_contrast_matrix(selector):
    t = tokens(selector)
    surfaces = ("--bg", "--bg-2", "--panel", "--panel-2", "--elev")
    for surface in surfaces:
        bg = rgb(t[surface])
        for fg in (
            "--text",
            "--text-dim",
            "--muted",
            "--primary-2",
            "--success",
            "--warn",
            "--danger",
            "--term-tag",
        ):
            assert contrast(rgb(t[fg]), bg) >= 4.5, (selector, fg, surface)
        for fg in ("--input-border", "--ring", "--primary"):
            assert contrast(rgb(t[fg], bg), bg) >= (3.2 if fg == "--input-border" else 3), (
                selector,
                fg,
                surface,
            )
        # Decorative tints do not identify controls alone. Guard against making
        # them as heavy as the essential boundary or the solid accent again.
        for border, solid in (
            ("--primary-border", "--primary"),
            ("--hover-border", "--input-border"),
        ):
            assert 1 < contrast(rgb(t[border], bg), bg) < contrast(rgb(t[solid]), bg), (
                selector,
                border,
                surface,
            )
        for fg, wash in (
            ("--primary-2", "--primary-soft"),
            ("--success", "--success-soft"),
            ("--term-tag", "--success-soft"),
            ("--warn", "--warn-soft"),
            ("--danger", "--danger-soft"),
            ("--text", "--row-dirty-hover"),
            ("--text", "--row-failed-hover"),
            ("--color-disabled-text", "--color-disabled-surface"),
        ):
            assert contrast(rgb(t[fg]), rgb(t[wash], bg)) >= 4.5, (selector, fg, wash, surface)
    assert contrast(rgb(t["--primary-ink"]), rgb(t["--primary"])) >= 4.5
    # CSS brightness filters affect both foreground and background, with clipping.
    for factor in (0.92,):

        def filtered(name, factor=factor):
            return tuple(min(1, c * factor) for c in rgb(t[name]))

        assert contrast(filtered("--primary-ink"), filtered("--primary")) >= 4.5


@pytest.mark.parametrize("selector", SELECTORS)
def test_shared_geometry_and_aliases(selector):
    t = tokens(selector)
    assert t.keys() == tokens(":root").keys()
    expected = {
        "--radius": "8px",
        "--radius-sm": "6px",
        "--radius-xs": "4px",
        "--font-size-body": "0.875rem",
        "--font-size-title": "1.5rem",
        "--control-height": "34px",
        "--row-height": "48px",
        "--focus-width": "2px",
        "--focus-offset": "2px",
        "--shadow-sm": "none",
        "--elevation-overlay": "none",
        "--elevation-popover": "none",
    }
    for name, value in expected.items():
        assert t[name] == value, (selector, name)
    assert t["--panel"] == t["--color-surface"]
    assert t["--text"] == t["--color-text"]
    assert t["--display-tracking"] == "-.01em"
    assert t["--radius-sm"] == "6px", "Console controls use a 6px radius"


def reduced_motion_blocks(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for match in re.finditer(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{", css):
        depth = 1
        for end in range(match.end(), len(css)):
            depth += (css[end] == "{") - (css[end] == "}")
            if depth == 0:
                yield css[match.end() : end]
                break
        else:
            raise AssertionError("unclosed media query")


def assert_reduced_motion(css):
    assert any(
        all(re.search(rf"{name}:\s*0s\s*;", block) for name in ("--motion-fast", "--motion-normal"))
        for block in reduced_motion_blocks(css)
    )


def test_reduced_motion_contract():
    css = CSS.read_text(encoding="utf-8")
    assert "var(--motion-fast)" in css
    assert_reduced_motion(css)


def test_reduced_motion_rejects_unrelated_block():
    with pytest.raises(AssertionError):
        assert_reduced_motion(
            "@media (prefers-reduced-motion: reduce) { a { color: red; } } "
            ":root { --motion-fast: 0s; --motion-normal: 0s; }"
        )


def test_token_parser_rejects_duplicate_and_missing_selectors():
    for source in (":root { --a: red; } :root { --a: blue; }", "html:root { --a: red; }"):
        with pytest.raises(AssertionError):
            tokens(":root", source)


def test_token_parser_nested_fallback_and_short_hex():
    assert tokens(":root", ":root { --a: #0005; --b: var(--absent, var(--a)); }")["--b"] == "#0005"
    assert rgb("#0005") == pytest.approx((2 / 3,) * 3)
