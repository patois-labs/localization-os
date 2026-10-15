"""Offline issue 221 regressions captured inside the actual Provider boundary."""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_pii_all_")

from sqlmodel import Session

from app.db import engine, init_db
from app.models import Document, Project, Segment, SegStatus, TransMode
from app.services import (
    assist,
    image_localization,
    lqa,
    pii,
    providers_part3,
    qe_jev,
    settings,
    term_extraction,
    tm,
    translate,
)
from app.services.pii_boundary import RequestRedaction
from app.services.providers import register_provider
from app.services.providers_common import Provider

VALUES = ("jane.doe@example.edu", "123-45-6789", "555-867-5309", "4111 1111 1111 1111")
SOURCE = "Please contact " + "; account ".join(VALUES) + " about the transcript."
SEEN = []


class Recording(Provider):
    name = "piirecording"

    def complete(self, prompt, system=None, task=None, payload=None, **kw):
        SEEN.append((prompt, system, payload, kw))
        payload = payload or {}
        if task == "lqa":
            return json.dumps({"errors": []})
        if task == "prune_terms":
            return json.dumps(payload["candidates"])
        if task == "translate_terms":
            return json.dumps({term: term for term in payload["terms"]})
        return payload.get("text", prompt)


def captured(action, enabled=True):
    SEEN.clear()
    result = action()
    assert SEEN, "case must reach the engine"
    wire = json.dumps(SEEN, ensure_ascii=False)
    for value in VALUES:
        assert (value not in wire) if enabled else (value in wire), (value, wire)
    return result


def pretranslate():
    with Session(engine) as session:
        project = Project(
            name="PII regression", source_locale="en-US", default_provider="piirecording"
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        tm.write_back(session, project.id, SOURCE, SOURCE, "en-US", "es-ES")
        doc = Document(
            project_id=project.id, filename="pii.txt", fmt="txt", mode=TransMode.SEGMENTED
        )
        session.add(doc)
        session.commit()
        session.refresh(doc)
        source = SOURCE.replace("transcript", "new transcript")
        segment = Segment(
            document_id=doc.id,
            target_locale="es-ES",
            order_index=0,
            source_text=source,
            target_text="",
            status=SegStatus.NEW,
        )
        session.add(segment)
        session.commit()
        segment_id = segment.id
        list(translate.pretranslate_stream(session, doc.id, "es-ES", doc_context_prepass=True))
        session.expire_all()
        assert session.get(Segment, segment_id).target_text == source
        assert any("<tm_examples>" in row[0] for row in SEEN), "must exercise fuzzy TM"
        assert any(row[0].startswith("SAMPLE:") for row in SEEN), "must exercise doc brief"


def judge():
    with patch.object(lqa, "provider_is_live", return_value=True):
        result = lqa._llm_errors(
            SOURCE,
            SOURCE,
            "en",
            "fr",
            SOURCE,
            "piirecording",
            style_guide=SOURCE,
        )
    assert result[4] == lqa.JUDGE_OK


def terms():
    provider = Recording()
    assert term_extraction._prune(list(VALUES), SOURCE, provider)[0] == list(VALUES)
    result, _ = term_extraction._propose_translations(list(VALUES), "en", "fr", provider)
    assert result == {value: value for value in VALUES}


def help_assistant():
    with (
        patch.object(assist, "is_mock_engine", return_value=False),
        patch.object(assist, "engine_is_live", return_value=True),
    ):
        result = assist.ask("", SOURCE, provider="piirecording")
    assert all(value in result["answer"] for value in VALUES)


def boundary():
    # Identical OFF arguments, including object identity, and no caller mutation ON.
    provider = Recording()
    payload = {"text": SOURCE, "messages": [{"role": "user", "content": SOURCE}]}
    settings.set_many({"pii_redaction": "0"})
    captured(lambda: provider.complete(SOURCE, SOURCE, payload=payload), enabled=False)
    assert SEEN[0] == (SOURCE, SOURCE, payload, {})
    assert SEEN[0][2] is payload
    settings.set_many({"pii_redaction": "1"})
    assert captured(lambda: provider.complete(SOURCE, SOURCE, payload=payload)) == SOURCE
    assert payload["text"] == SOURCE
    # Existing classic tokens, across messages, must never alias boundary tokens.
    classic, mapping = pii.redact(SOURCE)
    combined = (classic, SOURCE, "«PII_EMAIL_99»")
    redaction = RequestRedaction(combined)
    masked = redaction.redact(combined)
    assert masked[0] == classic and masked[2] == combined[2]
    assert redaction.restore(masked) == combined
    assert pii.restore(redaction.restore(masked)[0], mapping) == SOURCE
    assert json.loads(redaction.restore(json.dumps(masked))) == list(combined)
    assert json.loads(redaction.restore(json.dumps(masked, ensure_ascii=False))) == list(combined)
    captured(lambda: provider.complete("hello", messages=[{"content": SOURCE}]))


def other_transports():
    def vision(_image, prompt):
        SEEN.append(prompt)
        return prompt

    with patch.object(image_localization, "vision_call_override", vision):
        assert captured(lambda: image_localization._guarded_vision_call(b"", SOURCE)) == SOURCE

    def create(**kwargs):
        SEEN.append(kwargs)
        text = kwargs["messages"][-1]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=lambda: None,  # #437: local_complete closes the client it was handed
    )
    with patch.object(providers_part3, "_local_client", return_value=client):
        result = captured(lambda: providers_part3.local_complete("", "", "mock", SOURCE, SOURCE))
        assert result == SOURCE

    class HttpClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, *, headers, json):
            SEEN.append(json)
            return SimpleNamespace(status_code=200, json=lambda: json)

    with patch.object(qe_jev.httpx, "Client", HttpClient):
        body = {"messages": [{"content": SOURCE}], "system": SOURCE}
        assert captured(lambda: qe_jev._post(body)) == (200, body)

    def nmt_transport(request):
        body = json.loads(request.content)
        SEEN.append(body)
        if request.url.path == "/v2/translate":
            return httpx.Response(200, json={"translations": [{"text": body["context"]}]})
        return httpx.Response(200, json={"translation": body["instructions"][0]})

    profile = SimpleNamespace(
        base_url="https://pii.invalid",
        api_key_enc="",
        config={"context": SOURCE, "custom_instructions": [SOURCE], "instructions": [SOURCE]},
    )
    transport = httpx.MockTransport(nmt_transport)
    for cls in (providers_part3.DeepLProvider, providers_part3.LaraProvider):
        provider = cls(profile=profile, transport=transport)
        with patch.object(
            providers_part3,
            "local_engine_client_kwargs",
            return_value={"base_url": profile.base_url, "transport": transport},
        ):
            result = captured(
                lambda provider=provider: provider.complete(
                    SOURCE, payload={"text": SOURCE, "target": "fr"}
                )
            )
            assert result == SOURCE
        assert profile.config["instructions"] == [SOURCE]


def main():
    init_db()
    register_provider("piirecording", Recording())
    boundary()
    other_transports()
    for action in (pretranslate, judge, terms, help_assistant):
        captured(action)
        # Reproduce the unfixed provider boundary without replacing run_completion.
        # Every feature must independently detect the original leak.
        original = Recording.complete.__wrapped__.__wrapped__
        with patch.object(Recording, "complete", original):
            try:
                captured(action)
            except AssertionError:
                assert SEEN and any(value in str(SEEN) for value in VALUES)
            else:
                raise AssertionError(f"{action.__name__} did not catch the base leak")
        settings.set_many({"pii_redaction": "0"})
        captured(action, enabled=False)
        settings.set_many({"pii_redaction": "1"})
        print(action.__name__, "PASS")
    print("PII ALL PATHS OK")


if __name__ == "__main__":
    main()
