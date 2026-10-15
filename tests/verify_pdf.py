"""PDF ingestion + vision-OCR verification (offline).

- a digital (text-layer) PDF extracts text directly, no OCR
- a scanned (image-only) PDF triggers the OCR path; with no vision engine
  configured it returns a deterministic placeholder (real engines do real OCR)
- upload endpoint accepts .pdf
- export rebuilds a valid translated PDF
- EVERY default-locale script renders correctly in the rebuilt PDF (per-script ink
  + tofu + extraction checks for ru/el/ja/zh/ko/ar/he/hi/bn/th) — the hard part,
  now handled by typst's shaping + system-font fallback (see gate round 1: ReportLab
  produced blank/tofu/reversed output for 8 of the 30 default locales)

The filter (app/filters/pdf_filter.py) builds with typst and parses with pypdfium2;
this suite uses the same permissive stack — no copyleft PDF engine, and no ReportLab.
See tests/verify_licenses.py.
"""

import concurrent.futures as cf
import io
import os
import sys
import tempfile
import unicodedata

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_test_")

import pypdfium2 as pdfium
import typst
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, select

from app import config
from app.db import engine, init_db
from app.filters import pdf_filter
from app.filters.pdf_filter import PdfFilter
from app.main import app
from app.models import Segment, SpendLedger
from app.services import ocr
from app.services import settings as settings_svc
from app.services import spend as spend_svc

# Force the offline OCR placeholder so the test is deterministic regardless of
# whatever keys/servers happen to be reachable from this machine.
config.ANTHROPIC_API_KEY = config.OPENAI_API_KEY = config.GEMINI_API_KEY = ""
config.LOCAL_BASE_URL = ""

_FILTER = PdfFilter()


def text_pdf() -> bytes:
    """A digital (text-layer) PDF built with the real filter (typst) — same two
    strings the old fixture carried, so the extraction assertions below are
    unchanged."""
    return _FILTER.build(
        b"",
        {
            "0": "Hello world. This is a digital PDF.",
            "1": "Second paragraph here.",
        },
    )


def scanned_pdf() -> bytes:
    """A page that is ONLY an image (no text layer) -> forces OCR. Render the digital
    PDF's first page to a PNG (pypdfium2 + pillow), then place JUST that image (no
    text) on a fresh A4 page via typst — no ReportLab."""
    src = pdfium.PdfDocument(text_pdf())
    page = src[0]
    bitmap = page.render(scale=150 / 72)
    pil = bitmap.to_pil().convert("RGB")
    bitmap.close()
    page.close()
    src.close()

    with tempfile.TemporaryDirectory() as d:
        pil.save(os.path.join(d, "scan.png"))
        source = (
            '#set page(paper: "a4", margin: 0pt)\n#image("scan.png", width: 100%, height: 100%)\n'
        )
        return typst.compile(source.encode("utf-8"), root=d)


def mixed_pdf() -> bytes:
    """A two-page doc: page 1 is a real text layer (no OCR), page 2 is ONLY an image
    (forces OCR). Used by the thread-safety test to exercise both parse paths — text
    extraction AND page render — concurrently."""
    src = pdfium.PdfDocument(text_pdf())
    page = src[0]
    bitmap = page.render(scale=150 / 72)
    pil = bitmap.to_pil().convert("RGB")
    bitmap.close()
    page.close()
    src.close()
    with tempfile.TemporaryDirectory() as d:
        pil.save(os.path.join(d, "scan.png"))
        source = (
            '#set page(paper: "a4", margin: 40pt)\n'
            "This is page one with a real text layer and plenty of characters.\n"
            "#pagebreak()\n"
            '#set page(margin: 0pt)\n#image("scan.png", width: 100%, height: 100%)\n'
        )
        return typst.compile(source.encode("utf-8"), root=d)


def scanned_noise_pdf(n: int) -> bytes:
    """An N-page doc where every page is ONLY a dense random-noise image (no text -> every
    page hits the OCR path, and the rendered PNG does NOT compress, so each page's PNG is
    large). Used by the memory test to expose all-pages-buffered-before-OCR."""
    img = Image.frombytes("RGB", (1000, 1400), os.urandom(1000 * 1400 * 3))
    with tempfile.TemporaryDirectory() as d:
        img.save(os.path.join(d, "n.png"))
        one = '#image("n.png", width: 100%, height: 100%)'
        body = "\n#pagebreak()\n".join([one] * n)  # pagebreaks BETWEEN pages -> exactly n
        return typst.compile(
            ('#set page(paper: "a4", margin: 0pt)\n' + body + "\n").encode("utf-8"), root=d
        )


def upload(client, pid, name, data):
    r = client.post(
        f"/projects/{pid}/documents",
        data={"mode": "segmented"},
        files={"file": (name, data, "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[-1])


def segs(doc_id, locale="fr-FR"):
    with Session(engine) as s:
        return sorted(
            s.exec(
                select(Segment).where(
                    Segment.document_id == doc_id, Segment.target_locale == locale
                )
            ).all(),
            key=lambda x: x.order_index,
        )


# --- issue #29: OCR spend guard (fake Anthropic SDK, no network) -------------
class _FakeAnthropicUsage:
    input_tokens = 500
    output_tokens = 50
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _FakeAnthropicBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeAnthropicMessage:
    def __init__(self, text):
        self.content = [_FakeAnthropicBlock(text)]
        self.usage = _FakeAnthropicUsage()


class _FakeAnthropicMessages:
    calls = 0

    def create(self, **kw):
        _FakeAnthropicMessages.calls += 1
        return _FakeAnthropicMessage("Bonjour le monde")


class _FakeAnthropicClient:
    def __init__(self, *a, **kw):
        self.messages = _FakeAnthropicMessages()


def _reset_ledger():
    """Zero the current period's ledger row so a test starts from $0 regardless
    of suite order (record_usage is additive across the whole process)."""
    key = spend_svc.period_key()
    with Session(engine) as s:
        row = s.get(SpendLedger, key)
        if row:
            row.accumulated_usd = 0.0
            s.add(row)
            s.commit()
    spend_svc._tripped_audited.clear()


def test_ocr_usage_extraction_helpers():
    """Per-SDK usage normalizers (OpenAI/Gemini) map real response attribute
    names to our usage dict correctly. Claude reuses providers._claude_usage
    directly (already covered by verify_provider_resilience.py); these two are
    new in ocr.py and worth a direct unit check."""

    class _OAIDetails:
        cached_tokens = 30

    class _OAIUsage:
        prompt_tokens = 120
        completion_tokens = 40
        prompt_tokens_details = _OAIDetails()

    u = ocr._openai_usage(_OAIUsage())
    assert u == {
        "input_tokens": 90,  # prompt_tokens 120 INCLUDES the 30 cached: non-cached remainder (#506)
        "output_tokens": 40,
        "cache_read_tokens": 30,
        "cache_write_tokens": 0,
    }, u
    assert ocr._openai_usage(None) is None

    class _GemUsage:
        prompt_token_count = 200
        candidates_token_count = 60
        cached_content_token_count = 10

    g = ocr._gemini_usage(_GemUsage())
    assert g == {
        "input_tokens": 190,  # prompt_token_count 200 includes the 10 cached (#506)
        "output_tokens": 60,
        "cache_read_tokens": 10,
        "cache_write_tokens": 0,
    }, g
    assert ocr._gemini_usage(None) is None
    print("  ocr spend guard: openai/gemini usage normalizers OK")


def test_ocr_spend_guard():
    """Issue #29: OCR's real-provider calls are gated by the spend cap and
    recorded to the ledger, exactly like every other real-provider call site."""
    import anthropic

    orig_cls = anthropic.Anthropic
    orig_key = config.ANTHROPIC_API_KEY
    anthropic.Anthropic = _FakeAnthropicClient
    config.ANTHROPIC_API_KEY = "sk-test"
    try:
        # --- over cap: clean placeholder, the SDK is never even constructed ---
        _reset_ledger()
        settings_svc.set_many({"spend_cap_usd": "0.0001"})  # tiny cap
        # settings.set_many() -> apply_to_config() re-derives config.ANTHROPIC_
        # API_KEY from the (empty) stored/.env secret, clobbering the monkeypatch
        # above -- re-apply it after every set_many so the "claude" branch stays
        # reachable in this test.
        config.ANTHROPIC_API_KEY = "sk-test"
        spend_svc.record_usage(
            "claude", "claude-opus-4-8", {"input_tokens": 1_000_000}
        )  # $5 >> cap
        assert spend_svc.would_block()[0] is True
        _FakeAnthropicMessages.calls = 0
        out = ocr.ocr_image(b"fake-png-bytes", "claude")
        assert out.startswith("[OCR blocked:") and "Spend cap reached" in out, out
        assert _FakeAnthropicMessages.calls == 0, (
            "a blocked OCR call must never reach the provider",
            _FakeAnthropicMessages.calls,
        )
        print("  ocr spend guard: over cap -> clean degradation, provider not called")

        # --- under cap: the real call succeeds and accrues real spend ---
        settings_svc.set_many({"spend_cap_usd": "1000000"})
        config.ANTHROPIC_API_KEY = "sk-test"  # re-apply (see comment above)
        _reset_ledger()
        before = spend_svc.spent_this_period()
        out = ocr.ocr_image(b"fake-png-bytes", "claude")
        assert out == "Bonjour le monde", out
        assert _FakeAnthropicMessages.calls == 1, _FakeAnthropicMessages.calls
        after = spend_svc.spent_this_period()
        assert after > before, "a successful OCR call must accrue real spend"
        print(f"  ocr spend guard: under cap -> call succeeds, ledger accrues (${after:.6f})")
    finally:
        anthropic.Anthropic = orig_cls
        config.ANTHROPIC_API_KEY = orig_key
        settings_svc.set_many({"spend_cap_usd": "0"})
        _reset_ledger()

    # --- offline path (no keys) stays byte-identical: never touches spend ---
    # Only ANTHROPIC_API_KEY matters for provider="claude" below (re-asserted
    # blank defensively — the finally block above already restored it via
    # apply_to_config, same as the module-level setup). NOTE: settings.DEFAULTS
    # seeds "local_base_url" from a hardcoded fallback (settings.py:41) the first
    # time apply_to_config() runs in-process, so config.LOCAL_BASE_URL is NOT
    # reliably "" after any set_many() call in this suite -- irrelevant here
    # since it only gates the "local" branch, not "claude".
    config.ANTHROPIC_API_KEY = ""
    assert config.ANTHROPIC_API_KEY == "", "claude branch must be unreachable"
    before = spend_svc.spent_this_period()
    out = ocr.ocr_image(b"fake-png-bytes", "claude")
    assert out == "[scanned page - configure a vision-capable engine in Setup AI to enable OCR]", (
        out
    )
    assert spend_svc.spent_this_period() == before, (
        "the offline placeholder path must never call spend"
    )
    print("  ocr spend guard: offline placeholder path unchanged, no spend call")


# --- pypdfium2 read helpers (shared by the tests below) ----------------------
def _pdf_text(data: bytes) -> tuple[int, str]:
    """Return (page_count, all_text) for a PDF, extracted with pypdfium2."""
    doc = pdfium.PdfDocument(data)
    try:
        n = len(doc)
        chunks = []
        for i in range(n):
            page = doc[i]
            tp = page.get_textpage()
            chunks.append(tp.get_text_range() or "")
            tp.close()
            page.close()
        return n, "\n".join(chunks)
    finally:
        doc.close()


# --- per-script render measurement (HIGH-2 fix: measure the TEXT REGION, per script) -
# The top band of the page, at render scale 2, where a single-paragraph document's text
# sits. Measuring the band (not the whole page) is what makes a blank paragraph fail —
# the old suite averaged ink over the WHOLE page, so two invisible scripts hid behind
# three visible ones.
_REGION_TOP_PT = 120
_REGION_SCALE = 2
# Per-script render thresholds, calibrated on the shipping typst writer (every real
# script clears them by a wide margin; a blank paragraph is ~0.0 ink and a box-grid
# tofu render collapses the ink-vs-□ difference):
_INK_MIN = 0.0010  # real scripts here: 0.0037-0.0095
_TOFU_DIFF_MIN = 0.0020  # real scripts here: 0.0047-0.0113

# Ten scripts spanning the default-enabled catalogue's hard cases (CJK, RTL, Indic,
# Thai) plus Cyrillic/Greek. The two RTL scripts are compared by WORD SET because
# PDFium returns RTL runs in visual (reversed) order.
_SCRIPTS = {
    "ru": "Быстрая коричневая лиса перепрыгивает через ленивую собаку.",
    "el": "Η γρήγορη καφέ αλεπού πηδάει πάνω από το τεμπέλικο σκυλί.",
    "ja": "素早い茶色の狐が怠け者の犬を飛び越えます。",
    "zh-Hans": "敏捷的棕色狐狸跳过了懒狗。",
    "ko": "빠른 갈색 여우가 게으른 개를 뛰어넘습니다.",
    "ar": "الثعلب البني السريع يقفز فوق الكلب الكسول.",
    "he": "השועל החום המהיר קופץ מעל הכלב העצלן.",
    "hi": "तेज़ भूरी लोमड़ी आलसी कुत्ते के ऊपर कूद जाती है।",
    "bn": "দ্রুত বাদামী শিয়াল অলস কুকুরের উপর দিয়ে লাফ দেয়।",
    "th": "สุนัขจิ้งจอกสีน้ำตาลว่องไวกระโดดข้ามสุนัขขี้เกียจ",
}
_RTL = {"ar", "he"}


def _region_mask(data: bytes) -> list:
    """Binary ink mask of the page-1 text region (top _REGION_TOP_PT at scale 2)."""
    doc = pdfium.PdfDocument(data)
    try:
        pil = doc[0].render(scale=_REGION_SCALE).to_pil().convert("L")
    finally:
        doc.close()
    h = min(int(_REGION_TOP_PT * _REGION_SCALE), pil.height)
    crop = pil.crop((0, 0, pil.width, h))
    return [1 if p < 128 else 0 for p in crop.getdata()]


def _mask_ink(mask: list) -> float:
    return sum(mask) / max(1, len(mask))


def _region_extract(data: bytes) -> str:
    doc = pdfium.PdfDocument(data)
    try:
        return doc[0].get_textpage().get_text_range() or ""
    finally:
        doc.close()


def _norm(s: str) -> str:
    """NFC + whitespace-COLLAPSED form (runs of whitespace -> one space, ends stripped) for
    round-trip equality on Latin text, where PDFium preserves inter-word spaces faithfully.
    It never DELETES whitespace, so a whitespace-level mutation (a tab fusing two words)
    still fails the escape test (MED-2). The per-script (Indic/CJK/RTL) tests use character-
    multiset containment instead — see _chars."""
    return unicodedata.normalize("NFC", " ".join((s or "").split()))


def _chars(s: str):
    """Multiset of non-whitespace NFC characters. Used for the RTL round-trip check:
    PDFium returns Arabic/Hebrew runs in visual order AND may split a word across the
    reorder (e.g. 'السريع' -> 'يع السر'), so a word-set check is not robust; requiring
    the full CHARACTER multiset to round-trip is stronger (every glyph must survive) and
    order/fragmentation independent — with NO tolerance."""
    from collections import Counter

    return Counter(c for c in unicodedata.normalize("NFC", s or "") if not c.isspace())


def _tofu_baseline_mask(nchars: int):
    """A same-length paragraph of box glyphs to compare against (the tofu guard). Try
    □ (U+25A1) then ▯/▮ (U+25AF/U+25AE); return the FIRST that itself renders non-blank
    ink, or None if none do (LOW-4: never let a blank baseline silently turn the tofu
    check into a duplicate blank check)."""
    for glyph in ("□", "▯", "▮"):
        mask = _region_mask(_FILTER.build(b"", {"0": glyph * nchars}))
        if _mask_ink(mask) > _INK_MIN:
            return mask, glyph
    return None, None


def _check_one_script(code: str, text: str) -> None:
    """Assert one script renders: (1) text-region ink above the blank floor,
    (2) the render differs substantially from a same-length box paragraph — the tofu
    guard (skipped LOUDLY if no box baseline glyph renders on this host), and (3) EVERY
    character round-trips through extraction (full character-multiset CONTAINMENT — not a
    prefix). Containment rather than exact sequence equality because PDFium's extraction
    reorders/duplicates combining marks for complex scripts (Thai/Devanagari) and returns
    RTL runs reversed and sometimes word-split; requiring every source glyph to appear
    still catches any substitution or deletion (HIGH-2/HIGH-3). The Latin escape test
    (test_escape_is_load_bearing) holds the exact-sequence line where extraction is
    faithful."""
    data = _FILTER.build(b"", {"0": text})
    mask = _region_mask(data)
    ink = _mask_ink(mask)
    assert ink > _INK_MIN, f"[{code}] text region is blank (ink {ink:.5f} <= {_INK_MIN})"

    # tofu guard: a same-character-count box paragraph. Correctly shaped glyphs differ
    # substantially from a row of identical boxes; a tofu render (near-identical boxes)
    # collapses this difference. LOW-4: only meaningful if the box glyph itself renders.
    bmask, glyph = _tofu_baseline_mask(len(text.replace(" ", "")))
    if bmask is None:
        print(f"  script {code}: tofu baseline glyph blank on this host — tofu check SKIPPED")
        diff = float("nan")
    else:
        n = min(len(mask), len(bmask))
        diff = sum(1 for i in range(n) if mask[i] != bmask[i]) / max(1, n)
        assert diff > _TOFU_DIFF_MIN, (
            f"[{code}] render looks like tofu boxes (diff-vs-{glyph!r} {diff:.5f})"
        )

    got = _region_extract(data)
    missing = _chars(text) - _chars(got)
    assert not missing, (
        f"[{code}] characters did not round-trip (missing {dict(missing)}):\n"
        f"    src={_norm(text)!r}\n    got={_norm(got)!r}"
    )
    return ink, diff


def test_pagination() -> None:
    """(a) A long document paginates: 120 paragraphs build to > 1 page."""
    texts = {
        str(i): f"Paragraph {i}: enough words on this line to consume vertical space."
        for i in range(120)
    }
    data = _FILTER.build(b"", texts)
    n_pages, _ = _pdf_text(data)
    assert n_pages > 1, f"120 paragraphs should paginate to >1 page, got {n_pages}"
    print(f"  pagination: 120 paragraphs -> {n_pages} pages")


def test_per_script_render() -> None:
    """(b) The HARD criterion (HIGH-1/HIGH-2): EVERY default-locale script renders as
    real, shaped, extractable text — measured PER SCRIPT on its own one-paragraph page
    so a blank/tofu script can never hide behind a rendered one."""
    for code, text in _SCRIPTS.items():
        ink, diff = _check_one_script(code, text)
        print(f"  script {code:8} ink={ink:.5f} tofu-diff={diff:.5f} OK")


def test_counterfactual_fallbackless_fails() -> None:
    """The per-script guards must be non-vacuous. Monkeypatch the writer's font config to
    `fallback: false` + a NONEXISTENT family and assert the ja AND ar checks FAIL. This
    exercises the REAL build path (the same _emit_units string-literal emission + compile
    as production, only the font header swapped) and produces a genuinely blank render for
    every non-Latin script on any host — so it would catch a regression that stopped the
    body from rendering, unlike an empty-body stub."""
    orig = pdf_filter._build_source

    def _broken_source(ordered):
        # Real body + compile path; only the font header is broken (no coverage, no
        # fallback). The gate proved this blanks ja AND ar on any host.
        return (
            '#set page(paper: "a4", margin: 40pt)\n'
            + pdf_filter._text_set("LocTmsNoSuchFontFamily12345", fallback=False)
            + "#set par(leading: 0.65em)\n#set smartquote(enabled: false)\n"
            + pdf_filter._emit_units(ordered)
            + "\n"
        )

    pdf_filter._build_source = _broken_source
    try:
        for code in ("ja", "ar"):
            failed = False
            try:
                _check_one_script(code, _SCRIPTS[code])
            except AssertionError:
                failed = True
            assert failed, (
                f"counterfactual: the {code} per-script check did NOT fail on a fallback-less "
                "nonexistent-family writer"
            )
    finally:
        pdf_filter._build_source = orig
    print("  counterfactual: ja + ar per-script checks correctly FAIL (real path, no fallback)")


def test_scanned_hands_png_to_ocr() -> None:
    """(c) A scanned page hands ~150-dpi PNG bytes to ocr.ocr_image (monkeypatched
    to capture). The PNG must start with the signature and be A4-at-150dpi wide
    (~1240 px)."""
    captured = {}
    orig = ocr.ocr_image

    def _capture(png_bytes, provider=None):
        captured["bytes"] = png_bytes
        captured["provider"] = provider
        return "captured scanned page"

    ocr.ocr_image = _capture
    try:
        res = _FILTER.parse(scanned_pdf(), provider="claude")
    finally:
        ocr.ocr_image = orig
    assert "bytes" in captured, "OCR was never called for a scanned (image-only) page"
    png = captured["bytes"]
    assert png[:4] == b"\x89PNG", f"OCR did not receive PNG bytes: {png[:8]!r}"
    w, _h = Image.open(io.BytesIO(png)).size
    assert 1180 <= w <= 1300, f"scanned render width {w} not ~1240 (A4 @ 150 dpi)"
    assert res.skeleton["ocr_pages"] == 1
    print(f"  scanned: OCR received {len(png)} PNG bytes, width {w}px (~150 dpi A4)")


def test_parse_thread_safety() -> None:
    """HIGH-1 (gate round 2): PDFium is single-threaded; two concurrent parses corrupt its
    global state PERMANENTLY. parse() serializes every pypdfium2 call behind _PDFIUM_LOCK
    (released around the OCR call), so N concurrent parses of a mixed text+scanned document
    all succeed, each equals the single-threaded result, AND a post-burst sequential parse
    still works. build() (typst) stays clean under threads too. The unlocked build fails
    this — proof recorded in the errata."""
    doc = mixed_pdf()

    def _stub_ocr(png_bytes, provider=None):
        return "STUB OCR TEXT for the scanned page of this document."

    orig = ocr.ocr_image
    ocr.ocr_image = _stub_ocr
    try:
        baseline = [u.text for u in _FILTER.parse(doc, provider="claude").units]
        assert baseline and any("STUB OCR" in t for t in baseline), baseline

        def _one(_i):
            try:
                got = [u.text for u in _FILTER.parse(doc, provider="claude").units]
                return got == baseline
            except Exception as e:
                return f"ERR:{type(e).__name__}:{e}"

        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(_one, range(200)))
        ok = sum(1 for r in results if r is True)
        bad = [r for r in results if r is not True]
        assert ok == 200, f"concurrent parse: {ok}/200 ok; failures: {bad[:3]}"

        # post-burst: PDFium must NOT be permanently bricked.
        seq_ok = sum(1 for _ in range(10) if [u.text for u in _FILTER.parse(doc).units] == baseline)
        assert seq_ok == 10, f"post-burst sequential parse degraded: {seq_ok}/10"

        # build() (typst) stays correct under 8 threads. Verify the produced bytes are a
        # valid PDF WITHOUT touching pypdfium2 concurrently (the output is re-parsed
        # sequentially by the other tests; here we only prove concurrent build is safe).
        def _build_one(i):
            data = _FILTER.build(b"", {"0": f"Thread build {i} with unique content."})
            return data[:5] == b"%PDF-" and len(data) > 400

        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            build_ok = sum(1 for r in ex.map(_build_one, range(64)) if r)
        assert build_ok == 64, f"concurrent build: {build_ok}/64 ok"
    finally:
        ocr.ocr_image = orig
    print("  thread-safety: 200 concurrent parses OK, post-burst 10/10, 64 concurrent builds OK")


def test_parse_page_at_a_time_memory() -> None:
    """HIGH-1 (gate round 3): parse() must OCR page-at-a-time, so only ONE scanned page's
    PNG is alive at any moment (O(1) memory). An earlier fix buffered every page's PNG
    before the first OCR call (a 100-page scan held ~228 MiB regardless of the upload
    size). Parse a 60-page dense-noise scan under tracemalloc and assert peak allocation
    stays far below the total-if-buffered (proving sub-linear, O(1) memory)."""
    import tracemalloc

    doc = scanned_noise_pdf(60)
    sizes = []
    orig = ocr.ocr_image

    def _stub(png_bytes, provider=None):
        sizes.append(len(png_bytes))
        return "stub ocr text"

    ocr.ocr_image = _stub
    try:
        tracemalloc.start()
        res = _FILTER.parse(doc, provider="claude")
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    finally:
        ocr.ocr_image = orig
    assert res.skeleton["ocr_pages"] == 60, res.skeleton["ocr_pages"]
    one_png = max(sizes)
    total = sum(sizes)
    # Buffered would hold ALL pages' PNGs (peak >= total); page-at-a-time holds one PNG
    # plus a transient render bitmap, so peak is a small O(1) figure. Pin O(1) against a
    # fixed multiple of ONE page (gate round 4 LOW-1): a regression that buffered even a
    # third of the pages would sail under a total/3 bound; six PNGs of headroom (observed
    # ~3x) does not.
    assert peak < 6 * one_png, (
        f"parse peak {peak // 1024 // 1024} MiB is not O(1): more than six pages' PNGs "
        f"(one PNG {one_png // 1024} KiB; total-if-buffered {total // 1024 // 1024} MiB over "
        f"{len(sizes)} pages)"
    )
    assert peak < total / 3, "sanity: page-at-a-time must also beat the buffered shape"

    print(
        f"  memory: 60-page scan peak {peak // 1024 // 1024} MiB << buffered "
        f"{total // 1024 // 1024} MiB (one PNG {one_png // 1024} KiB) — page-at-a-time"
    )


def test_escape_is_load_bearing() -> None:
    """The translated text must reach the PDF VERBATIM. typst string-literal emission
    means the markup parser never sees it, so every metacharacter, shorthand, and
    line-start trigger round-trips EXACTLY. The comparison COLLAPSES runs of whitespace to
    one space and strips ends (the normalization PDFium's Latin extraction genuinely needs)
    but never DELETES whitespace — so a whitespace-level mutation (MED-1: a TAB fusing two
    words) can still fail this. Guards gate round 2 HIGH-2 (`--`->en-dash, `...`->ellipsis,
    `-?` eats `?`), HIGH-3 (a leading `- `/`= ` turned into a bullet/heading), and round 3
    MED-1 (a tab rendering zero-width)."""
    cases = [
        "Run the tool with --verbose --help to see all flags.",
        "Values in the range 10--20 are valid; see the docs...",
        "well--known --- ... -? co-?operate --> <-- 1--2",
        "typst meta: #call *bold* _emph_ `code` [group] $math$ @ref ~tilde // comment",
        "path/with/slashes and quotes 'x' \"y\" and a lone backslash \\ end",
        '<b>&</b> and #read("/x") #include "y" #import "z" stay literal',
        "- leading dash not a bullet",
        "+ leading plus not an enum",
        "= leading equals not a heading",
        "  - spaces then dash",
        "\t+ tab then plus",
        "  = spaced heading",
        "1. leading enum stays",
        "1.5 million (decimal)",
        "WORD1\tWORD2\tWORD3 must not fuse (tab -> space)",
        "col1\tcol2\tcol3 tabbed values stay separated",
    ]
    for i, s in enumerate(cases):
        data = _FILTER.build(b"", {"0": s})
        _, all_text = _pdf_text(data)
        assert _norm(s) == _norm(all_text), (
            f"escape regression on case {i}:\n    src={_norm(s)!r}\n    got={_norm(all_text)!r}"
        )
    print("  escape: shorthands + metacharacters + line-start triggers + tabs round-trip EXACTLY")


def test_build_reparses_to_same_paragraphs() -> None:
    """(d) The rebuilt PDF opens with pypdfium2 and re-parses to the same content."""
    paras = {
        "0": "The quick brown fox jumps over the lazy dog.",
        "1": "A second paragraph with distinct wording for the check.",
        "2": "Troisieme paragraphe avec des accents: eleve, francais.",
    }
    data = _FILTER.build(b"", paras)
    _, all_text = _pdf_text(data)
    norm = " ".join(all_text.split())
    for k, v in paras.items():
        assert " ".join(v.split()) in norm, f"paragraph {k} not found on re-parse: {v!r}"
    print("  build->parse: rebuilt PDF re-parses to the same paragraph text")


def test_multiscript_mixed_document() -> None:
    """A single document mixing all ten scripts as separate paragraphs builds cleanly,
    paginates, and round-trips each script's text (LTR exact, RTL word-set)."""
    units = {str(i): text for i, text in enumerate(_SCRIPTS.values())}
    data = _FILTER.build(b"", units)
    n_pages, all_text = _pdf_text(data)
    assert n_pages >= 1 and data[:5] == b"%PDF-"
    page_chars = _chars(all_text)
    for code, text in _SCRIPTS.items():
        # Every character of every script's paragraph must appear in the page (full
        # multiset containment, all 10 scripts — see _check_one_script for the rationale).
        missing = _chars(text) - page_chars
        assert not missing, f"[{code}] mixed-doc characters did not round-trip: {dict(missing)}"
    print(f"  mixed document: 10 scripts on {n_pages} page(s), all round-trip in full")


def main() -> None:
    init_db()
    c = TestClient(app)
    pid = int(
        c.post(
            "/projects",
            data={
                "name": "PDF Test",
                "source_locale": "en-US",
                "target_locales": "fr-FR",
                "default_provider": "claude",
            },
            follow_redirects=False,
        )
        .headers["location"]
        .rsplit("/", 1)[-1]
    )

    # --- digital PDF: direct text extraction, no OCR ---
    d1 = upload(c, pid, "digital.pdf", text_pdf())
    page = c.get(f"/documents/{d1}?locale=fr-FR").text
    s1 = segs(d1)
    allsrc = " ".join(x.source_text for x in s1)
    assert "digital PDF" in allsrc and "Second paragraph" in allsrc, allsrc
    assert "OCR" not in page, "digital PDF should not be OCR'd"
    print(f"  digital PDF: extracted {len(s1)} segments, no OCR")

    # --- scanned PDF: OCR path (offline placeholder) ---
    d2 = upload(c, pid, "scanned.pdf", scanned_pdf())
    s2 = segs(d2)
    assert s2 and "scanned page" in " ".join(x.source_text for x in s2).lower(), [
        x.source_text for x in s2
    ]
    page2 = c.get(f"/documents/{d2}?locale=fr-FR").text
    assert "OCR'd" in page2, "scanned PDF should report OCR"
    print(f"  scanned PDF: OCR path triggered ({len(s2)} unit(s), offline placeholder)")

    # --- export rebuilds a valid translated PDF ---
    for x in s1:
        c.post(f"/segments/{x.id}/save", data={"target_text": "Texte traduit.", "tte_ms": "0"})
    r = c.get(f"/documents/{d1}/export?locale=fr-FR")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-", r.content[:20]
    n_pages, all_text = _pdf_text(r.content)
    assert n_pages >= 1 and "Texte traduit" in all_text
    print(f"  export: rebuilt a {n_pages}-page translated PDF")

    # --- issue #29: OCR spend guard ---
    test_ocr_usage_extraction_helpers()
    test_ocr_spend_guard()

    # --- filter-level checks (no HTTP): pagination, per-script render, the writer
    # --- counterfactual, scanned->PNG hand-off, escape, and build->parse fidelity ---
    test_pagination()
    test_per_script_render()
    test_counterfactual_fallbackless_fails()
    test_scanned_hands_png_to_ocr()
    test_parse_thread_safety()
    test_parse_page_at_a_time_memory()
    test_escape_is_load_bearing()
    test_build_reparses_to_same_paragraphs()
    test_multiscript_mixed_document()

    print("PDF OK")


if __name__ == "__main__":
    main()
