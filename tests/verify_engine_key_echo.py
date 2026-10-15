"""#359 - an engine key echoed in a SUCCESSFUL answer never reaches stored text.

Contract (app/services/secrets_scrub.py, guard_answer): an answer carrying the engine's own
key, its paired secret or a global key is untrusted output, so the call FAILS as a terminal
engine error (no retry) instead of delivering a translation with a redaction marker. The key
therefore never lands in the returned text, the Prompt Tap record (response.text, error,
request), the logs, or anything downstream. Covers run_completion (every MT / workflow /
assist / judge / Advanced caller), complete_direct (glossary + term extraction), the OCR and
image-localization vision paths, the added key shapes and false positives on ordinary text.
Offline: httpx.Client.send is patched. ASCII-only prints.
"""

import json
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

TMP = init_env("loctms_keyecho_")

import httpx
from sqlmodel import Session

from app import config
from app.db import engine, init_db

init_db()
from app.models import EngineProfile
from app.services import engines, prompt_tap, qe_jev, spend
from app.services.providers import ProviderSpec, local_complete, run_completion
from app.services.secrets_scrub import (
    KeyEchoError,
    known_keys,
    redact_diagnostic,
    scrub,
)

KEY = "QZXmistralStyleKey9a8b7c6d5e4f3g2h1"  # prefix-less, like Mistral / Cohere
SLASHY = "QZX/slashy+key==9a8b7c6d5e4f3g2h1"  # URL-encoded / escaped spellings differ
PAIRED = "LARAsecretPAIRED0123456789abcdef"


def ok(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)


with Session(engine) as s:
    compat = EngineProfile(
        name="compat",
        kind="openai_compatible",
        base_url="https://api.example.invalid/v1",
        api_key_enc=engines.enc_key(KEY),
        model="m1",
        config={},
    )
    slashy = EngineProfile(
        name="slashy",
        kind="openai_compatible",
        base_url="https://api2.example.invalid/v1",
        api_key_enc=engines.enc_key(SLASHY),
        model="m1",
        config={},
    )
    s.add(compat)
    s.add(slashy)
    s.commit()
    CID, SID = compat.id, slashy.id
prompt_tap.set_enabled(True)

ANSWER = {"a": "Hallo Welt"}
USAGE = {"on": True}
calls = {"n": 0}
_orig_send = httpx.Client.send


def fake_send(self, request, *a, **k):
    calls["n"] += 1
    auth = request.headers.get("authorization", "")
    tok = auth.split(" ", 1)[1] if " " in auth else auth
    content = ANSWER["a"].replace("{KEY}", tok)
    body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    if USAGE["on"]:
        body["usage"] = {"prompt_tokens": 5, "completion_tokens": 3}
    return httpx.Response(200, json=body, request=request)


httpx.Client.send = fake_send


class Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = Cap()
logging.getLogger().addHandler(cap)
logging.getLogger().setLevel(logging.DEBUG)


def tap_blob():
    blob = ""
    for dp, _dn, fns in os.walk(os.path.join(TMP, "prompt_tap")):
        for fn in fns:
            with open(os.path.join(dp, fn), encoding="utf-8", errors="replace") as fh:
                blob += fh.read()
    return blob


def run(pid, answer, retry_max=2):
    ANSWER["a"] = answer
    calls["n"] = 0
    return run_completion(
        ProviderSpec(provider=f"engine:{pid}", retry_max=retry_max, backoff_base=0),
        "Hello",
        system="sys",
        task="translate",
    )


def spellings(key):
    return {key, key.upper(), json.dumps(key)[1:-1]}


def test_echo_shapes_fail_the_call_and_never_store_the_key():
    cases = {
        "plain": "{KEY}",
        "start": "{KEY} Hallo Welt",
        "middle": "Hallo {KEY} Welt",
        "end": "Hallo Welt {KEY}",
        "multiple": "{KEY} Hallo {KEY} Welt {KEY}",
        "bearer": "Hallo (Authorization: Bearer {KEY})",
        "json": '{"translation": "Hallo", "debug": {"echo": "{KEY}"}}',
    }
    for label, ans in cases.items():
        res = run(CID, ans)
        ok(res["text"] == "", f"{label}: no text delivered")
        ok(res["error"], f"{label}: failed as an engine error")
        ok(KEY not in res["error"], f"{label}: key not in the error")
        ok(calls["n"] == 1, f"{label}: terminal, not retried (calls={calls['n']})")
        ok(res["error_class"] == "KeyEchoError", f"{label}: error class {res['error_class']}")
    blob = tap_blob()
    ok("provider-error" in blob, "the failed attempt is still captured in Prompt Tap")
    ok(KEY not in blob, "key absent from every Prompt Tap record")
    ok(not any(KEY in line for line in cap.lines), "key absent from logs")


def test_echo_without_usage_block_is_still_billed():
    # Gate fix: an engine that reports no usage block is billed the length estimate on a
    # success; a key-echo failure of the same call must be billed the same way, once.
    USAGE["on"] = False
    try:
        clean = run(CID, "Hallo Welt")
        echo = run(CID, "Hallo {KEY}")
    finally:
        USAGE["on"] = True
    ok(clean["cost_usd"] > 0, "control: a usage-less success is billed")
    ok(echo["error_class"] == "KeyEchoError", "usage-less echo still fails")
    ok(echo["cost_usd"] > 0, f"usage-less echo is billed (cost={echo['cost_usd']})")
    ok(echo["attempts"] == 1, "billed for exactly one attempt")


def test_special_character_key_spellings():
    for label, ans in {
        "literal": "x {KEY}",
        "json-escaped": json.dumps({"k": "{KEY}"}),
    }.items():
        # the json-escaped answer embeds the token JSON-escaped by the fake server
        tok_ans = ans.replace("{KEY}", SLASHY.replace("\\", "\\\\").replace('"', '\\"'))
        res = run(SID, tok_ans)
        ok(res["text"] == "" and res["error"], f"slashy {label} fails")
        ok(SLASHY not in (res["error"] or ""), f"slashy {label}: key not in the error")
    from urllib.parse import quote

    res = run(SID, "see " + quote(SLASHY, safe=""))
    ok(res["text"] == "", "URL-encoded echo fails")
    ok(SLASHY not in tap_blob(), "slashy key absent from the tap")


def test_clean_answers_are_untouched():
    for ans in (
        "Hallo Welt",
        "sk is a prefix; the product code 3f2a9c1d4b5e6f708192a3b4c5d6e7f8 ships Monday",
        "Bearer bonds and api keys are explained in the sk-guide chapter",
        "Kontakt: fw_update_firmware_config und gsk_ ist ein Praefix",
        "xai-Modelle sind klein",
    ):
        res = run(CID, ans)
        ok(res["text"] == ans and not res["error"], f"clean answer kept: {ans[:30]}")
    blob = tap_blob()
    ok("3f2a9c1d4b5e6f708192a3b4c5d6e7f8" in blob, "ordinary hex product code stays in the tap")


def test_global_and_paired_keys_also_fail():
    old = config.ANTHROPIC_API_KEY
    config.ANTHROPIC_API_KEY = "sk-ant-GLOBALKEY-0123456789abcdefghij"
    try:
        res = run(CID, "Hallo " + config.ANTHROPIC_API_KEY)
        ok(res["text"] == "" and res["error"], "a global key echoed by an engine fails the call")
        ok(config.ANTHROPIC_API_KEY not in tap_blob(), "global key absent from the tap")
    finally:
        config.ANTHROPIC_API_KEY = old
    with Session(engine) as s:
        p = s.get(EngineProfile, CID)
        p.config = {"access_key_secret_enc": engines.enc_key(PAIRED)}
        s.add(p)
        s.commit()
    ok(PAIRED in known_keys(ref=f"engine:{CID}"), "paired secret is a known key")
    res = run(CID, "Hallo " + PAIRED)
    ok(res["text"] == "" and PAIRED not in (res["error"] or ""), "paired secret echo fails")
    ok(PAIRED not in tap_blob(), "paired secret absent from the tap")
    with Session(engine) as s:
        p = s.get(EngineProfile, CID)
        p.config = {}
        s.add(p)
        s.commit()


def test_short_key_does_not_garble_text():
    with Session(engine) as s:
        p = EngineProfile(
            name="lm",
            kind="openai_compatible",
            base_url="https://lm.example.invalid/v1",
            api_key_enc=engines.enc_key("lm-studio"),
            model="m1",
            config={},
        )
        s.add(p)
        s.commit()
        pid = p.id
    ok("lm-studio" not in known_keys(ref=f"engine:{pid}"), "placeholder-length key not enumerated")
    res = run(pid, "lm-studio is a local server")
    ok(res["text"] == "lm-studio is a local server" and not res["error"], "short key text kept")


def test_complete_direct_path():
    class FakeProv:
        name = "engine:x"
        last_usage = None
        profile = engines.get_profile(CID)

        def complete(self, prompt, **kw):
            return "Antwort " + KEY

    try:
        spend.complete_direct(FakeProv(), "p")
        ok(False, "complete_direct returned an echoed key")
    except KeyEchoError as exc:
        ok(KEY not in str(exc), "complete_direct error is key-free")

    class CleanProv(FakeProv):
        def complete(self, prompt, **kw):
            return "Antwort"

    ok(spend.complete_direct(CleanProv(), "p") == "Antwort", "complete_direct clean answer ok")


def test_local_profile_direct_and_glossary():
    # #389: a profile-bound LOCAL engine keeps its credential on `_profile_key` and has no
    # `.profile`, so known_keys used to miss it -- an echoing local server leaked the key
    # through complete_direct into a glossary target_term while run_completion rejected the
    # same echo. known_keys must enumerate the bound credential and the real guard reject it.
    from app.services import glossary_hub
    from app.services.providers import LocalProvider

    class BoundLocal(LocalProvider):
        def __init__(self, echo):
            self._profile_key = KEY  # exactly what _provider_for_profile binds for `local`
            self._echo = echo

        def complete(self, prompt, **kw):
            return self._echo

    ok(KEY in known_keys(BoundLocal("x")), "a bound local _profile_key is a known key")

    try:
        spend.complete_direct(BoundLocal("Antwort " + KEY), "p")
        ok(False, "complete_direct returned an echoed local-profile key")
    except KeyEchoError as exc:
        ok(KEY not in str(exc), "complete_direct local-profile error is key-free")
    ok(spend.complete_direct(BoundLocal("Antwort"), "p") == "Antwort", "clean local answer ok")

    # the glossary path swallows the engine error: it must yield NO candidate carrying the key
    leak = json.dumps([{"source_term": "server", "target_term": KEY, "note": ""}])
    rows = glossary_hub.build_from_text("the server is up", provider=BoundLocal(leak))
    ok(rows == [], f"build_from_text returns no candidates on an echo (got {rows})")
    ok(KEY not in json.dumps(rows), "build_from_text leaked the local-profile key")


def test_local_complete_connection_test():
    # #389: the Setup-AI "Test connection" ping returns its text to the caller, and a blank
    # key field sends the SAVED slot key. Reuse this module's fake transport (it echoes the
    # sent Bearer token), so the answer carries the outbound key -- guard_answer rejects it.
    base = "http://127.0.0.1:1234"
    ANSWER["a"] = "Bonjour {KEY}"
    try:
        local_complete(base, KEY, "gemma-3-it", "Translate to French: Hello")
        ok(False, "local_complete returned an echoed key")
    except KeyEchoError as exc:
        ok(KEY not in str(exc), "local_complete error is key-free")
    ANSWER["a"] = "Bonjour le monde"
    ok(
        local_complete(base, KEY, "gemma-3-it", "Translate to French: Hello") == "Bonjour le monde",
        "clean connection-test answer passes through",
    )


def test_ocr_and_vision_paths():
    from app.services import image_localization, ocr

    old = config.OPENAI_API_KEY
    config.OPENAI_API_KEY = "sk-OPENAIGLOBAL0123456789abcdefghij"
    try:
        try:
            ocr._guarded_vision_call(
                "openai", "m", 10, lambda: ("scan " + config.OPENAI_API_KEY, None)
            )
            ok(False, "ocr returned an echoed key")
        except KeyEchoError:
            pass
        ok(
            ocr._guarded_vision_call("openai", "m", 10, lambda: ("scan text", None))[0]
            == "scan text",
            "clean ocr ok",
        )
        try:
            image_localization._echo_checked("t " + config.OPENAI_API_KEY)
            ok(False, "vision text returned an echoed key")
        except KeyEchoError:
            pass
        ok(image_localization._echo_checked("plain") == "plain", "clean vision text ok")
    finally:
        config.OPENAI_API_KEY = old


def test_shapes_and_jev_redaction():
    for k in (
        "gsk_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0u1V2w3X4y5Z6",
        "xai-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0u1V2w3X4y5Z6",
        "fw_3ZkWSyAu3Z73YatLrHK0rDdr",
    ):
        out, changed = scrub("token " + k + " end", error_channel=False)
        ok(changed and k not in out, f"shape scrubbed: {k[:6]}")
    out, changed = scrub(
        "fw_update_firmware_config gsk_ xai-Modelle sk and 3f2a9c1d4b5e6f70", error_channel=False
    )
    ok(not changed, "no false positive on ordinary text")
    # the Jev path keeps its redaction
    red = qe_jev.redact({"answer": "hi " + KEY}, KEY)
    ok(KEY not in json.dumps(red), "qe_jev.redact still removes the key")
    ok(KEY not in redact_diagnostic("a " + KEY + " b", [KEY]), "redact_diagnostic removes it")


def main():
    test_echo_shapes_fail_the_call_and_never_store_the_key()
    test_echo_without_usage_block_is_still_billed()
    test_special_character_key_spellings()
    test_clean_answers_are_untouched()
    test_global_and_paired_keys_also_fail()
    test_short_key_does_not_garble_text()
    test_complete_direct_path()
    test_local_profile_direct_and_glossary()
    test_local_complete_connection_test()
    test_ocr_and_vision_paths()
    test_shapes_and_jev_redaction()
    httpx.Client.send = _orig_send
    prompt_tap.set_enabled(False)
    print("ENGINE KEY ECHO OK")


if __name__ == "__main__":
    main()
