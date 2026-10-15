from scripts.ux_test_inventory import inventory


def test_inventory_finds_dom_assertions_and_helper_constants(tmp_path):
    template = tmp_path / "page.html"
    template.write_bytes(b'<main id="unique-main" class="unique-card">Keep this wording</main>')
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_page.py").write_bytes(
        b'assert "unique-main" in html\nEXPECTED = "Keep this wording"\n'
        b'assert "unique-cardigan" in html\n'
    )
    output = inventory([template], tests)
    assert "1 assert: unique-main" in output
    assert "2 reference: Keep this wording" in output
    assert "3 assert" not in output


def test_inventory_css_reads_selectors_not_hex_colors(tmp_path):
    from scripts.ux_test_inventory import emitted_strings

    css = tmp_path / "style.css"
    css.write_bytes(b'/* .comment */ .card:hover, #panel { color: #abcdef; content: "Ready"; }')
    assert emitted_strings(css) == {"card", "panel", "Ready"}
