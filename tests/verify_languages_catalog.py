"""Language catalog + enabled-languages registry (P2, item 1) — offline, hermetic.

Covers:
  - the static BCP-47 catalog loads (name/native/code), is_valid normalizes
    (case-insensitive + underscore), bare + legacy codes handled, garbage rejected;
  - default enabled subset == the digest's top-30 (exact codes + order);
  - enable/disable round-trip via BOTH the service AND the CLI (parity), persisted
    as the enabled_locales Setting;
  - grandfathering: a disabled-but-in-use locale still renders on the project page;
  - project create/add-locale reject garbage but accept a disabled-but-valid code;
  - the project-create picker route renders the enabled subset + the escape hatch;
  - the "Enabled languages" settings page renders + saves;
  - the living-projects add-language flow seeds segments + reopens (P1 hook).

Hermetic like the rest: throwaway LOC_TMS_DATA_DIR + cleared provider keys at
import (forces MockProvider, touches no real data). ASCII-only prints.
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_langcat_")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.cli.__main__ import main as cli_main
from app.db import engine, init_db
from app.main import app
from app.models import (
    Document,
    Project,
    ProjectStatus,
    Segment,
    TargetLocale,
)
from app.services import content, languages, languages_catalog, settings

TOP_30 = [
    "en-US",
    "en-GB",
    "zh-Hans",
    "zh-Hant",
    "es-ES",
    "es-419",
    "hi-IN",
    "ar",
    "pt-BR",
    "pt-PT",
    "fr-FR",
    "fr-CA",
    "ja-JP",
    "ru-RU",
    "de-DE",
    "id-ID",
    "ko-KR",
    "it-IT",
    "tr-TR",
    "pl-PL",
    "nl-NL",
    "vi-VN",
    "th-TH",
    "uk-UA",
    "bn-BD",
    "ur-PK",
    "fa-IR",
    "ro-RO",
    "sv-SE",
    "nb-NO",
]


def _cli_json(argv):
    """Run the CLI in-process with --json, return the parsed last stdout line."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(["--json"] + argv)  # noqa: RUF005
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    text = out.getvalue().strip()
    payload = json.loads(text.splitlines()[-1]) if text else {}
    return code, payload, err.getvalue()


def main() -> None:
    init_db()

    # ---- catalog loads + validation ----
    codes = languages_catalog.all_codes()
    assert len(codes) >= 100, f"catalog too small: {len(codes)}"
    rows = languages_catalog.all_locales()
    assert all("code" in r and "name" in r for r in rows), "catalog rows missing fields"
    # script/region variants present as separate rows (digest requirement)
    for c in ("zh-Hans", "zh-Hant", "es-419", "es-MX", "pt-BR", "pt-PT", "fr-CA", "en-GB"):
        assert languages_catalog.is_valid(c), f"{c} should be in the catalog"
    # normalization: case-insensitive + underscore forms
    assert languages_catalog.is_valid("ES-es") and languages_catalog.is_valid("es_ES")
    # bare ISO-639-1 codes (legacy/freeform) validate + display
    assert languages_catalog.is_valid("es") and languages_catalog.is_valid("en")
    # garbage is rejected
    assert not languages_catalog.is_valid("zz-ZZ")
    assert not languages_catalog.is_valid("bogus")
    assert not languages_catalog.is_valid("")
    # name/label helpers
    assert "French" in languages_catalog.name_for("fr-FR")
    assert languages_catalog.name_for("zz-ZZ") == "zz-ZZ", "legacy code displays as-is"
    assert languages_catalog.label_for("fr-FR") == "French (France) (fr-FR)"
    print("  catalog: loads, is_valid normalizes, bare/legacy handled, garbage rejected")

    # ---- default enabled == top-30 (exact codes + order) ----
    assert languages_catalog.DEFAULT_ENABLED == TOP_30, "DEFAULT_ENABLED drifted"
    assert languages_catalog.enabled_locales() == TOP_30, "default enabled != top-30"
    assert len(languages_catalog.enabled_language_rows()) == 30
    print("  default enabled subset == the digest's top-30 (exact codes + order)")

    # ---- validate_codes: canonicalize valid, collect invalid ----
    valid, invalid = languages_catalog.validate_codes("fr-FR, es_ES, garbage, ja-JP")
    assert valid == ["fr-FR", "es-ES", "ja-JP"] and invalid == ["garbage"], (valid, invalid)
    print("  validate_codes: canonicalizes valid, flags garbage")

    # ---- enable/disable round-trip via the SERVICE ----
    stored = languages_catalog.set_enabled(["fr-FR", "de-DE", "ja-JP", "notacode"])
    assert stored == ["fr-FR", "de-DE", "ja-JP"], f"unknown code not dropped: {stored}"
    assert languages_catalog.enabled_locales() == ["fr-FR", "de-DE", "ja-JP"]
    assert settings.get("enabled_locales") == json.dumps(["fr-FR", "de-DE", "ja-JP"])
    # restore the default top-30 for the rest of the suite (blank falls back)
    settings.set_many({"enabled_locales": ""})
    assert languages_catalog.enabled_locales() == TOP_30
    print("  enable/disable round-trip (service): unknown dropped, persisted as Setting")

    # ---- enable/disable round-trip via the CLI (parity) ----
    code, payload, err = _cli_json(["lang", "list", "--enabled"])
    assert code == 0 and payload["ok"], (code, err)
    assert payload["enabled_count"] == 30, payload["enabled_count"]
    code, payload, _ = _cli_json(["lang", "list", "--all", "--search", "chinese"])
    assert code == 0 and any(r["code"] == "zh-Hans" for r in payload["languages"])
    code, payload, err = _cli_json(["lang", "enable", "haw"])
    assert code == 0 and "haw" in payload["enabled"], (code, payload, err)
    assert "haw" in languages_catalog.enabled_locales()
    code, payload, _ = _cli_json(["lang", "disable", "haw"])
    assert code == 0 and "haw" not in languages_catalog.enabled_locales(), payload
    # garbage code -> usage error, nothing changed (--json puts the error in payload)
    code, payload, err = _cli_json(["lang", "enable", "definitely-bogus"])
    assert code == 2 and "unknown" in str(payload.get("error", "")).lower(), (code, payload, err)
    settings.set_many({"enabled_locales": ""})  # reset to default
    print("  lang list/enable/disable via CLI (parity); garbage rejected (exit 2)")

    c = TestClient(app)

    # ---- project create rejects garbage, accepts a disabled-but-valid code ----
    # First shrink the enabled subset so 'ja-JP' is NOT offered (but still valid).
    languages_catalog.set_enabled(["fr-FR", "de-DE"])
    with Session(engine) as s:
        # disabled-but-valid: ja-JP is a real catalog code, just not enabled
        proj = content.project_create(
            s, "Valid Disabled", source_locale="en-US", target_locales="fr-FR, ja-JP"
        )
        assert proj is not None
        locs = {
            tl.locale
            for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == proj.id)).all()
        }
        assert locs == {"fr-FR", "ja-JP"}, locs
    # garbage -> InvalidLocaleError
    with Session(engine) as s:
        try:
            content.project_create(s, "Bad", target_locales="fr-FR, zz-ZZ")
            raise AssertionError("garbage locale should have raised")
        except content.InvalidLocaleError as exc:
            assert "zz-ZZ" in exc.invalid, exc.invalid
    settings.set_many({"enabled_locales": ""})
    print("  project create: accepts disabled-but-valid, rejects garbage")

    # ---- web project-create rejects garbage with a friendly redirect ----
    r = c.post(
        "/projects",
        data={
            "name": "WebBad",
            "source_locale": "en-US",
            "target_locales": "zz-ZZ",
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers["location"], r.headers.get("location")
    # web create with a multi-checkbox post (the picker) works
    r = c.post(
        "/projects",
        data={
            "name": "WebPicker",
            "source_locale": "en-US",
            "target_locales": ["fr-FR", "de-DE"],
            "default_provider": "mock",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/projects/" in r.headers["location"], r.headers["location"]
    print("  web project create: picker multi-post works; garbage redirects with err")

    # ---- picker route renders enabled subset + escape hatch ----
    page = c.get("/projects").text
    assert "data-langpicker" in page, "picker not rendered on /projects"
    assert "Show all languages" in page, "escape hatch missing"
    assert 'value="fr-FR"' in page and 'value="en-US"' in page, "enabled options missing"
    print("  /projects picker: renders enabled subset + 'show all' escape hatch")

    # ---- Enabled-languages settings page renders + saves ----
    page = c.get("/settings/languages").text
    assert "Enabled languages" in page and "data-langpicker" in page
    r = c.post(
        "/settings/languages", data={"enabled": ["fr-FR", "de-DE", "ja-JP"]}, follow_redirects=False
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    assert languages_catalog.enabled_locales() == ["fr-FR", "de-DE", "ja-JP"]
    settings.set_many({"enabled_locales": ""})
    print("  /settings/languages: renders grid, saves the enabled subset")

    # ---- grandfathering: a disabled-but-in-use locale still renders on the project ----
    languages_catalog.set_enabled(["fr-FR"])  # ja-JP NOT enabled
    with Session(engine) as s:
        gp = Project(
            name="Grandfathered", source_locale="en-US", default_provider="mock", created_by="admin"
        )
        s.add(gp)
        s.commit()
        s.refresh(gp)
        gpid = gp.id
        s.add(TargetLocale(project_id=gpid, locale="ja-JP"))  # legacy, now-disabled
        s.add(TargetLocale(project_id=gpid, locale="xx-Legacy"))  # not even in catalog
        s.commit()
    detail = c.get(f"/projects/{gpid}").text
    assert "ja-JP" in detail, "disabled-but-in-use locale must still render"
    assert "xx-Legacy" in detail, "legacy non-catalog locale must still render"
    settings.set_many({"enabled_locales": ""})
    print("  grandfathering: disabled + legacy locales still render on the project page")

    # ---- add-locale via service: validate, seed segments, reopen (P1 hook) ----
    with Session(engine) as s:
        lp = Project(
            name="Living",
            source_locale="en-US",
            default_provider="mock",
            created_by="admin",
            status=ProjectStatus.COMPLETED,
        )
        s.add(lp)
        s.commit()
        s.refresh(lp)
        lpid = lp.id
        s.add(TargetLocale(project_id=lpid, locale="fr-FR"))
        s.commit()
        doc = Document(project_id=lpid, filename="a.txt", fmt="txt")
        s.add(doc)
        s.commit()
        s.refresh(doc)
        did = doc.id
        # ingest-seeded segments for the existing locale
        for i, txt in enumerate(["Hello world", "Second sentence"]):
            s.add(Segment(document_id=did, target_locale="fr-FR", order_index=i, source_text=txt))
        s.commit()

    # garbage add-locale raises
    with Session(engine) as s:
        try:
            content.add_target_locale(s, lpid, "zz-ZZ")
            raise AssertionError("garbage add-locale should raise")
        except content.InvalidLocaleError:
            pass

    with Session(engine) as s:
        added = content.add_target_locale(s, lpid, "de-DE")
        assert added is True
        # segments seeded for the new locale, cloned from the existing one
        de_segs = s.exec(
            select(Segment).where(Segment.document_id == did, Segment.target_locale == "de-DE")
        ).all()
        assert len(de_segs) == 2, f"expected 2 seeded segments, got {len(de_segs)}"
        assert {sg.source_text for sg in de_segs} == {"Hello world", "Second sentence"}
        # P1 reopen: COMPLETED -> IN_TRANSLATION
        lp = s.get(Project, lpid)
        assert lp.status == ProjectStatus.IN_TRANSLATION, lp.status
    # idempotent
    with Session(engine) as s:
        assert content.add_target_locale(s, lpid, "de-DE") is False
    # dedup is NORMALIZED, not raw-string: a non-canonical existing row (a pre-P2
    # row or a connector's unvalidated free-text config, e.g. "ES-es") must not be
    # forked into a second row by a canonical add ("es_ES" -> "es-ES"). Otherwise
    # downstream (segment seeding, TM buckets, per-locale delivery) splits one
    # locale into two.
    with Session(engine) as s:
        dp = Project(
            name="NonCanon", source_locale="en-US", default_provider="mock", created_by="admin"
        )
        s.add(dp)
        s.commit()
        s.refresh(dp)
        dpid = dp.id
        s.add(TargetLocale(project_id=dpid, locale="ES-es"))  # legacy spelling
        s.commit()
    with Session(engine) as s:
        assert content.add_target_locale(s, dpid, "es_ES") is False, (
            "canonical add must dedup against a non-canonical existing row"
        )
        locs = [
            tl.locale
            for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == dpid)).all()
        ]
        assert locs == ["ES-es"], f"case/underscore duplicate created: {locs}"
    # the same normalized dedup guards the Language-Set merge
    with Session(engine) as s:
        nc_ls = languages.create("NC", "en-US", "es_ES, de-DE")
        added_ls = content.apply_language_set_to_project(s, dpid, nc_ls)
        locs = [
            tl.locale
            for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == dpid)).all()
        ]
        assert added_ls == 1 and locs.count("ES-es") == 1 and "es-ES" not in locs, (
            f"langset merge forked a locale: added={added_ls} locs={locs}"
        )
    print("  add-locale / langset merge: normalized dedup (no case/underscore fork)")
    print("  add-locale: validates, seeds segments for the new locale, reopens (P1)")

    # ---- web add-locale route ----
    r = c.post(f"/projects/{lpid}/add-locale", data={"locale": ["es-ES"]}, follow_redirects=False)
    assert r.status_code == 303 and "locale_added=1" in r.headers["location"], r.headers.get(
        "location"
    )
    with Session(engine) as s:
        locs = {
            tl.locale
            for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == lpid)).all()
        }
        assert "es-ES" in locs, locs
    # web add-locale garbage -> err redirect
    r = c.post(f"/projects/{lpid}/add-locale", data={"locale": ["zz-ZZ"]}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    print("  web /projects/{id}/add-locale: adds a valid locale; garbage -> err redirect")

    # ---- language sets validate against the catalog ----
    ls = languages.create("Good", "en-US", "fr-FR, de-DE")
    assert ls.target_locales == ["fr-FR", "de-DE"]
    try:
        languages.create("Bad", "en-US", "fr-FR, garbage")
        raise AssertionError("langset with garbage should raise")
    except languages.InvalidLocaleError as exc:
        assert "garbage" in exc.invalid
    print("  language sets: validate targets against the catalog")

    print("LANGUAGES_CATALOG OK")


if __name__ == "__main__":
    main()
