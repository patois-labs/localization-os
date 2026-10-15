"""Per-string image -> vision-model translation context -- Batch 14 Train T2.

The vision injection core + capability resolver + the per-step use_image_context toggle
(SPEC-string-image-context.md, section-17 T2 list + section-25). Hermetic + offline (no
SDK, no key, throwaway data dir via tests._bootstrap.init_env). Every image send is
captured on a Provider DOUBLE registered under a vision NAME (V-3: is_vision_capable keys
off the requested name, so a mock under "claude" is treated capable), so we can assert the
full resolve -> thread -> provider path with no network.

Coverage:
  [A] provider funnel: byte-identical no-image path per provider (LAW-IMG-7); correct
      content-block shape per provider (captured, no SDK); run_completion images_sent +
      per-target capability + attempt-0/M-3 usage allowance; MockProvider.last_images
      (deterministic + spend-exempt, output unchanged, M-11).
  [B] capability resolver matrix (claude/openai/gemini capable; local chat-path capable;
      qwen3.5 adapter NOT; engine:<id> org-scoped; cohere/NMT not) -- V-3 name-keyed.
  [C] orchestrator end-to-end over a keys project: PRESENCE + exactly-one skip note on a
      non-vision PRIMARY (qwen3.5 adapter / mock / vision->text-fallback), no double-count,
      no silent absence (T-9); a vision engine sends N (last_images); send_all_images
      raises the cap; multi-chunk sends on chunk 0 only (N-2); the three QE engine combos
      (D2-QE-ACCOUNTING).
  [D] the section-25 toggle: opted-out vs opted-in sibling in ONE run; opted-out-only on a
      VISION engine -> ZERO skip notes (M-2 false-note guard); opted-in on non-vision ->
      exactly one; QE judge of an opted-out step gets no image; default TRUE after migration
      AND fresh add_step; round-trips web/api/service; clone/copy preserve False; a
      non-LLM_AGENT step ignores the flag; the mandatory builder "image context off" badge
      (rendered-page shape).

ASCII-only output (Windows consoles).
"""

import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env
from tests._piece_patch import set_pieces

init_env("loctms_keyimgvis_")

import json as _json

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import (
    Document,
    GlossaryEntry,
    KeyImage,
    Project,
    ProjectType,
    Segment,
    SegStatus,
    StepRun,
    StepType,
    StringKey,
    TransMode,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services import content, key_images, orchestrator, refusal, settings, tm, workflows
from app.services import providers as P
from app.services.providers import (
    Provider,
    ProviderSpec,
    _chat_messages,
    claude_image_content,
    gemini_image_contents,
    is_vision_capable,
    openai_image_content,
    register_provider,
    run_completion,
)

init_db()

_SEQ = [0]


def _uniq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _ok(msg):
    print("  [OK]", msg)


def _png(w=24, h=24, color=(200, 100, 50)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# A capturing provider double. NON-mock is not required (we register it under a
# vision NAME, and the capability verdict keys off that name), but it must be
# spend-exempt-agnostic and deterministic. It records the images COUNT of the
# most recent call on `last_images`, returns the [locale] text translate
# convention, and can be flagged to raise (the failover probe).
# ---------------------------------------------------------------------------
class VisionDouble(Provider):
    name = "visiondouble"

    def __init__(self, raise_on_translate=False):
        self.last_images = 0
        self.last_task = None
        self.raise_on_translate = raise_on_translate
        self.last_usage = None

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        self.last_images = len(images) if images else 0
        self.last_task = task
        self.last_usage = {
            "input_tokens": max(1, len(prompt or "") // 4),
            "output_tokens": 4,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        payload = payload or {}
        if task == "translate":
            if self.raise_on_translate:
                raise RuntimeError("simulated vision engine outage")
            return f"[{payload.get('target', 'xx')}] {payload.get('text', prompt or '')}"
        if task == "qe":
            # A clean MQM card (score 100) so the gate passes; records last_images too.
            return _json.dumps(
                {"accuracy": 0, "fluency": 0, "terminology": 0, "style": 0, "rationale": "clean"}
            )
        return (prompt or "").strip()


# A provider that reports NO usage (like openai_compatible/cohere without a usage body),
# to exercise the M-3 image token allowance in the run_completion usage fallback.
class NoUsageVisionDouble(Provider):
    name = "nousagevision"

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        self.last_usage = None  # force the length-based fallback
        payload = payload or {}
        return f"[{payload.get('target', 'xx')}] {payload.get('text', '')}"


def _reset_overrides():
    P._OVERRIDES.clear()


# ===========================================================================
# [A] provider funnel + block shapes + run_completion
# ===========================================================================
def test_no_image_path_byte_identical():
    """LAW-IMG-7: images=None keeps every builder's message byte-identical. The shared
    chat-messages builder is the observable proxy (each provider builds its own wire body
    from these)."""
    a = _chat_messages("hello", "sys")
    b = _chat_messages("hello", "sys", None)
    c = _chat_messages("hello", "sys", [])
    if not (a == b == c):
        _fail("no-image _chat_messages diverged")
    # user content is the FLAT string when no images
    if a[-1]["content"] != "hello":
        _fail("no-image user content is not the flat prompt string")
    _ok("no-image path byte-identical (flat string user content)")


def test_block_shapes():
    """Correct per-provider content-block shape (captured, no SDK/key). SPEC section-2.5/6.1."""
    imgs = [b"AAA", b"BBB"]
    # Claude: image block(s) THEN text block.
    cc = claude_image_content("PROMPT", imgs)
    if not (
        len(cc) == 3
        and cc[0]["type"] == "image"
        and cc[0]["source"]["type"] == "base64"
        and cc[0]["source"]["media_type"] == "image/png"
        and cc[-1] == {"type": "text", "text": "PROMPT"}
    ):
        _fail(f"claude image content shape wrong: {cc!r}")
    # OpenAI/local: text block THEN image_url data-URI block(s).
    oc = openai_image_content("PROMPT", imgs)
    if not (
        oc[0] == {"type": "text", "text": "PROMPT"}
        and len(oc) == 3
        and oc[1]["type"] == "image_url"
        and oc[1]["image_url"]["url"].startswith("data:image/png;base64,")
    ):
        _fail(f"openai image content shape wrong: {oc!r}")
    # _chat_messages threads that shape onto the user message.
    msgs = _chat_messages("PROMPT", "sys", imgs)
    if not (isinstance(msgs[-1]["content"], list) and msgs[-1]["content"][0]["type"] == "text"):
        _fail("_chat_messages image content not a block list")
    # Gemini: Part(image) THEN prompt (types import may be unavailable offline -> tolerate).
    try:
        gc = gemini_image_contents("PROMPT", imgs)
        if gc[-1] != "PROMPT" or len(gc) != 3:
            _fail(f"gemini contents shape wrong: {gc!r}")
    except Exception:
        pass  # google.genai not installed offline; the shape is exercised where it is
    _ok("per-provider content-block shapes correct (claude/openai/gemini)")


def test_capability_resolver_matrix():
    m = is_vision_capable
    checks = {
        ("claude", None): True,
        ("openai", None): True,
        ("gemini", None): True,
        ("mock", None): False,
        ("deepl", None): False,
        ("cohere", None): False,
        ("google", None): False,
        ("microsoft", None): False,
    }
    for (ref, model), want in checks.items():
        if m(ref, model=model) is not want:
            _fail(f"is_vision_capable({ref!r}) != {want!r}")
    # local chat-path model capable; qwen3.5 ChatML adapter NOT.
    if not m("local", model="qwen3-vl-7b"):
        _fail("local chat-path vision model should be capable")
    if m("local", model="qwen3.5-9b"):
        _fail("qwen3.5 ChatML adapter must NOT be vision-capable")
    if m("local", model="qwen3.8-27b"):
        _fail("qwen3.8 ChatML adapter must NOT be vision-capable")
    # A ProviderSpec is accepted too.
    if not m(ProviderSpec(provider="claude")):
        _fail("is_vision_capable(spec) claude should be True")
    # engine:<id> nonexistent -> not capable (non-enumerable).
    if m("engine:999999"):
        _fail("nonexistent engine ref should be non-capable")
    _ok("capability resolver matrix (V-3 name-keyed; adapter excluded; engine-scoped)")


def test_run_completion_images_sent_and_last_images():
    _reset_overrides()
    dbl = VisionDouble()
    register_provider("claude", dbl)
    try:
        spec = ProviderSpec(provider="claude", retry_max=0)
        res = run_completion(
            spec, "p", task="translate", payload={"text": "hi", "target": "es"}, images=[b"X", b"Y"]
        )
        if not res["images_sent"]:
            _fail("vision target should report images_sent True")
        if dbl.last_images != 2:
            _fail(f"vision double should have received 2 images, got {dbl.last_images!r}")
        # Non-vision target: mock gets images=None, images_sent False.
        res2 = run_completion(
            ProviderSpec(provider="mock", retry_max=0),
            "p",
            task="translate",
            payload={"text": "hi", "target": "es"},
            images=[b"X"],
        )
        if res2["images_sent"]:
            _fail("non-vision target must report images_sent False")
        # No images -> images_sent False, no capture.
        dbl.last_images = 0
        res3 = run_completion(
            spec, "p", task="translate", payload={"text": "hi", "target": "es"}, images=None
        )
        if res3["images_sent"] or dbl.last_images != 0:
            _fail("no-image call must not send / capture")
    finally:
        _reset_overrides()
    _ok("run_completion per-target capability + images_sent + last_images")


def test_authorized_engine_capability_read_failure_propagates():
    """Reviewer round 2 MED-1: provider construction may read a live profile successfully, but a
    transient failure in the authorized capability read must stop before ``complete``. It must
    never be reclassified as an ordinary text-only engine and called with ``images=None``."""
    from app.services import engines

    profile = engines.create_profile(
        f"astra-r2-read-race-{_uniq()}",
        "local",
        base_url="http://127.0.0.1:1/v1",
        model="qwen3-vl-7b",
    )
    calls = []
    reads = []

    class Capture(Provider):
        name = "astra-r2-capture"

        def complete(self, prompt, images=None, **kwargs):
            calls.append(images)
            self.last_usage = {
                "input_tokens": 1,  # pregate: allow-secret usage counter fixture
                "output_tokens": 1,  # pregate: allow-secret usage counter fixture
            }
            return "translated"

    real_factory = P._provider_for_profile
    real_get = engines.get_profile

    def _second_read_boom(profile_id):
        reads.append(profile_id)
        if len(reads) == 2:
            raise RuntimeError("transient second profile read failure")
        return real_get(profile_id)

    set_pieces("app.services.providers", "_provider_for_profile", lambda _: Capture())
    engines.get_profile = _second_read_boom
    try:
        try:
            run_completion(
                ProviderSpec(provider=f"engine:{profile.id}", retry_max=0),
                "translate",
                task="translate",
                payload={"text": "Book", "target": "es"},
                images=[b"image"],
            )
        except RuntimeError as exc:
            if str(exc) != "transient second profile read failure":
                _fail(f"authorized capability read propagated the wrong error: {exc!r}")
        else:
            _fail(
                "authorized capability profile-read failure was swallowed: "
                f"reads={reads}, calls={calls}"
            )
    finally:
        engines.get_profile = real_get
        set_pieces("app.services.providers", "_provider_for_profile", real_factory)
    if reads != [profile.id, profile.id]:
        _fail(f"expected construction read then capability read, got {reads}")
    if calls:
        _fail(f"provider ran after capability profile-read failure: {calls}")
    _ok("Reviewer r2: authorized capability profile-read failure propagates before provider call")


def test_run_completion_vision_primary_failover_records_no_send():
    _reset_overrides()
    erroring = VisionDouble(raise_on_translate=True)
    register_provider("claude", erroring)
    try:
        spec = ProviderSpec(provider="claude", fallback_provider="mock", retry_max=1)
        res = run_completion(
            spec, "p", task="translate", payload={"text": "hi", "target": "es"}, images=[b"X"]
        )
        # The SUCCESSFUL call was the text-only mock fallback -> images_sent False.
        if res["images_sent"]:
            _fail("vision-primary->text-fallback must report images_sent False")
        if res["provider_used"] != "mock":
            _fail(f"expected failover to mock, got {res['provider_used']!r}")
    finally:
        _reset_overrides()
    _ok("vision-primary error -> text-only fallback reports images_sent False (LAW-IMG-4)")


def test_token_estimate_constant_in_sync():
    # providers.KEY_IMAGE_TOKEN_ESTIMATE is a plain literal (NOT imported from key_images,
    # to keep providers in a config-only import graph, spec HIGH-1); pin the two equal so a
    # future change to the T1 store constant cannot silently drift the spend allowance.
    if P.KEY_IMAGE_TOKEN_ESTIMATE != key_images.KEY_IMAGE_TOKEN_ESTIMATE:
        _fail("providers vs key_images KEY_IMAGE_TOKEN_ESTIMATE drifted")
    _ok("providers.KEY_IMAGE_TOKEN_ESTIMATE stays in sync with the T1 store constant")


def test_m3_usage_allowance():
    _reset_overrides()
    register_provider("claude", NoUsageVisionDouble())
    try:
        spec = ProviderSpec(provider="claude", retry_max=0)
        base = run_completion(
            spec, "prompt", task="translate", payload={"text": "hi", "target": "es"}, images=None
        )
        withimg = run_completion(
            spec,
            "prompt",
            task="translate",
            payload={"text": "hi", "target": "es"},
            images=[b"X"],
        )
        delta = withimg["usage"]["input_tokens"] - base["usage"]["input_tokens"]
        if delta != key_images.KEY_IMAGE_TOKEN_ESTIMATE:
            _fail(f"M-3 image token allowance not applied (delta={delta!r})")
    finally:
        _reset_overrides()
    _ok("M-3 usage-fallback adds a per-image token allowance")


def test_mock_last_images_deterministic_and_exempt():
    from app.services import spend
    from app.services.providers import MockProvider

    m = MockProvider()
    out1 = m.complete("hi", task="translate", payload={"text": "hi", "target": "es"})
    out2 = m.complete("hi", task="translate", payload={"text": "hi", "target": "es"}, images=[b"A"])
    if out1 != out2:
        _fail("MockProvider output changed when images were present (M-11)")
    if m.last_images != 1:
        _fail("MockProvider.last_images not recorded")
    if not spend.is_exempt(m):
        _fail("MockProvider must stay spend-exempt")
    _ok("MockProvider.last_images deterministic, output unchanged, spend-exempt (M-11)")


# ===========================================================================
# End-to-end scaffolding over a KEY-based project (Segments carry key_id).
# ===========================================================================
def _mk_key_project(default_provider="mock", default_model="") -> int:
    with Session(engine) as s:
        p = content.project_create(
            s,
            f"KIV {_uniq()}",
            source_locale="en-US",
            target_locales="es-ES",
            project_type=ProjectType.KEY_BASED,
        )
        proj = s.get(Project, p.id)
        proj.default_provider = default_provider
        if default_model:
            proj.default_model = default_model
        s.add(proj)
        s.commit()
        return p.id


def _add_key(pid, name) -> int:
    # Create the StringKey IDENTITY directly (no internal key-document segments), so the
    # test can build its OWN segmented Document whose segment carries this key_id without
    # colliding with the internal per-key segment ux_segment_key_locale_form unique index.
    with Session(engine) as s:
        k = StringKey(project_id=pid, namespace="app", name=name, context="")
        s.add(k)
        s.commit()
        s.refresh(k)
        return k.id


def _attach(key_id, data=None, send_all=False):
    data = data if data is not None else _png()
    with Session(engine) as s:
        key = s.get(StringKey, key_id)
        img = key_images.attach_image(s, key, data, uploaded_by="fixture@x.com")
        if send_all:
            row = s.get(KeyImage, img.id)
            row.send_all_images = True
            s.add(row)
            s.commit()
        return img.id


def _mk_doc_with_keys(pid, key_ids, sources=None, locale="es-ES") -> int:
    """A plain SEGMENTED document in the key project whose segments carry key_id -- the
    exact shape orchestrator._images_for resolves (segments[j].key_id + doc.project_id)."""
    with Session(engine) as s:
        d = Document(project_id=pid, filename="d.txt", mode=TransMode.SEGMENTED)
        s.add(d)
        s.commit()
        s.refresh(d)
        for i, kid in enumerate(key_ids):
            src = sources[i] if sources else f"Source string {i}"
            s.add(
                Segment(
                    document_id=d.id,
                    target_locale=locale,
                    order_index=i,
                    source_text=src,
                    key_id=kid,
                    status=SegStatus.NEW,
                )
            )
        s.commit()
        return d.id


def _mk_workflow(steps) -> int:
    with Session(engine) as s:
        wf = Workflow(name=f"KIVWF {_uniq()}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        for i, cols in enumerate(steps):
            kw = {
                "workflow_id": wf.id,
                "order_index": i,
                "retry_max": 0,
                "backoff_base": 0.0,
                "provider": "mock",
                "step_type": StepType.LLM_AGENT,
                "name": f"s{i}",
                "prompt_text": "Translate: {text}",
                "tm_examples": False,
            }
            kw.update(cols)
            s.add(WorkflowStep(**kw))
        s.commit()
        return wf.id


def _run(wf_id, doc_id, locale="es-ES"):
    with Session(engine) as s:
        return list(orchestrator.run_workflow_stream_segmented(s, wf_id, doc_id, locale))


def _mk_fulltext_doc(body="Hello world.", locale="es-ES") -> int:
    """A minimal FULL_TEXT document (one Segment holding the whole body) in a DOCUMENT
    project, so the FULL_TEXT StepRun stamping seam (orchestrator.run_workflow_stream) can be
    exercised alongside the segmented one."""
    with Session(engine) as s:
        p = content.project_create(
            s,
            f"KIV-FT {_uniq()}",
            source_locale="en-US",
            target_locales=locale,
            project_type=ProjectType.DOCUMENT,
        )
        proj = s.get(Project, p.id)
        proj.default_provider = "mock"
        s.add(proj)
        s.commit()
        # A FULL_TEXT doc reads its source from doc.skeleton["units"] (orchestrator._source_text)
        # and commits the produced text to the single Segment carrying the whole body -- the
        # exact shape ingest_document builds.
        d = Document(
            project_id=p.id,
            filename="d.txt",
            mode=TransMode.FULL_TEXT,
            skeleton={"units": [{"text": body}]},
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        s.add(
            Segment(
                document_id=d.id,
                target_locale=locale,
                order_index=0,
                source_text=body,
                status=SegStatus.NEW,
            )
        )
        s.commit()
        return d.id


def _run_fulltext(wf_id, doc_id, locale="es-ES"):
    with Session(engine) as s:
        return list(orchestrator.run_workflow_stream(s, wf_id, doc_id, locale))


def _done(events):
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "done":
            return ev
    _fail("no done event in run")


def _skip_count(events):
    d = _done(events)
    return (d.get("image_context") or {}).get("skipped", 0)


def _switch(on: bool):
    settings.set_many({"key_image_context_enabled": "1" if on else "0"})
    settings.invalidate_cache()


# ===========================================================================
# [C] orchestrator end-to-end
# ===========================================================================
def test_vision_engine_sends_images():
    _reset_overrides()
    _switch(True)
    dbl = VisionDouble()
    register_provider("claude", dbl)
    try:
        pid = _mk_key_project()
        k = _add_key(pid, "k::vis")
        _attach(k)
        doc = _mk_doc_with_keys(pid, [k])
        wf = _mk_workflow([{"provider": "claude"}])
        events = _run(wf, doc)
        if dbl.last_images != 1:
            _fail(f"vision engine should have received 1 image, got {dbl.last_images!r}")
        if _skip_count(events) != 0:
            _fail("a vision engine that sent the image must record NO skip")
    finally:
        _reset_overrides()
        _switch(True)
    _ok("vision engine sends the image (last_images) with no skip note")


def test_send_all_images_raises_cap():
    _reset_overrides()
    _switch(True)
    dbl = VisionDouble()
    register_provider("claude", dbl)
    try:
        pid = _mk_key_project()
        k = _add_key(pid, "k::all")
        _attach(k, _png(color=(1, 1, 1)), send_all=True)
        _attach(k, _png(color=(2, 2, 2)))
        doc = _mk_doc_with_keys(pid, [k])
        wf = _mk_workflow([{"provider": "claude"}])
        _run(wf, doc)
        if dbl.last_images != 2:
            _fail(f"send_all_images should raise the per-request cap to 2, got {dbl.last_images!r}")
    finally:
        _reset_overrides()
    _ok("send_all_images raises the per-request cap")


def test_skip_note_nonvision_primary():
    """T-9: a string-with-image on a non-vision PRIMARY records EXACTLY ONE skip (presence
    resolved, no target sent) -- no silent absence, no double-count. Probe mock, the
    qwen3.5 ChatML adapter, and a vision-primary->text fallback."""
    _switch(True)
    # (a) mock primary (non-vision built-in).
    _reset_overrides()
    pid = _mk_key_project()
    k = _add_key(pid, "k::mock")
    _attach(k)
    doc = _mk_doc_with_keys(pid, [k])
    wf = _mk_workflow([{"provider": "mock"}])
    if _skip_count(_run(wf, doc)) != 1:
        _fail("mock primary: expected exactly one skip note")
    # (b) qwen3.5 ChatML adapter as a local primary.
    _reset_overrides()
    register_provider("local", VisionDouble())  # name is local, but model is an adapter id
    try:
        pid = _mk_key_project(default_provider="local")
        k = _add_key(pid, "k::qwen")
        _attach(k)
        doc = _mk_doc_with_keys(pid, [k])
        wf = _mk_workflow([{"provider": "local", "model": "qwen3.5-9b"}])
        if _skip_count(_run(wf, doc)) != 1:
            _fail("qwen3.5 adapter primary: expected exactly one skip note (T-9)")
    finally:
        _reset_overrides()
    # (c) vision-primary that errors -> text-only fallback.
    _reset_overrides()
    register_provider("claude", VisionDouble(raise_on_translate=True))
    try:
        pid = _mk_key_project()
        k = _add_key(pid, "k::fo")
        _attach(k)
        doc = _mk_doc_with_keys(pid, [k])
        wf = _mk_workflow([{"provider": "claude", "fallback_provider": "mock", "retry_max": 0}])
        if _skip_count(_run(wf, doc)) != 1:
            _fail("vision-primary->text fallback: expected exactly one skip note (T-9)")
    finally:
        _reset_overrides()
    _ok("non-vision primary records exactly one skip note (mock/qwen3.5/failover, T-9)")


def test_multichunk_sends_chunk0_only():
    """N-2: a key value that splits into >1 chunk sends images on chunk 0 only. Direct
    _run_step_on_segment unit test with a tiny chunk cap so the value splits."""
    _reset_overrides()
    calls = []

    class ChunkSpy(Provider):
        name = "chunkspy"

        def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
            calls.append(len(images) if images else 0)
            self.last_usage = None
            return f"[{(payload or {}).get('target', 'xx')}] {(payload or {}).get('text', '')}"

    register_provider("claude", ChunkSpy())
    try:
        spec = ProviderSpec(provider="claude", retry_max=0)
        step = WorkflowStep(
            workflow_id=1,
            order_index=0,
            step_type=StepType.LLM_AGENT,
            provider="claude",
            prompt_text="Translate: {text}",
            chunk_policy="auto",
        )
        # Two paragraphs (split on \n\n), each > MAX_CHUNK_TOKENS/2, so the value splits
        # into >= 2 chunks and we can prove images ride only chunk 0.
        _para = "word " * 4000
        long_src = _para + "\n\n" + _para
        res = orchestrator._run_step_on_segment(
            spec,
            "Translate: {text}",
            {"source_locale": "en-US"},
            step,
            long_src,
            long_src,
            "es-ES",
            [],
            images=[b"IMG"],
        )
        if len(calls) < 2:
            _fail(f"value did not split into multiple chunks; test setup invalid ({calls!r})")
        if calls[0] != 1:
            _fail("chunk 0 must carry the image")
        if any(c != 0 for c in calls[1:]):
            _fail(f"chunks after 0 must carry NO image (N-2): {calls!r}")
        if not res["images_present"] or not res["images_sent"]:
            _fail("leg should report images_present + images_sent True")
    finally:
        _reset_overrides()
    _ok("multi-chunk value sends images on chunk 0 only (N-2)")


def test_qe_engine_combos():
    """D2-QE-ACCOUNTING: qe.score_segment carries images ONLY when the segment's generation
    images_sent. Three combos: (a) vision-gen + vision-qe -> both see, no skip; (b)
    nonvision-gen + vision-qe -> QE gets NO image, skip recorded; (c) both nonvision -> no
    image anywhere, skip."""
    _switch(True)

    # (a) vision generation + vision QE.
    _reset_overrides()
    gen = VisionDouble()
    qedbl = VisionDouble()
    register_provider("claude", gen)
    register_provider("openai", qedbl)
    try:
        pid = _mk_key_project()
        k = _add_key(pid, "k::qa")
        _attach(k)
        doc = _mk_doc_with_keys(pid, [k])
        wf = _mk_workflow(
            [{"provider": "claude", "qe_gate": True, "qe_provider": "openai", "qe_threshold": 50}]
        )
        events = _run(wf, doc)
        if gen.last_images != 1:
            _fail("(a) generation should have sent the image")
        if qedbl.last_images != 1:
            _fail("(a) vision QE should have inherited the image")
        if _skip_count(events) != 0:
            _fail("(a) no skip expected when both saw the image")
    finally:
        _reset_overrides()

    # (b) non-vision generation (mock) + vision QE (openai) -> QE must NOT egress.
    _reset_overrides()
    qedbl = VisionDouble()
    register_provider("openai", qedbl)
    try:
        pid = _mk_key_project()
        k = _add_key(pid, "k::qb")
        _attach(k)
        doc = _mk_doc_with_keys(pid, [k])
        wf = _mk_workflow(
            [{"provider": "mock", "qe_gate": True, "qe_provider": "openai", "qe_threshold": 50}]
        )
        events = _run(wf, doc)
        if qedbl.last_images != 0:
            _fail(
                "(b) vision QE must NOT get the image when generation skipped (no surprise egress)"
            )
        if _skip_count(events) != 1:
            _fail("(b) exactly one skip expected (generation-scoped)")
    finally:
        _reset_overrides()

    # (c) both non-vision -> no image anywhere, skip recorded.
    _reset_overrides()
    pid = _mk_key_project()
    k = _add_key(pid, "k::qc")
    _attach(k)
    doc = _mk_doc_with_keys(pid, [k])
    wf = _mk_workflow([{"provider": "mock", "qe_gate": True, "qe_threshold": 50}])
    if _skip_count(_run(wf, doc)) != 1:
        _fail("(c) both non-vision: exactly one skip expected")
    _ok("QE engine combos: judge inherits the generation send verdict (D2-QE-ACCOUNTING)")


# ===========================================================================
# [D] the section-25 use_image_context toggle
# ===========================================================================
def test_toggle_sibling_steps_one_run():
    """One run over the SAME string: an opted-OUT LLM_AGENT step gets images=None while a
    sibling opted-IN step carries the image. Vision engine, so no skip note fires from the
    opted-out step (its images_present is False, section-25.4)."""
    _reset_overrides()
    _switch(True)
    on_dbl = VisionDouble()
    off_dbl = VisionDouble()
    register_provider("claude", on_dbl)
    register_provider("openai", off_dbl)
    try:
        pid = _mk_key_project()
        k = _add_key(pid, "k::sib")
        _attach(k)
        doc = _mk_doc_with_keys(pid, [k])
        wf = _mk_workflow(
            [
                {"provider": "claude", "use_image_context": True},
                {"provider": "openai", "use_image_context": False},
            ]
        )
        events = _run(wf, doc)
        if on_dbl.last_images != 1:
            _fail("opted-IN step should carry the image")
        if off_dbl.last_images != 0:
            _fail("opted-OUT step must get images=None")
        # Both engines are vision-capable; the only image-carrying step sent it, and the
        # opted-out step is excluded from accounting -> ZERO skip notes (M-2 false-note guard).
        if _skip_count(events) != 0:
            _fail("opted-out sibling on a vision engine must not raise a phantom skip")
    finally:
        _reset_overrides()
    _ok("toggle: opted-out vs opted-in sibling in one run; no phantom skip (section-25.4)")


def test_opted_out_only_on_vision_zero_skips():
    """M-2: the opted-out-only string on a VISION engine records ZERO skip notes -- the
    naive images_present-from-key-attachment design would wrongly flag 'engine not
    vision-capable' here (the engine CAN see images; the author turned them off)."""
    _reset_overrides()
    _switch(True)
    dbl = VisionDouble()
    register_provider("claude", dbl)
    try:
        pid = _mk_key_project()
        k = _add_key(pid, "k::oo")
        _attach(k)
        doc = _mk_doc_with_keys(pid, [k])
        wf = _mk_workflow([{"provider": "claude", "use_image_context": False}])
        events = _run(wf, doc)
        if dbl.last_images != 0:
            _fail("opted-out step must not send the image")
        if _skip_count(events) != 0:
            _fail("opted-out-only on a vision engine must record ZERO skip notes (M-2)")
    finally:
        _reset_overrides()
    _ok("opted-out-only on a VISION engine -> ZERO skip notes (M-2 false-note guard)")


def test_opted_in_on_nonvision_still_skips():
    _reset_overrides()
    _switch(True)
    pid = _mk_key_project()
    k = _add_key(pid, "k::in")
    _attach(k)
    doc = _mk_doc_with_keys(pid, [k])
    wf = _mk_workflow([{"provider": "mock", "use_image_context": True}])
    if _skip_count(_run(wf, doc)) != 1:
        _fail("opted-in on a non-vision engine must still record exactly one skip")
    _ok("opted-in on a non-vision engine still records one skip (toggle didn't suppress)")


def test_default_true_after_migration_and_fresh_create():
    # Fresh add_step defaults TRUE.
    with Session(engine) as s:
        wf = Workflow(name=f"dft {_uniq()}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        st = workflows.add_step(s, wf.id, name="t", step_type=StepType.LLM_AGENT)
        if st.use_image_context is not True:
            _fail("fresh add_step must default use_image_context True")
    # A legacy-shaped row (built without the field via the ORM default) reads True.
    st2 = WorkflowStep(workflow_id=1, order_index=0, step_type=StepType.LLM_AGENT)
    if st2.use_image_context is not True:
        _fail("model default must be True (migration server_default '1' parity)")
    _ok("use_image_context defaults TRUE (fresh add_step + model/migration default)")


def test_roundtrip_service_and_api():
    # Service round-trip: set False -> read False.
    with Session(engine) as s:
        wf = Workflow(name=f"rt {_uniq()}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        st = workflows.add_step(s, wf.id, name="t", step_type=StepType.LLM_AGENT)
        workflows.update_step(s, st.id, use_image_context=False)
        s.refresh(st)
        if st.use_image_context is not False:
            _fail("service update to False did not persist")
        workflows.update_step(s, st.id, use_image_context=True)
        s.refresh(st)
        if st.use_image_context is not True:
            _fail("service update back to True did not persist")
    # /api round-trip via TestClient with an api-scoped token (the /api gate needs one).
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import apikeys, security

    u = security.create_user(f"kiv-api-{_uniq()}@x.com", "KIV", "admin", "kivpass123456")
    _row, tok = apikeys.create_key(u.id, "kiv", scopes="api docs")
    hdr = {"X-Loc-Token": tok}
    c = TestClient(app)
    with Session(engine) as s:
        wf = Workflow(name=f"rtapi {_uniq()}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        wid = wf.id
    r = c.post(
        f"/api/workflows/{wid}/steps",
        headers=hdr,
        json={
            "step_type": "llm_agent",
            "prompt_text": "Translate: {text}",
            "use_image_context": False,
        },
    )
    if r.status_code not in (200, 201):
        _fail(f"/api add-step failed: {r.status_code} {r.text}")
    show = c.get(f"/api/workflows/{wid}", headers=hdr).json()
    row = show["steps"][0]
    if "use_image_context" not in row:
        _fail("/api workflow show step row missing use_image_context")
    if row["use_image_context"] is not False:
        _fail(f"/api round-trip did not persist False: {row.get('use_image_context')!r}")
    # A MISSING key on /api add-step resolves to the ratified default TRUE.
    r2 = c.post(
        f"/api/workflows/{wid}/steps",
        headers=hdr,
        json={"step_type": "llm_agent", "prompt_text": "Translate: {text}"},
    )
    if r2.status_code not in (200, 201):
        _fail(f"/api add-step (default) failed: {r2.text}")
    show2 = c.get(f"/api/workflows/{wid}", headers=hdr).json()
    if show2["steps"][1]["use_image_context"] is not True:
        _fail("/api missing key must resolve to default TRUE")
    _ok("round-trip: service + /api set/read False; missing key -> default TRUE")


def test_clone_and_copy_preserve():
    with Session(engine) as s:
        wf = Workflow(
            name=f"cl {_uniq()}", scope="global", source_locale="en-US", target_locale="es-ES"
        )
        s.add(wf)
        s.commit()
        s.refresh(wf)
        workflows.add_step(
            s, wf.id, name="t", step_type=StepType.LLM_AGENT, use_image_context=False
        )
        cl = workflows.clone_workflow(s, wf.id)
        rows = s.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == cl.id)).all()
        if [r.use_image_context for r in rows] != [False]:
            _fail("clone_workflow did not preserve use_image_context False")
    _ok("clone_workflow preserves use_image_context (copy_to_pairs pinned by clone-parity suite)")


def test_non_llm_agent_ignores_flag():
    """A non-LLM_AGENT step ignores the flag: setting False on an LQA step is inert
    (accepted, not refused) AND the eligibility predicate excludes it by type regardless."""
    with Session(engine) as s:
        wf = Workflow(name=f"lqa {_uniq()}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        # No WorkflowError raised (the tm_examples dead-field posture, section-25.5-A).
        st = workflows.add_step(
            s, wf.id, name="check", step_type=StepType.LQA, use_image_context=False
        )
        if orchestrator._image_eligible_step(st):
            _fail("an LQA step must never be image-eligible regardless of the flag")
        # And a TRUE flag on an LQA step is equally inert.
        st2 = WorkflowStep(
            workflow_id=wf.id, order_index=1, step_type=StepType.LQA, use_image_context=True
        )
        if orchestrator._image_eligible_step(st2):
            _fail("an LQA step with the flag True must still be excluded by type")
    _ok("non-LLM_AGENT step ignores the flag (inert on add, excluded by type)")


def test_dynamic_attach_capability_warning():
    """F-1 / SPEC section-7.2: the DYNAMIC attach-time capability warning. On a project whose
    configured engine is NOT vision-capable the keys grid escalates the static callout to a
    visible warning; on a vision-capable engine the warning is ABSENT (UI agrees with the
    machine-side is_vision_capable predicate). Rendered-page shape (mirrors
    verify_package_view.test_web_panel_renders_facts)."""
    from fastapi.testclient import TestClient

    from app.main import app

    _switch(True)
    c = TestClient(app)
    needle = "cannot see images, so images you attach here"
    # Text-only engine (deepl NMT) -> warning present.
    pid_text = _mk_key_project(default_provider="deepl")
    r_text = c.get(f"/projects/{pid_text}/keys")
    if r_text.status_code != 200:
        _fail(f"keys grid GET (text engine) failed: {r_text.status_code}")
    if needle not in r_text.text:
        _fail("non-vision engine must escalate the dynamic attach warning")
    # Vision engine (claude) -> warning ABSENT (static callout still shown).
    pid_vis = _mk_key_project(default_provider="claude")
    r_vis = c.get(f"/projects/{pid_vis}/keys")
    if r_vis.status_code != 200:
        _fail(f"keys grid GET (vision engine) failed: {r_vis.status_code}")
    if needle in r_vis.text:
        _fail("a vision-capable engine must NOT show the non-vision warning")
    # Feature-off -> neither the static callout nor the dynamic warning renders.
    _switch(False)
    try:
        r_off = c.get(f"/projects/{pid_text}/keys")
        if needle in r_off.text:
            _fail("feature-off must not render the capability warning")
    finally:
        _switch(True)
    _ok("dynamic attach-time capability warning: present on non-vision, absent on vision (F-1)")


def test_builder_badge_rendered_page():
    """M-4 mandatory human-layer signal: the rendered builder page shows the 'image context
    off' badge for an opted-out LLM_AGENT step and NOT for an opted-in one (rendered-page
    shape, mirroring verify_package_view.test_web_panel_renders_facts)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with Session(engine) as s:
        wf = Workflow(name=f"badge {_uniq()}", scope="global")
        s.add(wf)
        s.commit()
        s.refresh(wf)
        workflows.add_step(
            s,
            wf.id,
            name="translator",
            step_type=StepType.LLM_AGENT,
            prompt_text="Translate: {text}",
            use_image_context=True,
        )
        workflows.add_step(
            s,
            wf.id,
            name="finalizer",
            step_type=StepType.LLM_AGENT,
            prompt_text="Polish: {text}",
            use_image_context=False,
        )
        wid = wf.id
    c = TestClient(app)
    r = c.get(f"/workflows/{wid}")
    if r.status_code != 200:
        _fail(f"builder GET failed: {r.status_code}")
    n = r.text.count("image context off")
    if n != 1:
        _fail(f"expected exactly ONE 'image context off' badge (opted-out step only), got {n}")
    _ok("builder renders the 'image context off' badge only for the opted-out step (M-4)")


# ===========================================================================
# run-190 Item D: over-translation (page-dump) HARD guard on the classic llm_agent path
# ===========================================================================
_PAGE_DUMP = "| en | es |\n|---|---|\n| Book | Reservar |\n| Cancel | Cancelar |"


class _PageDump(Provider):
    """A model that returns the WHOLE PAGE (a cross-language markdown table) for one short
    unit — the run-190 Item D bug. When `cure_on_retry`, it returns a clean single-unit
    target ONCE it sees the strengthened single-unit instruction in the system message
    (i.e. on the guard's one retry)."""

    name = "claude"

    def __init__(self, *, cure_on_retry):
        self.cure_on_retry = cure_on_retry
        self.systems = []

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        self.systems.append(system or "")
        self.last_usage = None
        retried = orchestrator._SINGLE_UNIT_INSTRUCTION in (system or "")
        if retried and self.cure_on_retry:
            return "Reservar"
        return _PAGE_DUMP


def _seg_step():
    return WorkflowStep(
        workflow_id=1,
        order_index=0,
        step_type=StepType.LLM_AGENT,
        provider="claude",
        prompt_text="Translate: {text}",
        chunk_policy="auto",
    )


def _run_seg(dbl):
    spec = ProviderSpec(provider="claude", retry_max=0)
    return orchestrator._run_step_on_segment(
        spec,
        "Translate: {text}",
        {"source_locale": "en-US"},
        _seg_step(),
        "Book",
        "Book",
        "es-ES",
        [],
    )


def test_d_page_dump_rejected_retry_then_honest_failure():
    """A single-line source whose candidate is a WHOLE-PAGE table is REJECTED; the guard
    retries ONCE with the strengthened single-unit instruction; a model that STILL dumps
    yields over_translation=True (the DB thread withholds it — never commits the dump)."""
    _reset_overrides()
    dbl = _PageDump(cure_on_retry=False)
    register_provider("claude", dbl)
    try:
        res = _run_seg(dbl)
    finally:
        _reset_overrides()
    assert len(dbl.systems) == 2, ("exactly one retry expected", dbl.systems)
    assert orchestrator._SINGLE_UNIT_INSTRUCTION not in dbl.systems[0], "first call unstrengthened"
    assert orchestrator._SINGLE_UNIT_INSTRUCTION in dbl.systems[1], (
        "retry must strengthen the system"
    )
    assert res.get("over_translation") is True, res
    _ok("page dump rejected -> one strengthened retry -> over_translation=True (withheld)")


def test_d_retry_cures_page_dump():
    """When the strengthened retry returns a clean single-unit target, the segment is
    ACCEPTED (over_translation False) and the cured text is the output — not the dump."""
    _reset_overrides()
    dbl = _PageDump(cure_on_retry=True)
    register_provider("claude", dbl)
    try:
        res = _run_seg(dbl)
    finally:
        _reset_overrides()
    assert len(dbl.systems) == 2, dbl.systems
    assert res.get("over_translation") is False, res
    assert res["output"] == "Reservar", res
    _ok("strengthened retry cures the dump -> clean unit committed")


def test_d_kill_switch_commits_dump():
    """OVER_TRANSLATION_MAX_RATIO == 0 DISABLES the guard: the dump is neither retried nor
    withheld (the pre-190 behaviour, leaving only the soft length_runaway flag downstream)."""
    from app import config as _cfg

    _reset_overrides()
    _saved = _cfg.OVER_TRANSLATION_MAX_RATIO
    _cfg.OVER_TRANSLATION_MAX_RATIO = 0.0
    dbl = _PageDump(cure_on_retry=False)
    register_provider("claude", dbl)
    try:
        res = _run_seg(dbl)
    finally:
        _cfg.OVER_TRANSLATION_MAX_RATIO = _saved
        _reset_overrides()
    assert len(dbl.systems) == 1, ("kill switch must skip the retry", dbl.systems)
    assert res.get("over_translation") is False, res
    assert res["output"] == _PAGE_DUMP, "kill switch commits the candidate unchanged"
    _ok("kill switch (ratio=0) disables the guard: dump committed, no retry")


def test_d_end_to_end_withholds_page_dump():
    """End-to-end: a page-dump segment ends NEEDS_REVIEW with the dump WITHHELD from the
    delivered target (the honest-failure disposition — never a committed page)."""
    _reset_overrides()
    _switch(True)
    register_provider("claude", _PageDump(cure_on_retry=False))
    try:
        pid = _mk_key_project()
        kid = _add_key(pid, "cta.book")
        doc_id = _mk_doc_with_keys(pid, [kid], sources=["Book"])
        wf_id = _mk_workflow([{"provider": "claude"}])
        _run(wf_id, doc_id)
        with Session(engine) as s:
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
            assert seg.status == SegStatus.NEEDS_REVIEW, seg.status
            assert _PAGE_DUMP not in (seg.target_text or ""), (
                "the page dump must NEVER be committed to the delivered target",
                seg.target_text,
            )
    finally:
        _reset_overrides()
    _ok("end-to-end: page dump withheld -> segment NEEDS_REVIEW, dump not delivered")


def test_d_withhold_survives_refusal_guard_off():
    """H2 (gate round 1): Item D's withhold is governed by OVER_TRANSLATION_MAX_RATIO ALONE,
    NEVER by the unrelated REFUSAL_GUARD_ENABLED kill switch. With the refusal guard OFF the
    page dump must STILL be withheld -- target_text is not written and the segment is
    NEEDS_REVIEW. (Pre-fix this committed the dump to target_text while the StepRun blob said
    WITHHELD -- the run trace contradicting the delivered target.)"""
    from app import config as _cfg

    _reset_overrides()
    _switch(True)
    _saved = _cfg.REFUSAL_GUARD_ENABLED
    _cfg.REFUSAL_GUARD_ENABLED = False
    register_provider("claude", _PageDump(cure_on_retry=False))
    try:
        pid = _mk_key_project()
        kid = _add_key(pid, "cta.book2")
        doc_id = _mk_doc_with_keys(pid, [kid], sources=["Book"])
        wf_id = _mk_workflow([{"provider": "claude"}])
        _run(wf_id, doc_id)
        with Session(engine) as s:
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
            assert _PAGE_DUMP not in (seg.target_text or ""), (
                "with REFUSAL_GUARD_ENABLED=0 the dump was COMMITTED to target_text (H2)",
                seg.target_text,
            )
            assert seg.status == SegStatus.NEEDS_REVIEW, (
                "the withheld dump segment must be NEEDS_REVIEW even with the refusal guard off",
                seg.status,
            )
    finally:
        _cfg.REFUSAL_GUARD_ENABLED = _saved
        _reset_overrides()
    _ok("H2: over-translation withhold holds with REFUSAL_GUARD_ENABLED=0 (dump not delivered)")


def _stepruns_for_doc(s, doc_id):
    """Every StepRun row for the run(s) over `doc_id`, in id order."""
    run_ids = s.exec(select(WorkflowRun.id).where(WorkflowRun.document_id == doc_id)).all()
    return s.exec(
        select(StepRun).where(StepRun.workflow_run_id.in_(run_ids)).order_by(StepRun.id)
    ).all()


def test_d_withhold_labelled_over_translation_not_refusal():
    """M-1 (gate round 2): an over-translation (page-dump) withhold is labelled HONESTLY, not
    as a model refusal. The segment carries a `nontranslation`-family qa_flag whose reason
    NAMES over-translation AND the shape rule that fired (a markdown table here), never a
    `refusal` flag reading "model refusal"; and the StepRun blob uses the distinct
    over-translation marker, not the refusal WITHHELD_OUTPUT. Counterfactual: on `ac7bf169`
    the only signal was `type: "refusal"` reading "suspected non-translation / model refusal"
    and the refusal marker, so this test goes red there."""
    _reset_overrides()
    _switch(True)
    register_provider("claude", _PageDump(cure_on_retry=False))
    try:
        pid = _mk_key_project()
        kid = _add_key(pid, "cta.book.m1")
        doc_id = _mk_doc_with_keys(pid, [kid], sources=["Book"])
        wf_id = _mk_workflow([{"provider": "claude"}])
        _run(wf_id, doc_id)
        with Session(engine) as s:
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
            flags = seg.qa_flags or []
            assert not any(isinstance(f, dict) and f.get("type") == "refusal" for f in flags), (
                "an over-translation withhold must NOT carry a refusal flag (M-1)",
                flags,
            )
            nt = [
                f
                for f in flags
                if isinstance(f, dict) and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
            ]
            assert nt, ("the withhold must carry a nontranslation flag naming the guard", flags)
            reason = (nt[0].get("reason") or "") + " " + (nt[0].get("message") or "")
            assert "over-translation" in reason, ("the reason must name over-translation", nt[0])
            assert "model refusal" not in reason, ("must not say model refusal (M-1)", nt[0])
            assert "markdown table" in reason, ("the reason must name the shape rule", nt[0])
            blobs = " ".join(x.output_text or "" for x in _stepruns_for_doc(s, doc_id))
            assert refusal.WITHHELD_OUTPUT_OVER_TRANSLATION in blobs, (
                "the StepRun blob must use the over-translation marker",
                blobs,
            )
            assert refusal.WITHHELD_OUTPUT not in blobs, (
                "the StepRun blob must NOT use the refusal marker for an over-translation dump",
                blobs,
            )
    finally:
        _reset_overrides()
    _ok("M-1: over-translation withhold labelled over-translation, never model refusal")


def test_d_output_chars_is_produced_not_blob():
    """M-2 (gate round 2): StepRun.output_chars is the PRODUCED translation length — the sum
    of the committed per-segment text, a withheld unit counting 0 — honouring the column
    contract and matching the FULL_TEXT seam's len(output), NOT len(output_blob) (which folds
    in the SEG_JOINER separators and the "[withheld — ...]" markers). Two seams:
    (a) a clean two-segment run stamps the produced chars, strictly LESS than the blob length;
    (b) a fully-withheld page-dump step stamps 0, not the marker length. Counterfactual: on
    `ac7bf169` (a) stamped blob+separator length and (b) stamped 54, so both assertions fail."""
    _reset_overrides()
    _switch(True)
    # (a) clean two-segment run (mock provider commits both segments)
    try:
        pid = _mk_key_project()
        k1 = _add_key(pid, "m2.a")
        k2 = _add_key(pid, "m2.b")
        doc_id = _mk_doc_with_keys(pid, [k1, k2], sources=["Hello", "Goodbye"])
        wf_id = _mk_workflow([{}])
        _run(wf_id, doc_id)
        with Session(engine) as s:
            segs = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
            produced = sum(len(sg.target_text or "") for sg in segs)
            assert produced > 0, ("expected committed targets on a clean run", segs)
            sr = _stepruns_for_doc(s, doc_id)
            assert sr, "expected a StepRun row"
            assert sr[0].output_chars == produced, (
                "clean run: output_chars must equal the produced text length, not the blob",
                sr[0].output_chars,
                produced,
                sr[0].output_text,
            )
            assert sr[0].output_chars < len(sr[0].output_text or ""), (
                "output_chars must be strictly less than the blob (the separator inflates it)",
                sr[0].output_chars,
                sr[0].output_text,
            )
    finally:
        _reset_overrides()
    # (b) a fully-withheld page-dump step produced 0 deliverable chars
    _switch(True)
    register_provider("claude", _PageDump(cure_on_retry=False))
    try:
        pid = _mk_key_project()
        kid = _add_key(pid, "m2.dump")
        doc_id = _mk_doc_with_keys(pid, [kid], sources=["Book"])
        wf_id = _mk_workflow([{"provider": "claude"}])
        _run(wf_id, doc_id)
        with Session(engine) as s:
            sr = _stepruns_for_doc(s, doc_id)
            assert sr, "expected a StepRun row"
            assert sr[0].output_chars == 0, (
                "a fully-withheld page-dump step produced 0 chars, not the marker length",
                sr[0].output_chars,
                sr[0].output_text,
            )
            assert sr[0].output_chars != len(sr[0].output_text or ""), (
                "output_chars must NOT be the withheld-marker length (M-2)",
                sr[0].output_text,
            )
    finally:
        _reset_overrides()
    # (c) LOW-4: the OTHER stamping seam, the FULL_TEXT path (orchestrator.run_workflow_stream)
    # -- it was already honest (len(output)), but was untested; pin it so both seams are covered.
    _reset_overrides()
    try:
        doc_id = _mk_fulltext_doc(body="Hello world.")
        wf_id = _mk_workflow([{}])
        _run_fulltext(wf_id, doc_id)
        with Session(engine) as s:
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
            sr = _stepruns_for_doc(s, doc_id)
            assert sr, "expected a FULL_TEXT StepRun row"
            assert sr[0].output_chars == len(seg.target_text or ""), (
                "FULL_TEXT: output_chars must equal the delivered target length",
                sr[0].output_chars,
                seg.target_text,
            )
    finally:
        _reset_overrides()
    _ok("M-2: output_chars is produced length on both seams (segmented + FULL_TEXT)")


def _seg_step_guard(value):
    st = _seg_step()
    st.over_translation_guard = value
    return st


def _run_seg_step(dbl, step):
    spec = ProviderSpec(provider="claude", retry_max=0)
    return orchestrator._run_step_on_segment(
        spec, "Translate: {text}", {"source_locale": "en-US"}, step, "Book", "Book", "es-ES", []
    )


def test_d_per_step_opt_out_skips_guard():
    """M5 (gate round 1): a step whose over_translation_guard is False opts OUT of the guard
    entirely -- the page-dump candidate is committed unchanged, NO strengthened retry, and
    over_translation is False (the per-step twin of the ratio=0 kill switch). Pre-fix there
    was no per-step opt-out, so the guard would have fired and withheld the dump."""
    _reset_overrides()
    dbl = _PageDump(cure_on_retry=False)
    register_provider("claude", dbl)
    try:
        res = _run_seg_step(dbl, _seg_step_guard(False))
    finally:
        _reset_overrides()
    assert len(dbl.systems) == 1, ("opt-out must skip the retry", dbl.systems)
    assert res.get("over_translation") is False, res
    assert res["output"] == _PAGE_DUMP, "opt-out commits the candidate unchanged"
    _ok("M5: over_translation_guard=False opts the step out (dump committed, no retry)")


def test_d_per_step_guard_on_still_fires():
    """M5 control: over_translation_guard True/None still runs the guard (retry + withhold),
    so the opt-out test above is not vacuous."""
    _reset_overrides()
    for value in (True, None):
        dbl = _PageDump(cure_on_retry=False)
        register_provider("claude", dbl)
        try:
            res = _run_seg_step(dbl, _seg_step_guard(value))
        finally:
            _reset_overrides()
        assert len(dbl.systems) == 2, (value, dbl.systems)
        assert res.get("over_translation") is True, (value, res)
    _ok("M5 control: over_translation_guard on/None still fires the guard")


def test_d_per_step_opt_out_refused_on_non_llm_agent():
    """M5 (gate round 1): over_translation_guard is llm_agent-only -- a non-default value on
    an lqa/human_review/tm_match/external step is REFUSED (dead-field guard), the same posture
    as tm_protect. Proves the four-surface field is not silently accepted-and-ignored."""
    from app.services import workflows as _wf

    with Session(engine) as s:
        wf = _wf.create_workflow(s, name="otg-dead-fields")
        for bad_type in (StepType.LQA, StepType.HUMAN_REVIEW, StepType.TM_MATCH):
            try:
                _wf.add_step(s, wf.id, name="x", step_type=bad_type, over_translation_guard="off")
                raise AssertionError(f"over_translation_guard must be refused on {bad_type}")
            except _wf.WorkflowError:
                pass
    _ok("M5: over_translation_guard refused on lqa/human_review/tm_match (dead field)")


class _SegBurn(Provider):
    """A segmented-path double: raises ThinkingBurn (empty output + finish=length) when
    `burn_on` is absent OR present in the prompt/system; otherwise returns a clean single-unit
    translation with last_finish='stop'. Lets a test drive the segmented StepRun's
    finish_reason / error_class stamp on both the burn and the normal-stop shapes."""

    name = "claude"

    def __init__(self, *, burn_on=None):
        self.burn_on = burn_on
        self.last_finish = None
        self.last_usage = None

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        hay = (prompt or "") + " " + (system or "")
        if self.burn_on is None or self.burn_on in hay:
            raise P.ThinkingBurn("segmented burn", empty_output=True, finish_reason="length")
        self.last_finish = "stop"
        self.last_usage = None
        return f"[es] {(payload or {}).get('text', '')}"


class _SegBurnOnSource(Provider):
    """A segmented-path double that burns (ThinkingBurn) ONLY for a segment whose SOURCE text is
    in `burn_sources`; every other segment returns a clean single-unit translation
    (last_finish='stop'). Lets a test build a MIXED segmented step (one burn beside one clean) in
    either segment order to pin the M-3 order-independent, self-consistent finish-shape stamp."""

    name = "claude"

    def __init__(self, *, burn_sources):
        self.burn_sources = {s.strip() for s in burn_sources}
        self.last_finish = None
        self.last_usage = None

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        text = ((payload or {}).get("text") or "").strip()
        if text in self.burn_sources:
            raise P.ThinkingBurn("mixed segmented burn", empty_output=True, finish_reason="length")
        self.last_finish = "stop"
        self.last_usage = None
        return f"[es] {text}"


class _SegBurnContains(Provider):
    """Like _SegBurnOnSource but keys on a SUBSTRING of the text it is handed, so it can burn a
    specific segment on a LATER step where the working text is a PRIOR step's translation (e.g.
    mock's '[es-ES] Book'), not the raw source. Used to build the DEGRADED-burn shape (M-2): a
    segment already produced by an earlier step whose call now fails but keeps its prior text."""

    name = "claude"

    def __init__(self, *, burn_contains):
        self.burn_contains = burn_contains
        self.last_finish = None
        self.last_usage = None

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        text = (payload or {}).get("text") or ""
        if self.burn_contains in text:
            raise P.ThinkingBurn(
                "degraded segmented burn", empty_output=True, finish_reason="length"
            )
        self.last_finish = "stop"
        self.last_usage = None
        return f"[es] {text}"


class _MarkerDouble(Provider):
    """Returns a canned output chosen by a MARKER substring in the rendered prompt, so a two-step
    Translate -> Fix workflow can drive step 0 (produce a DNT-preserving translation) and step 1
    (break the DNT term -> fix-guard revert) through the single provider name both steps share."""

    name = "claude"

    def __init__(self, mapping):
        self.mapping = mapping  # marker substring -> canned output text
        self.last_finish = "stop"
        self.last_usage = None

    def complete(self, prompt, system=None, task=None, payload=None, images=None, **kw):
        for marker, out in self.mapping.items():
            if marker in (prompt or ""):
                return out
        return (payload or {}).get("text") or (prompt or "")


def test_d_segmented_stamps_finish_reason_and_error_class():
    """M-2 (gate round 3): the segmented StepRun stamps finish_reason / error_class from the
    per-chunk run_completion result -- _run_step_on_segment never THREADED them out before, so
    `seg_finish_reason = r.get("finish_reason") or seg_finish_reason` was dead code and BOTH
    columns were ALWAYS NULL on the segmented path. (a) a ThinkingBurn segment stamps
    finish_reason='length' + error_class='ThinkingBurn'; (b) a clean stop segment stamps
    finish_reason='stop' + error_class=None. Counterfactual on 45b570c6: both columns NULL, so
    every assertion goes red."""
    # (a) ThinkingBurn on the segmented path
    _reset_overrides()
    register_provider("claude", _SegBurn())  # always burns
    try:
        pid = _mk_key_project()
        kid = _add_key(pid, "m2fr.burn")
        doc_id = _mk_doc_with_keys(pid, [kid], sources=["Book"])
        wf_id = _mk_workflow([{"provider": "claude"}])
        _run(wf_id, doc_id)
        with Session(engine) as s:
            sr = _stepruns_for_doc(s, doc_id)
            assert sr, "expected a StepRun row"
            assert sr[0].finish_reason == "length", (
                "a segmented ThinkingBurn must stamp finish_reason=length (was NULL)",
                sr[0].finish_reason,
            )
            assert sr[0].error_class == "ThinkingBurn", (
                "a segmented ThinkingBurn must stamp error_class=ThinkingBurn (was NULL)",
                sr[0].error_class,
            )
    finally:
        _reset_overrides()
    # (b) a normal stop on the segmented path
    _reset_overrides()
    register_provider("claude", _SegBurn(burn_on="NEVER-IN-ANY-PROMPT"))  # never burns
    try:
        pid = _mk_key_project()
        kid = _add_key(pid, "m2fr.stop")
        doc_id = _mk_doc_with_keys(pid, [kid], sources=["Book"])
        wf_id = _mk_workflow([{"provider": "claude"}])
        _run(wf_id, doc_id)
        with Session(engine) as s:
            sr = _stepruns_for_doc(s, doc_id)
            assert sr, "expected a StepRun row"
            assert sr[0].finish_reason == "stop", (
                "a clean segmented stop must stamp finish_reason=stop (was NULL)",
                sr[0].finish_reason,
            )
            assert sr[0].error_class is None, (
                "a clean segmented stop has no error_class",
                sr[0].error_class,
            )
    finally:
        _reset_overrides()
    _ok("M-2: segmented StepRun stamps finish_reason/error_class (burn=length/ThinkingBurn; stop)")


def test_low1_output_chars_excludes_carryover():
    """LOW-1 (gate round 3): on a multi-step run whose step-2 segments all DEGRADE (a thinking
    burn keeps each segment's step-1 text rather than committing new text), step 2 must stamp
    output_chars=0 -- it produced NOTHING this visit. Before the fix the formula counted the
    CARRIED-OVER working[] text, so step 2 stamped step-1's length. Counterfactual on 45b570c6:
    step 2's output_chars equals the step-1 target length (> 0), so `== 0` goes red."""
    _reset_overrides()
    # step 1 translates cleanly (mock); step 2 (its prompt carries POLISH) thinking-burns both
    register_provider("claude", _SegBurn(burn_on="POLISH"))
    try:
        pid = _mk_key_project()
        k1 = _add_key(pid, "low1.a")
        k2 = _add_key(pid, "low1.b")
        doc_id = _mk_doc_with_keys(pid, [k1, k2], sources=["Hello", "World"])
        wf_id = _mk_workflow(
            [{"provider": "claude"}, {"provider": "claude", "prompt_text": "POLISH: {text}"}]
        )
        _run(wf_id, doc_id)
        with Session(engine) as s:
            sr = _stepruns_for_doc(s, doc_id)
            assert len(sr) >= 2, ("expected StepRun rows for both steps", [x.id for x in sr])
            step1, step2 = sr[0], sr[1]
            assert step1.output_chars > 0, ("step 1 produced real text", step1.output_chars)
            assert step2.output_chars == 0, (
                "step 2 degraded every segment (kept step-1 carry-over) -> produced 0 chars "
                "this visit; must not count the carried-over text (LOW-1)",
                step2.output_chars,
                step2.output_text,
            )
    finally:
        _reset_overrides()
    _ok("LOW-1: output_chars excludes carried-over text on an all-degraded step (stamps 0)")


def test_d_clean_run_clears_stale_over_translation_flag():
    """LOW-5 (gate round 3): a segment first WITHHELD as an over-translation page dump carries a
    nontranslation-family 'over-translation: ...' reason. After a LATER CLEAN run commits a good
    target for that unit, the stale withhold reason is CLEARED (reason-scoped, beside the
    scaffold clear), so the editor grid / segment API / review summary stop showing a review
    reason the delivered text no longer earns. Counterfactual on 45b570c6: the family had no
    over-translation clear, so the stale reason persists after the clean commit."""
    _reset_overrides()
    _switch(True)
    register_provider("claude", _PageDump(cure_on_retry=False))
    try:
        pid = _mk_key_project()
        kid = _add_key(pid, "low5.stale")
        doc_id = _mk_doc_with_keys(pid, [kid], sources=["Book"])
        wf_dump = _mk_workflow([{"provider": "claude"}])
        _run(wf_dump, doc_id)

        # LOW-4 (round 4): resolve the reason prefix via getattr with the literal default so a
        # counterfactual copy onto a pre-constant tree reaches the BEHAVIOUR (the cleared flag)
        # rather than AttributeError-ing on the missing constant name.
        _ot_prefix = getattr(refusal, "OVER_TRANSLATION_REASON_PREFIX", "over-translation:")

        def _has_ot(seg) -> bool:
            return any(
                isinstance(f, dict)
                and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
                and str(f.get("reason") or "").startswith(_ot_prefix)
                for f in (seg.qa_flags or [])
            )

        with Session(engine) as s:
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
            assert seg.status == SegStatus.NEEDS_REVIEW, ("run 1 withholds the dump", seg.status)
            assert _has_ot(seg), ("run 1 must leave the over-translation flag", seg.qa_flags)
    finally:
        _reset_overrides()
    # a later CLEAN run (mock provider) commits a good single-unit target for the same segment
    _reset_overrides()
    try:
        wf_clean = _mk_workflow([{}])  # mock provider -> clean single-unit output
        _run(wf_clean, doc_id)
        with Session(engine) as s:
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
            assert (seg.target_text or "").strip(), (
                "the clean run must commit a target",
                seg.target_text,
            )
            assert not _has_ot(seg), (
                "the clean commit must CLEAR the stale over-translation flag (LOW-5)",
                seg.qa_flags,
            )
    finally:
        _reset_overrides()
    _ok("LOW-5: a clean re-commit clears the stale over-translation withhold flag")


# LOW-3 (gate round 3): the shape-token map in refusal._OVER_TRANSLATION_SHAPES is a SECOND
# source of truth for the four reason tokens orchestrator._looks_like_unit_translation returns.
# Pin them together at IMPORT time so a renamed token fails LOUDLY instead of degrading the
# over-translation message to the generic "a page-shaped dump" wording.
assert set(refusal._OVER_TRANSLATION_SHAPES) == {
    "code_fence",
    "markdown_table",
    "multi_line",
    "too_long",
}, ("LOW-3: the over-translation shape-token set drifted", set(refusal._OVER_TRANSLATION_SHAPES))


def test_low3_shape_tokens_produced_by_detector():
    """LOW-3 (gate round 3): every token in refusal._OVER_TRANSLATION_SHAPES is ACTUALLY produced
    by orchestrator._looks_like_unit_translation on a fixture (so the human-readable map cannot
    silently name a token the detector never emits, which would degrade to generic wording)."""
    fixtures = {
        "code_fence": ("```\nreservar\n```", "Book"),
        "markdown_table": ("| en | es |\n| Book | Reservar |", "Book"),
        "multi_line": ("linea uno\nlinea dos", "Book"),
        "too_long": ("palabra " * 60, "Book"),
    }
    for token, (candidate, source) in fixtures.items():
        usable, shape = orchestrator._looks_like_unit_translation(candidate, source)
        assert not usable and shape == token, (
            "the detector must emit this shape token for its fixture",
            token,
            usable,
            shape,
        )
    assert set(fixtures) == set(refusal._OVER_TRANSLATION_SHAPES), (
        "every mapped token must have a producing fixture",
        set(refusal._OVER_TRANSLATION_SHAPES),
    )
    _ok("LOW-3: each over-translation shape token is produced by the detector (import-pinned)")


def test_m3_segmented_finish_shape_order_independent():
    """M-3 (gate round 4): a MIXED segmented step (one burned segment beside one clean one) stamps
    a DETERMINISTIC, self-consistent finish shape INDEPENDENT of segment order -- worst-case wins:
    finish_reason='length' + error_class='ThinkingBurn' whenever ANY segment burned. Before, the
    accumulation was 'last truthy wins' for finish_reason, so a LATER clean 'stop' clobbered an
    EARLIER segment's 'length', leaving 'stop' beside 'ThinkingBurn' -- order-dependent and
    contradictory. Counterfactual on c81c6d22: the burn-FIRST order stamps finish_reason='stop',
    so '== length' goes red."""
    for burn_source, label in (("Book", "burn-first"), ("Cancel", "burn-last")):
        _reset_overrides()
        register_provider("claude", _SegBurnOnSource(burn_sources={burn_source}))
        try:
            pid = _mk_key_project()
            k1 = _add_key(pid, f"m3.{label}.a")
            k2 = _add_key(pid, f"m3.{label}.b")
            doc_id = _mk_doc_with_keys(pid, [k1, k2], sources=["Book", "Cancel"])
            wf_id = _mk_workflow([{"provider": "claude"}])
            _run(wf_id, doc_id)
            with Session(engine) as s:
                sr = _stepruns_for_doc(s, doc_id)
                assert sr, "expected a StepRun row"
                assert sr[0].finish_reason == "length", (
                    "a mixed step with ANY burned segment must stamp finish_reason=length "
                    "regardless of segment order (worst-case wins)",
                    label,
                    sr[0].finish_reason,
                    sr[0].error_class,
                )
                assert sr[0].error_class == "ThinkingBurn", (
                    "error_class must name the burn, consistent with finish_reason (never "
                    "stop beside ThinkingBurn)",
                    label,
                    sr[0].error_class,
                )
        finally:
            _reset_overrides()
    _ok("M-3: a mixed segmented step stamps length/ThinkingBurn independent of segment order")


def test_low2_output_chars_excludes_status_protected_carryover():
    """LOW-2 (gate round 4): output_chars counts only what THIS step visit PRODUCED. A
    status-protected (human-approved) segment is dropped from pending_idx and KEEPS its prior
    target as carry-over, so its length must NOT be counted. Before, the sum ran over
    range(len(segments)) (minus refused/degraded), folding in the approved carry-over.
    Counterfactual on c81c6d22: output_chars includes the 61-char approved carry-over."""
    _reset_overrides()
    try:
        pid = _mk_key_project()
        k1 = _add_key(pid, "low2.a")
        k2 = _add_key(pid, "low2.b")
        doc_id = _mk_doc_with_keys(pid, [k1, k2], sources=["Book", "Cancel"])
        carry = "X" * 61
        with Session(engine) as s:
            segs = sorted(
                s.exec(select(Segment).where(Segment.document_id == doc_id)).all(),
                key=lambda x: x.order_index,
            )
            segs[0].status = SegStatus.APPROVED  # human-approved -> status-protected, excluded
            segs[0].target_text = carry
            s.add(segs[0])
            s.commit()
        _run(_mk_workflow([{"provider": "mock"}]), doc_id)  # mock translates only the pending seg
        with Session(engine) as s:
            sr = _stepruns_for_doc(s, doc_id)
            assert sr, "expected a StepRun row"
            segs = sorted(
                s.exec(select(Segment).where(Segment.document_id == doc_id)).all(),
                key=lambda x: x.order_index,
            )
            produced_len = len(segs[1].target_text or "")
            assert produced_len > 0, ("segment 1 must have produced text", segs[1].target_text)
            assert sr[0].output_chars == produced_len, (
                "output_chars must count only the segment THIS visit produced, excluding the "
                "status-protected carry-over (LOW-2)",
                sr[0].output_chars,
                produced_len,
                len(carry),
            )
    finally:
        _reset_overrides()
    _ok("LOW-2: output_chars excludes status-protected carry-over (only this-visit produced)")


def test_low1_over_translation_clear_ungated():
    """LOW-1 (gate round 4): the over-translation CLEAR must be UNGATED, matching the ungated
    WITHHOLD (governed by OVER_TRANSLATION_MAX_RATIO alone, never REFUSAL_GUARD_ENABLED -- H2).
    With REFUSAL_GUARD_ENABLED=0 a dump is STILL withheld+flagged (H2) AND a later clean run must
    STILL clear the stale over-translation flag. Before, the clear sat under `if guard:`, so with
    the guard off the stale badge persisted forever. Counterfactual on c81c6d22: the flag
    survives the clean commit when the guard is off."""
    from app import config as _cfg

    def _has_ot(seg) -> bool:
        return any(
            isinstance(f, dict)
            and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
            and str(f.get("reason") or "").startswith(refusal.OVER_TRANSLATION_REASON_PREFIX)
            for f in (seg.qa_flags or [])
        )

    _reset_overrides()
    _switch(True)
    _saved = _cfg.REFUSAL_GUARD_ENABLED
    _cfg.REFUSAL_GUARD_ENABLED = False
    try:
        register_provider("claude", _PageDump(cure_on_retry=False))
        pid = _mk_key_project()
        kid = _add_key(pid, "low1g.stale")
        doc_id = _mk_doc_with_keys(pid, [kid], sources=["Book"])
        _run(_mk_workflow([{"provider": "claude"}]), doc_id)
        with Session(engine) as s:
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
            assert seg.status == SegStatus.NEEDS_REVIEW and _has_ot(seg), (
                "guard-off: the dump must be withheld and flagged (H2 + Item D)",
                seg.status,
                seg.qa_flags,
            )
        _reset_overrides()  # drop the dump double; the clean mock now translates the same unit
        _cfg.REFUSAL_GUARD_ENABLED = False  # keep the guard OFF across the clean re-run
        _run(_mk_workflow([{}]), doc_id)
        with Session(engine) as s:
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
            assert (seg.target_text or "").strip(), (
                "the clean run must commit a target",
                seg.target_text,
            )
            assert not _has_ot(seg), (
                "guard-off: the clean commit must CLEAR the stale over-translation flag (LOW-1)",
                seg.qa_flags,
            )
    finally:
        _cfg.REFUSAL_GUARD_ENABLED = _saved
        _reset_overrides()
    _ok("LOW-1: the over-translation clear is ungated (fires with REFUSAL_GUARD_ENABLED=0)")


def test_m2_degraded_segment_latches_finish_shape():
    """M-2 (gate round 5): a segment whose provider call FAILED but which ALREADY holds a prior
    translation degrades gracefully (keeps its pre-step text) and `continue`s -- and its failure
    shape must STILL feed the worst-case-wins latch. Before, the degraded `continue` ran BEFORE
    `_latch_seg_shape`, so a step-2+ burn on an already-translated segment let a sibling clean
    segment stamp finish_reason='stop'/error_class=None on the step -- contradicting docs/CLI.md's
    'any burned/errored segment wins'. A first clean step (mock) produces both segments; the SECOND
    step (claude) burns on ONE of them (now DEGRADED, since it is already in run-level `produced`).
    Covered in BOTH segment orders. Counterfactual on 945fd7dd: the second step's StepRun stamps
    finish_reason='stop' + error_class=None, so '== length' goes red."""
    for burn_contains, label in (("Book", "burn-first"), ("Cancel", "burn-last")):
        _reset_overrides()
        register_provider("claude", _SegBurnContains(burn_contains=burn_contains))
        try:
            pid = _mk_key_project()  # default provider mock -> step 0 produces BOTH segments
            k1 = _add_key(pid, f"m2deg.{label}.a")
            k2 = _add_key(pid, f"m2deg.{label}.b")
            doc_id = _mk_doc_with_keys(pid, [k1, k2], sources=["Book", "Cancel"])
            # step 0 = mock (clean, produces both); step 1 = claude (burns on one -> degrades it)
            wf_id = _mk_workflow([{"provider": "mock"}, {"provider": "claude"}])
            _run(wf_id, doc_id)
            with Session(engine) as s:
                sr = _stepruns_for_doc(s, doc_id)
                assert len(sr) >= 2, ("expected two StepRun rows (two steps)", len(sr))
                deg = sr[1]  # the SECOND step: one segment burned + degraded, one clean
                assert deg.finish_reason == "length", (
                    "a degraded (failed but already-translated) burn segment must feed the "
                    "worst-case-wins latch, so the step stamps finish_reason=length, never a clean "
                    "'stop' beside a real burn",
                    label,
                    deg.finish_reason,
                    deg.error_class,
                )
                assert deg.error_class == "ThinkingBurn", (
                    "error_class must name the burn, consistent with finish_reason",
                    label,
                    deg.error_class,
                )
        finally:
            _reset_overrides()
    _ok("M-2: a degraded burn segment latches length/ThinkingBurn on the StepRun (both orders)")


def test_low1_tm_step0_fill_stamps_output_chars():
    """LOW-1 (gate round 5): a 100% TM match filled by the step-0 short-circuit is delivered THIS
    visit, so its chars must be counted in output_chars. The TM-filled segment is never appended to
    `pending_idx` (no provider call), so the round-4 `sum(... for j in pending_idx ...)` stamped 0
    for a step whose whole delivery was a TM fill. Counterfactual on 945fd7dd: output_chars=0."""
    _reset_overrides()
    try:
        pid = _mk_key_project()
        target = "Reservar ahora mismo"  # 20 chars, distinctive
        with Session(engine) as s:
            tm.write_back(s, pid, "Book", target, "en-US", "es-ES")
        kid = _add_key(pid, "low1tm.a")
        doc_id = _mk_doc_with_keys(pid, [kid], sources=["Book"])
        _run(_mk_workflow([{"provider": "mock"}]), doc_id)  # single step at order 0 -> TM-first
        with Session(engine) as s:
            sr = _stepruns_for_doc(s, doc_id)
            assert sr, "expected a StepRun row"
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
            assert seg.target_text == target, ("the segment must be TM-filled", seg.target_text)
            assert sr[0].output_chars == len(target), (
                "output_chars must count the step-0 TM fill (this visit's delivery), not 0",
                sr[0].output_chars,
                len(target),
            )
    finally:
        _reset_overrides()
    _ok("LOW-1: a step-0 100% TM fill stamps its real output_chars (not 0)")


def test_low2_output_chars_excludes_reverted_carryover():
    """LOW-2 (gate round 5): a fix-guard REVERTED segment kept its PRIOR working[] text (the
    rejected edit was discarded), so its chars must NOT be counted in output_chars -- the same
    carry-over breach the round-4 LOW-2 closed for the status-protected class. A reverted segment
    is in `pending_idx` and is neither refused nor degraded, so the round-4 sum still counted it.
    A Translate step produces a DNT-preserving target; a Fix step breaks the DNT term -> reverted.
    Counterfactual on 945fd7dd: the Fix step's output_chars counts the kept carry-over."""
    _reset_overrides()
    try:
        pid = _mk_key_project()
        with Session(engine) as s:
            s.add(
                GlossaryEntry(
                    project_id=pid,
                    source_term="BrandX",
                    target_term="BrandX",
                    source_locale="en-US",
                    target_locale="es-ES",
                    dnt=True,
                )
            )
            s.commit()
        kid = _add_key(pid, "low2rev.a")
        doc_id = _mk_doc_with_keys(pid, [kid], sources=["Keep BrandX visible."])
        register_provider(
            "claude",
            _MarkerDouble(
                {
                    "STEP0:": "Mantener BrandX visible.",  # keeps the DNT term -> produced
                    "FIX:": "Mantener MarcaX visible.",  # breaks the DNT term -> reverted
                }
            ),
        )
        wf_id = _mk_workflow(
            [
                {"provider": "claude", "prompt_text": "STEP0: {text}"},
                {"provider": "claude", "prompt_text": "FIX: {text}"},
            ]
        )
        _run(wf_id, doc_id)
        with Session(engine) as s:
            sr = _stepruns_for_doc(s, doc_id)
            assert len(sr) >= 2, ("expected two StepRun rows", len(sr))
            seg = s.exec(select(Segment).where(Segment.document_id == doc_id)).one()
            assert seg.target_text == "Mantener BrandX visible.", (
                "the reverted edit must be discarded (pre-fix text kept)",
                seg.target_text,
            )
            assert sr[1].output_chars == 0, (
                "output_chars must exclude a fix-guard REVERTED carry-over (produced nothing "
                "this visit)",
                sr[1].output_chars,
            )
    finally:
        _reset_overrides()
    _ok("LOW-2: output_chars excludes a fix-guard reverted carry-over (this-visit produced only)")


def main():
    # [A]
    test_no_image_path_byte_identical()
    test_block_shapes()
    test_capability_resolver_matrix()
    test_run_completion_images_sent_and_last_images()
    test_authorized_engine_capability_read_failure_propagates()
    test_run_completion_vision_primary_failover_records_no_send()
    test_token_estimate_constant_in_sync()
    test_m3_usage_allowance()
    test_mock_last_images_deterministic_and_exempt()
    # [C]
    test_vision_engine_sends_images()
    test_send_all_images_raises_cap()
    test_skip_note_nonvision_primary()
    test_multichunk_sends_chunk0_only()
    test_qe_engine_combos()
    # [D]
    test_toggle_sibling_steps_one_run()
    test_opted_out_only_on_vision_zero_skips()
    test_opted_in_on_nonvision_still_skips()
    test_default_true_after_migration_and_fresh_create()
    test_roundtrip_service_and_api()
    test_clone_and_copy_preserve()
    test_non_llm_agent_ignores_flag()
    test_dynamic_attach_capability_warning()
    test_builder_badge_rendered_page()
    # run-190 Item D: over-translation (page-dump) HARD guard
    test_d_page_dump_rejected_retry_then_honest_failure()
    test_d_retry_cures_page_dump()
    test_d_kill_switch_commits_dump()
    test_d_end_to_end_withholds_page_dump()
    test_d_withhold_survives_refusal_guard_off()  # H2
    test_d_withhold_labelled_over_translation_not_refusal()  # M-1
    test_d_output_chars_is_produced_not_blob()  # M-2
    test_d_segmented_stamps_finish_reason_and_error_class()  # M-2 (round 3)
    test_low1_output_chars_excludes_carryover()  # LOW-1 (round 3)
    test_d_clean_run_clears_stale_over_translation_flag()  # LOW-5 (round 3)
    test_low3_shape_tokens_produced_by_detector()  # LOW-3 (round 3)
    test_m3_segmented_finish_shape_order_independent()  # M-3 (round 4)
    test_low2_output_chars_excludes_status_protected_carryover()  # LOW-2 (round 4)
    test_low1_over_translation_clear_ungated()  # LOW-1 (round 4)
    test_m2_degraded_segment_latches_finish_shape()  # M-2 (round 5)
    test_low1_tm_step0_fill_stamps_output_chars()  # LOW-1 (round 5)
    test_low2_output_chars_excludes_reverted_carryover()  # LOW-2 (round 5)
    test_d_per_step_opt_out_skips_guard()  # M5
    test_d_per_step_guard_on_still_fires()  # M5 control
    test_d_per_step_opt_out_refused_on_non_llm_agent()  # M5 dead-field
    print("KEY IMAGE VISION OK")


if __name__ == "__main__":
    main()
