"""Hermetic Jev QE contracts. Run only through test_verify_suites.py."""

import asyncio
import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

_TMP = init_env("loctms_jev_")
os.environ["TYPESAFE_API_KEY"] = ""

import httpx

from app import config
from app.db import init_db
from app.services import qe, qe_jev, settings, spend
from app.services.providers import ProviderSpec


def answer(**levels):
    return {
        "model": "jev-1.13.0",
        "answers": {
            **{
                c: {
                    "type": "score",
                    "probabilities": [float(i == levels.get(c, 0)) for i in range(4)],
                    "confidence": 0.9,
                }
                for c in qe.QE_CATEGORIES
            },
            "meaning_changed": {"type": "noul", "noul": 0.1},
        },
        "usage": {"input_tokens": 1000, "output_tokens": 20},
    }


def score():
    return qe_jev.score_segment("Pay ten euros.", "Zahlen Sie zehn Euro.", "en", "de")


def main():
    init_db()
    with patch.object(qe_jev, "_post") as post:
        assert score()["reason"] == "not_configured"
        post.assert_not_called()
        config.TYPESAFE_API_KEY = "fake"  # pregate: allow-secret placeholder, HTTP is patched
        settings.set_many({"qe_jev_enabled": "0"})
        assert score()["reason"] == "jev_disabled"
        post.assert_not_called()
    settings.set_many({"qe_jev_enabled": "1"})
    scoring_train_cases()
    fix_round_one()
    followups_335()
    number_gaps_367()
    coordinate_negatives_458()
    test_no_veto()
    assert settings.DEFAULTS["qe_jev_enabled"] == "1"
    assert settings.DEFAULTS["qe_jev_confidence_floor"] == "0.6"
    mock_card = qe.score_segment(ProviderSpec(provider="mock"), "a", "b", "en", "de")
    for levels, expected in (({}, 100), ({"accuracy": 2}, 60), ({"style": 3}, 85)):
        with patch.object(qe_jev, "_post", return_value=(200, answer(**levels))) as post:
            before = spend.spent_this_period()
            card = score()
            assert set(card) == set(mock_card) | {"unsure"}, card
            assert card["score"] == expected, card
            assert abs(card["cost_usd"] - 1000 * 0.042e-6) < 1e-12
            assert spend.spent_this_period() > before
            body = post.call_args.args[0]
            assert body["model"] == config.TYPESAFE_MODEL
            assert set(body["state"]) == {"source", "translation", "source_locale", "target_locale"}
            assert set(body["questions"]) == {*qe.QE_CATEGORIES, "meaning_changed"}
            assert (
                body["questions"]["meaning_changed"]["instructions"]
                == qe_jev.INSTRUCTIONS["meaning_changed"]
            )
            for c in qe.QE_CATEGORIES:
                question = body["questions"][c]
                assert question["instructions"] == qe_jev.INSTRUCTIONS[c]
                assert question["type"] == "score" and len(question["criteria"]) == 4
                assert all(set(level) == {"summary", "signals"} for level in question["criteria"])
    data = answer()
    data["answers"]["meaning_changed"]["noul"] = 0.8
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        assert score()["categories"]["accuracy"] == 2
    data["answers"]["meaning_changed"]["noul"] = 0.799
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        assert score()["score"] == 100
    data = answer(accuracy=1)
    data["answers"]["meaning_changed"]["noul"] = 1
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        assert score()["score"] == 60
    data = answer()
    data["answers"]["fluency"]["confidence"] = 0.59
    del data["answers"]["fluency"]["probabilities"]
    data["answers"]["fluency"]["score"] = 0
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        card = score()
        assert card["reason"] == "low_confidence" and "fluency" in card["error"]
        assert card["cost_usd"] > 0
    settings.set_many({"qe_jev_confidence_floor": "0.5"})
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        assert not score()["skipped"]
    settings.set_many({"qe_jev_confidence_floor": "0.6"})
    for responses, reason, calls in (
        ([(429, {}), (200, answer())], None, 2),
        ([(529, {}), (200, answer())], None, 2),
        ([httpx.ConnectError("offline"), (200, answer())], None, 2),
        ([(429, {}), (429, {})], "provider_error", 2),
        ([httpx.ReadTimeout("timeout"), httpx.ReadTimeout("timeout")], "provider_error", 2),
        ([(401, {})], "provider_error", 1),
        ([(422, {})], "provider_error", 1),
        ([(200, "junk")], "junk_json", 1),
    ):
        with (
            patch.object(qe_jev, "_post", side_effect=responses) as post,
            patch.object(qe_jev.time, "sleep") as sleep,
        ):
            assert score()["reason"] == reason
            assert post.call_count == calls
            assert sleep.call_count == calls - 1
            if calls == 2:
                sleep.assert_called_once_with(1.0)
    data = answer()
    del data["answers"]["style"]
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        before = spend.spent_this_period()
        card = score()
        assert card["reason"] == "junk_json" and card["tokens_in"] == 1000
        assert card["cost_usd"] > 0 and spend.spent_this_period() > before
    data = answer()
    del data["answers"]["accuracy"]["probabilities"]
    data["answers"]["accuracy"]["score"] = 1.6
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        assert score()["score"] == 60
    for bad in (None, [], {}, [0, 0, 0, 0], [float("nan"), 0, 0, 1]):
        data = copy.deepcopy(answer())
        data["answers"]["accuracy"]["probabilities"] = bad
        with patch.object(qe_jev, "_post", return_value=(200, data)):
            assert score()["reason"] == "junk_json"
    with patch.object(qe_jev, "_post") as post:
        card = qe_jev.score_segment("a", "  ", "en", "de")
        assert card["score"] == 0 and card["rationale"] == "empty translation"
        assert set(card) == set(mock_card) | {"unsure"}
        post.assert_not_called()
    test_fix_round_1()
    test_lab()
    test_observers_and_cap()
    test_dispatch_and_surfaces()
    test_cli()
    print("verify_qe_jev OK")


def followups_335():
    """#335: N4 advisory minor number findings, hard MAJOR kept; R3-1 trailing punctuation."""
    from app.services import qe_autofix
    from app.services import qe_jev_structural as structural

    def worst(*args):
        sev = {f["severity"] for f in structural.findings(*args)}
        return "major" if "major" in sev else ("minor" if sev else None)

    # N4: the number checker's own MINOR findings are advisory, never a block.
    advisory = (
        ("Meet at 6:45 PM", "Tapaaminen klo 18.45", "en", "fi"),
        ("Meet at 6:45 PM", "Treffen um 18.45 Uhr", "en", "de"),
        ("Meet at 6:45 PM", "Møde kl. 18.45", "en", "da"),
        ("Meet at 6:45 PM", "Møte kl. 18.45", "en", "nb"),
        ("You have 2 items", "Vous avez deux articles", "en", "fr"),
    )
    for args in advisory:
        hits = structural.findings(*args)
        assert hits and all(f["severity"] == "minor" for f in hits), (args, hits)
        assert all(f["dimension"] == "numbers" for f in hits), hits
        with patch.object(qe_jev, "_post", return_value=(200, answer())):
            card = qe_jev.score_segment(*args)
        assert not card["structural_blocked"] and card["structural_findings"], (args, card)
        assert qe_autofix.gate_decision(card, 70) == "passed", (args, card)
    # Dangerous direction: a real mismatch must still hard-block as MAJOR.
    blocking = (
        ("Meet at 6:45", "Rendez-vous 7:45", "en", "fr"),
        ("Meet at 6:45 PM", "Rendez-vous 7:45", "en", "fr"),
        ("Meet at 6:45 AM", "Rendez-vous 6:45 PM", "en", "en"),
        ("Meet at 6:45 AM", "Rendez-vous 18 h 45", "en", "fr"),
        ("Meet at 6:45 PM", "Rendez-vous 6 h 45", "en", "fr"),
        ("You have 5 items", "Vous avez 50 articles", "en", "fr"),
        ("Pay 25 euros.", "Payez 26 euros.", "en", "fr"),
    )
    for args in blocking:
        assert worst(*args) == "major", (args, structural.findings(*args))
        with patch.object(qe_jev, "_post", return_value=(200, answer())):
            card = qe_jev.score_segment(*args)
        assert card["structural_blocked"], (args, card)
        assert qe_autofix.gate_decision(card, 0) == "failed", (args, card)
    # A MINOR number finding next to a MAJOR one still blocks.
    mixed = structural.findings("5 items at 6:45 PM", "cinq articles a 7:45", "en", "fr")
    assert any(f["severity"] == "major" for f in mixed), mixed
    # Gate fix: the checker grades a dropped or added number MINOR ("count") whether it
    # was spelled out or really lost, and a wrong dot time is the same class. Only a
    # dot time equal to the source time or a digit written as a word is advisory.
    real = (
        ("You have 3 items", "Vous avez des articles", "en", "fr"),  # dropped
        ("Save now", "Sauvez 5 maintenant", "en", "fr"),  # invented
        ("Pay 25 euros", "Payez 25 euros dans 3 jours", "en", "fr"),  # extra
        ("You have 2 items", "Sie haben drei Artikel", "en", "de"),  # wrong word
        ("You have three items", "Vous avez 4 articles", "en", "fr"),
        ("You have 3 items", "您有一些项目", "en", "zh"),
        ("Wait 11 minutes", "Attendez onze minutes", "en", "fr"),  # no word table past 10
        ("Meet at 6:45 PM", "Treffen um 19.45 Uhr", "en", "de"),  # wrong dot time
        ("Meet at 6:45 PM", "Tapaaminen klo 7.45", "en", "fi"),
        ("Meet at 6:45 AM", "Treffen um 18.45 Uhr", "en", "de"),  # AM/PM swap, dot form
        ("Meet at 6:45 PM", "Rendez-vous a 18 h", "en", "fr"),  # minutes lost
        ("Pages 1-10", "Pages 110", "en", "fr"),  # regrouped digits
        ("Price 1.5", "Price 1,500", "en", "ar"),  # ambiguous rendering
        ("Room 5 at 6:45 PM", "Salle 18 a cinq", "en", "fr"),
    )
    for args in real:
        assert worst(*args) == "major", (args, structural.findings(*args))
    explained = (
        ("You have 2 items", "Sie haben zwei Artikel", "en", "de"),
        ("You have 3 items", "您有三个项目", "en", "zh"),
        ("You have three items", "Vous avez 3 articles", "en", "fr"),
        ("Meet at 6:45 AM", "Rendez-vous a 6.45", "en", "fr"),
        ("At 6:45 PM pay 2.30", "Um 18.45 Uhr 2,30 zahlen", "en", "de"),
        ("5 items at 6:45 PM", "cinq articles a 18.45", "en", "fr"),
    )
    for args in explained:
        assert worst(*args) == "minor", (args, structural.findings(*args))

    # R3-1: trailing punctuation (ASCII or full-width) does not turn a short word
    # in a spaceless script into multi-word text.
    for word in ("保存！", "保存!", "保存。", "保存？", "请重启应用", "请重启应用。"):
        assert structural._single_token(word), word
        hits = structural.findings(word, word, "zh", "en")
        assert hits and not any(f["severity"] == "major" for f in hits), (word, hits)
    # Interior sentence punctuation or a long run is still multi-word text.
    for text in ("保存！退出", "请重新启动应用程序。"):
        assert not structural._single_token(text), text
        assert worst(text, text, "zh", "en") == "major", text


def number_gaps_367():
    """#367: a dropped time, a changed date, a lost minus and a swapped currency block."""
    from app.services import qe_autofix
    from app.services import qe_jev_structural as structural

    def worst(*args):
        sev = {f["severity"] for f in structural.findings(*args)}
        return "major" if "major" in sev else ("minor" if sev else None)

    blocking = (
        # A time dropped entirely, in each target script.
        ("Meet at 6:45 PM", "Treffen", "en", "de"),
        ("Meet at 6:45 PM", "Rendez-vous demain", "en", "fr"),
        ("Meet at 6:45 PM", "Tapaaminen huomenna", "en", "fi"),
        ("Meet at 6:45 PM", "明日の会議", "en", "ja"),
        ("Meet at 6:45 PM", "明天开会", "en", "zh"),
        ("Meet at 6:45 PM", "الاجتماع غدا", "en", "ar"),
        ("Meet at 6:45 PM", "Reunión mañana", "en", "es-MX"),
        ("Meet at 18:45", "Rendez-vous", "en-GB", "fr"),
        ("会議は午後6時45分です", "The meeting", "ja", "en"),
        # A CJK clock time that changed.
        ("Meet at 6:45 PM", "午後7時45分に会議", "en", "ja"),
        ("Meet at 6:45 PM", "下午7点45分开会", "en", "zh"),
        ("Meet at 6:45 AM", "午後6時45分に会議", "en", "ja"),
        # A number dropped entirely.
        ("You have 3 items", "Sie haben Artikel", "en", "de"),
        ("You have 3 items", "項目があります", "en", "ja"),
        ("You have 3 items", "لديك عناصر", "en", "ar"),
        # A date whose day, month or year changed.
        ("Due 3/14/2026", "Fällig 15.03.2026", "en-US", "de"),
        ("Due 3/14/2026", "Échéance 15/03/2026", "en-US", "fr"),
        ("Due 3/14/2026", "Erääntyy 15.3.2026", "en-US", "fi"),
        ("Due 3/14/2026", "期限 2026/03/15", "en-US", "ja"),
        ("Due 3/14/2026", "到期 2026/03/15", "en-US", "zh"),
        ("Due 3/14/2026", "تاريخ الاستحقاق 15/03/2026", "en-US", "ar"),
        ("Due 3/14/2026", "Vence 15/03/2026", "en-US", "es-MX"),
        ("Due 3/14/2026", "Due 15/03/2026", "en-US", "en-GB"),
        ("Due 3/14/2026", "Échéance 14/04/2026", "en-US", "fr"),
        ("Due 3/14/2026", "Échéance 14/03/2027", "en-US", "fr"),
        ("Due 3/4/2026", "Fällig 03.04.2026", "en-US", "de"),  # 4 March -> 3 April
        ("Due 03/04/2026", "Fällig 04.03.2026", "en-GB", "de"),  # 3 April -> 4 March
        ("Due 2026-03-14", "Fällig 2026-03-15", "en", "de"),
        ("Due 2026-03-14", "Fällig 15.03.2026", "en", "de"),
        ("Due 3/14/2026", "Fällig", "en-US", "de"),  # dropped
        ("Due 2026-03-14", "到期", "en", "zh"),
        ("Meet on 3/14", "Treffen am 15/03", "en-US", "de"),
        ("Meet on 3/14", "Réunion le 14/04", "en-US", "fr"),
        # A lost minus sign, every glyph and locale.
        ("Balance -5", "Saldo 5", "en", "de"),
        ("Balance -5", "Solde 5", "en", "fr"),
        ("Balance -5", "Saldo 5", "en", "fi"),
        ("Balance -5", "残高 5", "en", "ja"),
        ("Balance -5", "余额 5", "en", "zh"),
        ("Balance -5", "الرصيد 5", "en", "ar"),
        ("Balance -5", "Saldo 5", "en", "es-MX"),
        ("Balance −5", "Saldo 5", "en", "de"),
        ("Balance –5", "Saldo 5", "en", "de"),
        ("Balance -$25.00", "Saldo 25,00 $", "en", "de"),
        ("Balance ($25.00)", "Saldo 25,00 $", "en", "de"),
        ("Balance (25.00 USD)", "Solde 25,00 USD", "en", "fr"),
        # A currency swapped or dropped.
        ("Pay $25", "Zahle 25 EUR", "en", "de"),
        ("Pay $25", "Payez 25 €", "en", "fr"),
        ("Pay $25", "Maksa 25 euroa", "en", "fi"),
        ("Pay $25", "25ユーロを支払う", "en", "ja"),
        ("Pay $25", "支付25欧元", "en", "zh"),
        ("Pay $25", "ادفع 25 يورو", "en", "ar"),
        ("Pay $25", "Paga 25 euros", "en", "es-MX"),
        ("Pay $25", "Pay £25", "en-US", "en-GB"),
        ("Pay $25", "Zahle 25 CAD", "en", "de"),
        ("Pay $25", "Paga 25 pesos", "en", "es-MX"),
        ("Pay €25", "Zahle 25 $", "en", "de"),
        ("Pay 25 USD", "Zahle 25 EUR", "en", "de"),
        ("Pay £25", "Zahle 25 €", "en-GB", "de"),
        ("Pay $25", "Zahle 25", "en", "de"),  # dropped
        ("Pay 25 euros", "Payez 25 dollars", "en", "fr"),
    )
    for args in blocking:
        assert worst(*args) == "major", (args, structural.findings(*args))
        with patch.object(qe_jev, "_post", return_value=(200, answer())):
            card = qe_jev.score_segment(*args)
        assert card["structural_blocked"], (args, card)
        assert qe_autofix.gate_decision(card, 0) == "failed", (args, card)

    clean = (
        # A date reordered for the target locale is correct.
        ("Due 3/14/2026", "Fällig 14.03.2026", "en-US", "de"),
        ("Due 3/14/2026", "Échéance 14/03/2026", "en-US", "fr"),
        ("Due 3/14/2026", "Erääntyy 14.3.2026", "en-US", "fi"),
        ("Due 3/14/2026", "期限 2026/03/14", "en-US", "ja"),
        ("Due 3/14/2026", "到期 2026/03/14", "en-US", "zh"),
        ("Due 3/14/2026", "تاريخ الاستحقاق 14/03/2026", "en-US", "ar"),
        ("Due 3/14/2026", "Vence 14/03/2026", "en-US", "es-MX"),
        ("Due 3/14/2026", "Due 14/03/2026", "en-US", "en-GB"),
        ("Due 3/14/26", "Fällig 14.03.26", "en-US", "de"),
        ("Due 3/4/2026", "Fällig 04.03.2026", "en-US", "de"),
        ("Due 03/04/2026", "Fällig 03.04.2026", "en-GB", "de"),
        ("Due 03/04/2026", "Due 4/3/2026", "en-GB", "en-US"),
        ("Due 2026-03-14", "Fällig 14.03.2026", "en", "de"),
        ("Due 2026-03-14", "期限 2026/03/14", "en", "ja"),
        ("Due 2026-03-14", "Échéance 2026-03-14", "en", "fr"),
        ("Due 2026-03-14", "Erääntyy 14. 3. 2026", "en", "fi"),
        ("Meet on 3/14", "Treffen am 14.03.", "en-US", "de"),
        ("Meet on 3/14", "Réunion le 14/03", "en-US", "fr"),
        ("Meet on 3/14", "Tapaaminen 14.3.", "en-US", "fi"),
        # Minus sign variants and accounting negatives.
        ("Balance -5", "Saldo −5", "en", "fi"),
        ("Balance -5", "Saldo -5", "en", "de"),
        ("Balance −5", "Solde -5", "en", "fr"),
        ("Balance –5", "Saldo −5", "en", "es-MX"),
        ("Balance -5", "‏؜-5 الرصيد", "en", "ar"),
        ("Balance -$25.00", "Saldo ($25.00)", "en", "es-MX"),
        ("Balance ($25.00)", "Saldo -25,00 $", "en", "de"),
        ("Balance (25.00 USD)", "Solde -25,00 USD", "en", "fr"),
        ("Balance -5", "Saldo minus 5", "en", "de"),
        ("Balance -5", "Solde moins 5", "en", "fr"),
        ("Balance -5", "残高 マイナス5", "en", "ja"),
        ("Balance -5", "余额 负5", "en", "zh"),
        ("Pages 5-10", "Seiten 5-10", "en", "de"),
        ("COVID-19 and Wi-Fi 6", "COVID-19 und Wi-Fi 6", "en", "de"),
        # Currency symbol, code and word equivalents, and per-locale placement.
        ("Pay $25", "Zahle 25 $", "en", "de"),
        ("Pay $25", "Zahle 25 USD", "en", "de"),
        ("Pay $25", "Zahle 25 US$", "en", "de"),
        ("Pay $25", "Zahle US$25", "en", "de"),
        ("Pay $25", "Zahle 25 US-Dollar", "en", "de"),
        ("Pay $25", "Payez 25 $", "en", "fr"),
        ("Pay $25", "Payez 25 $ US", "en", "fr"),
        ("Pay $25", "Payez 25 dollars", "en", "fr"),
        ("Pay $25", "Maksa 25 $", "en", "fi"),
        ("Pay $25", "Maksa 25 dollaria", "en", "fi"),
        ("Pay $25", "25ドルを支払う", "en", "ja"),
        ("Pay $25", "支付25美元", "en", "zh"),
        ("Pay $25", "ادفع 25 دولار", "en", "ar"),
        ("Pay $25", "Paga $25", "en", "es-MX"),
        ("Pay $25", "Paga 25 USD", "en", "es-MX"),
        ("Pay $25", "Paga 25 dólares", "en", "es-MX"),
        ("Pay $25.00", "Zahle 25,00 $", "en", "de"),
        ("Pay $25.00", "Zahle $ 25,00", "en", "de"),
        ("Pay 25 USD", "Pay $25", "en", "en-GB"),
        ("Pay US$25", "Zahle 25 $", "en", "de"),
        ("Pay €25", "Zahle 25 €", "en", "de"),
        ("Pay €25", "Zahle 25 EUR", "en", "de"),
        ("Pay €25", "Payez 25 €", "en", "fr"),
        ("Pay €25", "Maksa 25 euroa", "en", "fi"),
        ("Pay €25", "25ユーロを支払う", "en", "ja"),
        ("Pay €25", "支付25欧元", "en", "zh"),
        ("Pay 25 euros", "Zahle 25 EUR", "en", "de"),
        ("Pay £25", "Zahle 25 £", "en-GB", "de"),
        ("Pay £25", "Zahle 25 GBP", "en-GB", "de"),
        ("Pay ¥100", "100円を支払う", "en", "ja"),
        ("Pay ¥100", "支付100元", "en", "zh"),
        ("Pay $25", "Pay $25", "en-CA", "en-GB"),
        ("Pay CAD 25", "Pay $25", "en", "en-CA"),
        ("Weigh 5 pounds", "Wiegen Sie 5 Pfund", "en", "de"),
    )
    for args in clean:
        hits = structural.findings(*args)
        assert not [f for f in hits if f["severity"] == "major"], (args, hits)
    # A time or date written out is advisory, never a block.
    for args in (
        ("Meet at 5:30 PM", "Rendez-vous cinq heures et demie", "en", "fr"),
        ("Meet at 6:45 PM", "会议在下午6点45分", "en", "zh"),
        ("Due 3/14/2026", "Due 14 March 2026", "en-US", "en-GB"),
        ("Due 3/14/2026", "期限 2026年3月14日", "en-US", "ja"),
    ):
        assert worst(*args) != "major", (args, structural.findings(*args))
    # The advisory forms #335 approved stay advisory next to the new checks.
    for args in (
        ("Pay $25 at 6:45 PM", "Zahle 25 $ um 18.45 Uhr", "en", "de"),
        ("Pay €25 at 6:45 PM", "Maksa 25 € klo 18.45", "en", "fi"),
        ("2 items for $25", "zwei Artikel für 25 $", "en", "de"),
        ("Due 3/14/2026 at 6:45 PM", "Fällig 14.03.2026 um 18.45 Uhr", "en-US", "de"),
    ):
        assert worst(*args) == "minor", (args, structural.findings(*args))
    # Unchanged meaning stays silent: same number, date, minus sign and currency.
    for args in (
        (
            "Pay $25 on 3/14/2026 at 6:45 PM, balance -5",
            "Pay $25 on 3/14/2026 at 6:45 PM, balance -5",
            "en-US",
            "en-GB",
        ),
        ("Version 1.2.3 costs 10.5 USD", "Version 1.2.3 kostet 10,5 USD", "en", "de"),
    ):
        assert worst(*args) is None, (args, structural.findings(*args))
    # Gate fixes: legitimate translations the first build blocked (corpus and probe finds).
    for args in (
        ("Ref. 12-03-45 applies", "Az. 12-03-45 gilt", "en-US", "de"),  # copied unchanged
        ("Ratio 3/4/2026", "Verhältnis 3/4/2026", "en-US", "de"),
        ("Due 3/14/2026", "ครบกำหนด 14/3/2569", "en-US", "th"),  # Buddhist era year
        ("Due 3/14/2026", "Határidő: 2026. 03. 14.", "en-US", "hu"),
        ("Meet at 6:45 PM", "오후 6시 45분에 만나요", "en", "ko"),
        ("Around 16:00 he left", "Alrededor de las cuatro de la tarde se fue", "en", "es"),
        ("Set it to -5", "Réglez-le sur – 5", "en", "fr"),
        ("Below -30C", "au-dessous de-30 °C", "en", "fr"),
        ("Ages 18 -25", "Alter 18 bis 25", "en", "de"),
        ("Items:\n-5 apples\n-3 pears", "Artikel:\n5 Äpfel\n3 Birnen", "en", "de"),
        ("It cost 120,000 yuan ($14,510)", "Il a coûté 120 000 yuans (14 510 dollars)", "en", "fr"),
        ("Rs. 1 lakh ($2,500)", "Rs. 1 lakh (US$2500)", "en", "es"),
        ("Pay 500 SEK", "Betala 500 kronor", "en", "sv"),
        ("Pay 500 DKK", "Betal 500 kroner", "en", "da"),
        ("Pay $25", "Trả 25 đô la", "en-US", "vi"),
        ("Pay $25", "จ่าย 25 ดอลลาร์", "en-US", "th"),
        ("Pay $25", "שלם 25 דולר", "en-US", "he"),
        ("Pay $25", "25 डॉलर का भुगतान करें", "en-US", "hi"),
        ("Pay $25", "Πληρώστε 25 δολάρια", "en-US", "el"),
        ("Pay €25", "שלם 25 יורו", "en", "he"),
        ("Pay £25", "支付25英镑", "en-GB", "zh"),
        ("Pay ¥500", "支付500日元", "en", "zh"),
        ("Pay ¥1,100", "Paga 1.100 yenes", "en", "es"),
        ("Pay R$25", "Pague 25 reais", "en", "pt-BR"),
        ("We raised $5M", "Nous avons levé 5 M$", "en-US", "fr-CA"),
        ("Over $100,000", "Au-delà de 100 000 $US", "en-US", "fr"),
        ("MYR$6 in Malaysia", "6 dollars en Malaisie", "en", "fr"),
        ("Euro 2012 To-Do List", "Lista para la Eurocopa 2012", "en", "es"),
        ("At COP 16 in Cancun", "En la 16 Conferencia de las Partes", "en", "es"),
        ("TRY 30 DAYS FREE", "ESSAYEZ 30 JOURS GRATUITEMENT", "en", "fr"),
        (
            "Exports trail other regions such as Asia ($883) and Latin America ($1,099).",
            "Les exportations sont loin derrière l'Asie (883 dollars) et l'Amérique latine"
            " (1 099 dollars).",
            "en",
            "fr",
        ),
        ("The RM 60B package", "Le paquet de 60 milliards de ringgits", "en", "fr"),
        ("Worth 30 trillion yen a year", "Vale 30 trillones de yen al año", "en", "es"),
        ("Introducing AED4EU", "Presentamos DESA4EU", "en", "es"),
    ):
        hits = structural.findings(*args)
        assert not [f for f in hits if f["severity"] == "major"], (args, hits)
    for args in (
        ("Meet at 6:45 PM", "오후 7시 45분에 만나요", "en", "ko"),
        ("Due 3/14/2026", "ครบกำหนด 15/3/2569", "en-US", "th"),
        ("Balance: ($25.00)", "Saldo: 25,00 $", "en-US", "es-MX"),
        ("Pay $25", "Trả 25 euro", "en-US", "vi"),
        ("Pay 500 NOK", "Betal 500 SEK", "en", "nb"),
    ):
        assert worst(*args) == "major", (args, structural.findings(*args))


def coordinate_negatives_458():
    """#458: a negative token with no readable value is unverifiable, never a lost minus."""
    from app.services import qe_jev_structural as structural

    def minus_losses(*args):
        return [f for f in structural.findings(*args) if "Minus sign lost" in f["reason"]]

    # The crash: "-33.865 151.209" is read as ONE token (a space group separator plus a
    # decimal point), which has no value under any locale, so the copied minus sign that
    # opens a coordinate pair cannot be judged, and `min()` over its empty value set used
    # to raise and skip the whole card.
    coordinate = ("Location: -33.865 151.209", "Ubicacion: -33.865 151.209", "en", "es")
    assert not minus_losses(*coordinate), structural.findings(*coordinate)
    # The card survives: the judge is called and the segment is not QE skipped.
    with patch.object(qe_jev, "_post", return_value=(200, answer())):
        card = qe_jev.score_segment(*coordinate)
    assert not card["skipped"] and card["score"] == 100, card
    # A minus the target really dropped is still reported, coordinates or not.
    for args in (
        ("-5", "5", "en", "de"),
        ("Balance -33.865", "Saldo 33,865", "en", "de"),
    ):
        assert minus_losses(*args), (args, structural.findings(*args))


def scoring_train_cases():
    from app.services import qe_autofix

    failures = []
    cases = (
        ("untranslated", "Please restart the application.", " PLEASE restart the APPLICATION. "),
        ("tags", '<b>Save</b> your <a href="/x">changes</a>.', "Enregistrez les modifications."),
        ("placeholders", "Hello {name}, %d messages.", "Bonjour, des messages."),
        ("numbers", "Pay 25 euros.", "Payez 26 euros."),
        ("numbers", "Do not restart before 5:30 PM.", "Ne redemarrez pas avant 18h45."),
        ("placeholders", "Hello {{name}}", "Bonjour {name}"),
        ("tags", "⟦r1⟧Save⟦/r1⟧", "Enregistrer"),
        ("placeholders", "{n, plural, one {# item} other {# items}}", "Des articles"),
    )
    for dimension, source, target in cases:
        with patch.object(qe_jev, "_post", return_value=(200, answer())) as post:
            card = qe_jev.score_segment(source, target, "en", "fr")
        findings = card.get("structural_findings", [])
        if not (
            post.call_count == 1
            and qe_autofix.gate_decision(card, 0) == "failed"
            and any(f["dimension"] == dimension and f["severity"] == "major" for f in findings)
        ):
            failures.append(
                f"#200 {dimension}: passing judge must retain a MAJOR structural failure"
            )
    for label, dimension in (
        ("minor", {"type": "score", "score": 1, "confidence": 0.9}),
        ("unsure", {"type": "score", "score": 0, "confidence": 0.2}),
    ):
        data = answer()
        data["answers"]["accuracy"] = dimension
        data["answers"]["meaning_changed"]["noul"] = 0.95
        with patch.object(qe_jev, "_post", return_value=(200, data)):
            card = score()
        if card["skipped"] or card["categories"]["accuracy"] != 2:
            failures.append(
                f"#202 {label} accuracy: meaning promotion must precede low-confidence skip"
            )
    for failure in failures:
        print("FAIL " + failure)
    assert not failures, failures
    for source, target, terms in (
        ("123.45", "123.45", []),
        ("https://example.test/x", "https://example.test/x", []),
        ("AB-123", "AB-123", []),
        ("Acme Cloud", "Acme Cloud", [{"source_term": "Acme Cloud", "dnt": True}]),
        ("<b>Hello {name}</b> %s", "<b>Bonjour {name}</b> %1$s", []),
        ("⟦r1⟧Save⟦/r1⟧", "⟦r1⟧Enregistrer⟦/r1⟧", []),
        ("Hello {{name}}", "Bonjour {{name}}", []),
        (
            "{n, plural, one {# item} other {# items}}",
            "{n, plural, one {# article} other {# articles}}",
            [],
        ),
        ("Restart at 5:30 PM", "Redemarrez a 17h30", []),
        ("Pay 1,234.56", "Payez 1 234,56", []),
    ):
        with patch.object(qe_jev, "_post", return_value=(200, answer())):
            card = qe_jev.score_segment(source, target, "en", "fr", glossary_terms=terms)
        assert not card.get("structural_blocked"), (source, card)
        assert qe_autofix.gate_decision(card, 70) == "passed"
    data = answer()
    data["answers"]["meaning_changed"]["noul"] = 0.95
    data["answers"]["fluency"] = {"type": "score", "score": 1, "confidence": 0.2}
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        card = score()
    assert not card["skipped"] and card["categories"]["accuracy"] == 2
    assert card["unsure"] == ["fluency"] and card["meaning_promoted"]


def test_no_veto():
    from app.cli.__main__ import main as cli

    cases = []
    live = answer(accuracy=2)
    for dimension, probs in {
        "accuracy": [0, 0, 1, 0],
        "fluency": [0.8, 0.19, 0, 0],
        "terminology": [0.44, 0.56, 0, 0],
        "style": [0.96, 0.04, 0, 0],
    }.items():
        live["answers"][dimension]["probabilities"] = probs
    cases.append((live, 60, ["terminology"]))  # 15: live confident major
    cases.append((answer(), 100, []))  # 16: all confident none
    split = answer()
    split["answers"]["accuracy"]["probabilities"] = [0.5, 0.5, 0, 0]
    cases.append((copy.deepcopy(split), None, ["accuracy"]))  # 17: only unsure
    two = answer(style=1)
    for dimension in ("accuracy", "terminology"):
        two["answers"][dimension]["probabilities"] = [0.44, 0.56, 0, 0]
    cases.append((two, 95, ["accuracy", "terminology"]))  # 18: minor plus two unsure
    split["answers"]["meaning_changed"]["noul"] = 0.95
    cases.append((split, 60, []))  # 19: meaning promotes before low confidence
    promoted = copy.deepcopy(two)
    promoted["answers"]["meaning_changed"]["noul"] = 0.95
    cases.append((promoted, 55, ["terminology"]))  # 20: promote unsure accuracy
    fallback = answer(style=1)
    fallback["answers"]["accuracy"] = {"type": "score", "score": 3, "confidence": 0.4}
    cases.append((fallback, 95, ["accuracy"]))  # 21: score-only uncertainty
    no_promotion = copy.deepcopy(fallback)
    no_promotion["answers"]["meaning_changed"]["noul"] = 0.95
    cases.append((no_promotion, 55, []))  # 22: unsure critical becomes confident major
    failures = []
    for number, (data, expected, unsure) in enumerate(cases, 15):
        try:
            with patch.object(qe_jev, "_post", return_value=(200, data)):
                card = score()
            assert card["score"] == expected, card
            if expected is None:
                assert card["reason"] == "low_confidence", card
                assert all(c in json.loads(card["error"]) for c in unsure)
            else:
                assert not card["skipped"] and card["unsure"] == unsure, card
                assert all(card["categories"][c] == 0 for c in unsure), card
                for c in unsure:
                    detail = (
                        "p_none=0.44,p_err=0.56"
                        if "probabilities" in data["answers"][c]
                        else "api_conf=0.40"
                    )
                    assert f"{c}=unsure({detail})" in card["rationale"], card
                if number == 15:
                    assert card["categories"]["accuracy"] == 2, card
            print(f"[{number}] no veto OK")
        except (AssertionError, KeyError) as exc:
            failures.append(f"[{number}] {exc}")
    # 22: conditional ASCII column and JSON pass-through; legacy cards omit unsure.
    pairs = Path(_TMP) / "no-veto.jsonl"
    pairs.write_text(
        json.dumps({"source": "a", "target": "b", "source_locale": "en", "target_locale": "de"}),
        encoding="utf-8",
    )
    output = Path(_TMP) / "no-veto-report.json"
    for dims in ([], ["terminology"], ["terminology", "style"]):
        stream = io.StringIO()
        card = dict(qe._skipped("fixture"), skipped=False, score=60)
        with (
            patch.object(qe, "score_segment", return_value=card),
            patch.object(qe_jev, "score_segment", return_value=dict(card, unsure=dims)),
            contextlib.redirect_stdout(stream),
        ):
            assert (
                cli(["lqa", "qe-compare", "--yes", "--pairs", str(pairs), "--json", str(output)])
                == 0
            )
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["rows"][0]["jev"]["unsure"] == dims
        table = stream.getvalue().split("summary:")[0]
        if ("unsure" in table) != bool(dims) or (
            dims and ",".join(c[0].upper() for c in dims) not in table
        ):
            failures.append("[22] conditional unsure column missing or incorrect")
        assert table.isascii()
    # 23: shipped confidence/card/report/loop documentation.
    architecture = (Path(_ROOT) / "docs/architecture/quality-lqa-evaluations.md").read_text(
        encoding="utf-8"
    )
    section = architecture.split("### Jev QE backend (POC)", 1)[1].split("## 4.", 1)[0]
    for token in (
        "unsure dimensions contribute count 0",
        "terminology=unsure(p_none=0.44,p_err=0.56)",
        "absent `unsure` as an empty list",
        "every count < 2",
        "`T` or `T,S`",
        "`undecided=True`",
        "`passed=None`",
        "`qe_jev_loop.refine` requires an empty `unsure` list",
        "fully confident round wins a score tie",
        "`source=cli`",
        "`prompt-tap show --provider jev`",
    ):
        if token not in " ".join(section.split()):
            failures.append(f"[23] missing documentation: {token}")
    assert "\u2014" not in section
    assert not failures, failures


def test_observers_and_cap():
    from app.ports import telemetry

    with (
        patch.object(qe_jev, "_post", return_value=(200, answer())) as post,
        patch.object(spend, "would_block", return_value=(True, 1.0, 1.0)),
    ):
        assert score()["reason"] == "provider_error"
        post.assert_not_called()
    with patch.object(qe_jev, "_post", return_value=(401, answer())):
        card = score()
        assert card["reason"] == "provider_error" and "server rejected the key" in card["error"]
        assert card["tokens_in"] == 1000 and card["tokens_out"] == 20
        assert card["cost_usd"] > 0
    with (
        patch.object(qe_jev, "_post", side_effect=[(429, answer()), (200, answer())]),
        patch.object(qe_jev.time, "sleep"),
        patch.object(telemetry, "enabled", return_value=True),
        patch.object(telemetry, "emit") as emit,
    ):
        meta = {"workflow_id": 42}
        card = qe_jev.score_segment(
            "source", "target", "en", "de", tap_meta=meta, images=[b"ignored"]
        )
        assert not card["skipped"] and emit.call_count == 2
        records = [c.args[0] for c in emit.call_args_list]
        assert abs(sum(r["cost_usd"] for r in records) - card["cost_usd"]) < 1e-12
        rec = records[-1]
        assert rec["provider"] == "jev" and rec["task"] == "qe" and rec["meta"] == meta
        assert json.loads(rec["response"]["text"])["answers"] == answer()["answers"]
        assert rec["request"]["payload"]["state"]["translation"] == "target"
        assert "ignored" not in json.dumps(rec) and config.TYPESAFE_API_KEY not in json.dumps(rec)
    with (
        patch.object(qe_jev, "_post", return_value=(200, answer())),
        patch.object(telemetry, "enabled", side_effect=RuntimeError("tap broken")),
    ):
        assert qe_jev.score_segment("s", "t", "en", "de", tap_meta={})["score"] == 100


def test_lab():
    from app.services import eval_metrics

    for has_ref in (True, False):
        ids = {s.id for s in eval_metrics.applicable_scorers(has_ref)}
        assert "judge_qe" in ids and "judge_qe_jev" in ids
    with patch.object(config, "TYPESAFE_API_KEY", ""):
        result = eval_metrics.score("judge_qe_jev", source_text="a", output_text="b")
        assert result.not_applicable and result.value is None
    with patch.object(qe_jev, "_post", return_value=(200, answer())):
        result = eval_metrics.score("judge_qe_jev", source_text="a", output_text="b")
        assert result.ok and result.value == 100
        assert result.detail["judge_provider"] == "jev"
        assert result.detail["judge_model"] == config.TYPESAFE_MODEL


def test_dispatch_and_surfaces():
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app.cli.__main__ import main as cli
    from app.db import engine
    from app.main import app
    from app.models import SegStatus, WorkflowStep
    from app.services import apikeys, orchestrator, security, workflows
    from app.services.providers import register_provider

    # Reuse the gate suite's document/provider fixtures without resetting its environment.
    with patch("tests._bootstrap.init_env", return_value=_TMP):
        from tests.verify_qe_gate import QEProvider, make_wf, new_segmented_doc, segs_for

    class TranslatedQEProvider(QEProvider):
        def complete(self, *args, **kwargs):
            text = super().complete(*args, **kwargs)
            if kwargs.get("task") == "translate":
                for src, tgt in (
                    ("Unsure", "Incertain"),
                    ("Confident", "Confiant"),
                    ("Contact", "Contactez"),
                ):
                    text = text.replace(src, tgt)
            return text

    register_provider("qeprov", TranslatedQEProvider())
    client = TestClient(app)
    response = client.post(
        "/projects",
        data={
            "name": "Jev",
            "source_locale": "en-US",
            "target_locales": "fr-FR",
            "default_provider": "qeprov",
        },
        follow_redirects=False,
    )
    pid = int(response.headers["location"].rsplit("/", 1)[-1])
    from app.services import adaptive

    for levels, unsure_dimension, expected_score in (
        ({"style": 1}, "accuracy", 95),
        ({"accuracy": 2}, "terminology", 60),
    ):
        data = answer(**levels)
        data["answers"][unsure_dimension]["probabilities"] = [0.44, 0.56, 0, 0]
        with Session(engine) as session:
            wid = make_wf(
                session,
                "unsure gate",
                qe_gate=True,
                qe_provider="jev",
                qe_threshold=70,
                adaptive_feedback=True,
                adaptive_feedback_window=5,
            )
        did = new_segmented_doc(client, pid, ["Unsure first.", "Confident second."])
        QEProvider.reset()
        with (
            patch.object(qe_jev, "_post", side_effect=[(200, data), (200, answer())]),
            Session(engine) as session,
        ):
            events = list(orchestrator.run_workflow_stream_segmented(session, wid, did, "fr-FR"))
        with Session(engine) as session:
            segment = segs_for(session, did)[0]
            flag = next(f for f in segment.qa_flags if f["type"] == "qe")
            assert flag["score"] == expected_score
            if expected_score == 95:
                assert segment.status == SegStatus.MT
                assert flag["passed"] is None and flag["undecided"] is True, flag
                assert flag["unsure"] == [unsure_dimension] and not flag["skipped"]
            else:
                assert segment.status == SegStatus.NEEDS_REVIEW and flag["passed"] is False
        expected = (
            {"scored": 1, "passed": 1, "failed": 0, "skipped": 1}
            if expected_score == 95
            else {
                "scored": 2,
                "passed": 1,
                "failed": 1,
                "skipped": 0,
            }
        )
        for event in events:
            if event.get("type") in ("step", "done") and "qe" in event:
                assert event["qe"] == expected, event
        translates = [c for c in QEProvider.calls if c["task"] == "translate"]
        assert len(translates) == 2
        assert adaptive._FEEDBACK_HEADER not in translates[1]["prompt"]
    for provider in ("jev", ""):
        with Session(engine) as session:
            wid = make_wf(
                session, "dispatch " + provider, qe_gate=True, qe_provider=provider, redact_pii=True
            )
        did = new_segmented_doc(client, pid, ["Contact SSN 123-45-6789 now."])
        QEProvider.reset()
        with patch.object(qe_jev, "_post", return_value=(200, answer(accuracy=2))) as post:
            with Session(engine) as session:
                events = list(
                    orchestrator.run_workflow_stream_segmented(session, wid, did, "fr-FR")
                )
            assert any(e.get("type") == "done" for e in events), events
            calls = [c for c in QEProvider.calls if c["task"] == "qe"]
            if provider == "jev":
                assert post.call_count == 1 and not calls
                state = post.call_args.args[0]["state"]
                assert "123-45-6789" not in json.dumps(state) and "PII_" in json.dumps(state)
                with Session(engine) as session:
                    seg = segs_for(session, did)[0]
                    assert seg.status == SegStatus.NEEDS_REVIEW
                    flag = next(f for f in seg.qa_flags if f["type"] == "qe")
                    assert flag["score"] == 60 and flag["rationale"].startswith("jev:")
            else:
                post.assert_not_called()
                assert len(calls) == 1
    with Session(engine) as session:
        wid = workflows.create_workflow(session, "four surfaces").id
    fields = {
        "name": "Jev",
        "step_type": "llm_agent",
        "provider": "mock",
        "prompt_text": "Translate: {text}",
        "qe_gate": True,
        "qe_provider": "jev",
    }
    response = client.post(
        f"/workflows/{wid}/steps", data={**fields, "qe_gate": "1"}, follow_redirects=False
    )
    assert response.status_code in (200, 303), response.text
    user = security.create_user("jev@test.invalid", "Jev tester", "admin", "jev-password-1234")
    _, key = apikeys.create_key(user.id, "jev", scopes="api docs")
    headers = {"X-Loc-Token": key}
    response = client.post(f"/api/workflows/{wid}/steps", json=fields, headers=headers)
    assert response.status_code == 200, response.text
    with contextlib.redirect_stdout(io.StringIO()):
        assert (
            cli(
                [
                    "workflow",
                    "add-step",
                    str(wid),
                    "--provider",
                    "mock",
                    "--prompt",
                    "Translate: {text}",
                    "--qe-gate",
                    "--qe-provider",
                    "jev",
                ]
            )
            == 0
        )
    mcp_dir = Path(_ROOT) / "integrations" / "mcp-server"
    sys.path.insert(0, str(mcp_dir))
    spec = importlib.util.spec_from_file_location("jev_mcp_server", mcp_dir / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    def local_client():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
        )

    with patch.object(server, "_client", local_client), patch.object(server, "TOKEN", key):
        result = json.loads(
            asyncio.run(server.edit_workflow(workflow_id=wid, action="add_step", fields=fields))
        )
        assert "error" not in result, result
    with Session(engine) as session:
        steps = session.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == wid)).all()
        assert len(steps) == 4 and all(s.qe_provider == "jev" and s.qe_gate for s in steps)


def test_cli():
    from app.cli.__main__ import main as cli

    test_cli_tap_and_unsure()

    fixture = Path(_ROOT) / "tests/fixtures/qe_pairs_sample.jsonl"
    pairs = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
    assert len(pairs) == 12 and sum(p["gold_pass"] for p in pairs) == 8
    for index in (8, 10):
        assert pairs[index]["gold_pass"] is True
        assert pairs[index]["gold_note"] == "minor by design: passes at threshold 70"
    assert {p["target_locale"] for p in pairs} == {"de", "es"}
    output = Path(_TMP) / "comparison.json"
    cards = [
        dict(
            qe._skipped("fixture"),
            skipped=False,
            score=100 if p["gold_pass"] else 60,
            cost_usd=0.001,
        )
        for p in pairs
    ]
    stream = io.StringIO()
    with (
        patch.object(qe, "score_segment", side_effect=copy.deepcopy(cards)),
        patch.object(qe_jev, "score_segment", side_effect=copy.deepcopy(cards)),
        contextlib.redirect_stdout(stream),
    ):
        assert (
            cli(
                [
                    "lqa",
                    "qe-compare",
                    "--yes",
                    "--pairs",
                    str(fixture),
                    "--llm-provider",
                    "mock",
                    "--json",
                    str(output),
                ]
            )
            == 0
        )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert len(report["rows"]) == 12
    assert report["summary"]["gate_agreement_rate"] == 1
    assert report["summary"]["mean_absolute_score_difference"] == 0
    assert report["summary"]["llm"]["gold_accuracy"] == 1
    assert report["summary"]["jev"]["gold_accuracy"] == 1
    assert abs(report["summary"]["llm"]["total_cost_usd"] - 0.012) < 1e-12
    assert report["summary"]["llm"]["scored"] == 12
    for label in (
        "llm score",
        "jev score",
        "llm pass",
        "jev pass",
        "agree",
        "llm ms",
        "jev ms",
        "summary",
        "p50",
        "p95",
        "total_cost",
    ):
        assert label in stream.getvalue(), label
    small = Path(_TMP) / "small.jsonl"
    small.write_text("\n".join(json.dumps(p) for p in pairs[:3]), encoding="utf-8")
    good = dict(cards[0], score=100)
    bad = dict(cards[0], score=60)
    skip = qe._skipped("low_confidence")
    with (
        patch.object(qe, "score_segment", side_effect=[good, bad, skip]),
        patch.object(qe_jev, "score_segment", side_effect=[good, skip, bad]),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert (
            cli(
                [
                    "lqa",
                    "qe-compare",
                    "--yes",
                    "--pairs",
                    str(small),
                    "--threshold",
                    "100",
                    "--json",
                    str(output),
                ]
            )
            == 0
        )
    report = json.loads(output.read_text(encoding="utf-8"))
    summary = report["summary"]
    assert summary["both_scored"] == 1 and summary["gate_agreement_rate"] == 1
    assert summary["llm"]["scored"] == summary["jev"]["scored"] == 2
    assert summary["llm"]["gold_accuracy"] == summary["jev"]["gold_accuracy"] == 0.5
    assert report["rows"][1]["agree"] is None
    with (
        patch.object(qe, "score_segment", return_value=skip),
        patch.object(qe_jev, "score_segment", return_value=skip),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert (
            cli(["lqa", "qe-compare", "--yes", "--pairs", str(small), "--json", str(output)]) == 0
        )
    summary = json.loads(output.read_text(encoding="utf-8"))["summary"]
    assert summary["gate_agreement_rate"] is None
    assert summary["mean_absolute_score_difference"] is None
    assert summary["jev"]["gold_accuracy"] is None


def test_cli_tap_and_unsure():
    from app.cli.__main__ import main as cli
    from app.ports import telemetry
    from app.services import prompt_tap

    with contextlib.redirect_stdout(io.StringIO()):
        assert cli(["prompt-tap", "on"]) == 0

    pairs = Path(_TMP) / "tap-pairs.jsonl"
    output = Path(_TMP) / "tap-report.json"
    pairs.write_text(
        json.dumps(
            {
                "id": "pair-a",
                "source": "Source",
                "target": "Target",
                "source_locale": "en",
                "target_locale": "de",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    for levels, expected in (({"style": 1}, None), ({"accuracy": 2}, False)):
        data = answer(**levels)
        data["answers"]["terminology"]["probabilities"] = [0.44, 0.56, 0, 0]
        with (
            patch.object(qe_jev, "_post", return_value=(200, data)),
            patch.object(telemetry, "enabled", return_value=True),
            patch.object(telemetry, "emit", side_effect=prompt_tap.capture) as emit,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            assert (
                cli(
                    [
                        "lqa",
                        "qe-compare",
                        "--yes",
                        "--pairs",
                        str(pairs),
                        "--llm-provider",
                        "mock",
                        "--json",
                        str(output),
                    ]
                )
                == 0
            )
        records = [call.args[0] for call in emit.call_args_list]
        assert len(records) == 2 and sum(r["provider"] == "jev" for r in records) == 1
        for record in records:
            meta = record["meta"]
            assert meta["source"] == "cli" and meta["tool"] == "qe-compare"
            assert meta["pair"] == "pair-a" and meta["round"] is None
            assert meta["segment_index"] == 1
            assert all(meta[k] is None for k in ("workflow_id", "workflow_run_id", "document_id"))
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["rows"][0]["jev"]["pass"] is expected
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            assert cli(["--json", "prompt-tap", "show", "--provider", "jev"]) == 0
        shown = json.loads(capture.getvalue())["records"]
        assert any(r.get("meta", {}).get("tool") == "qe-compare" for r in shown)
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli(["prompt-tap", "off"]) == 0


def test_fix_round_1():
    failures = []
    for number, test in enumerate(
        (
            fix_1_probabilities,
            fix_2_usage,
            fix_3_floor,
            fix_5_switch,
            fix_6_transport_tap,
            fix_7_segmented_docs,
            fix_8_pairs,
            fix_9_live_shape,
            fix_10_error_confidence,
            fix_11_floor_bounds,
            fix_12_none_precedence,
            fix_13_error_mass_and_detail,
        ),
        1,
    ):
        try:
            test()
            print(f"[{number}] fix round 1 OK")
        except Exception as exc:
            failures.append(f"[{number}] {type(exc).__name__}: {exc}")
            print(f"[{number}] fix round 1 FAILED: {exc}")
    assert not failures, failures


def fix_1_probabilities():
    for probs, reason in (
        ([0.25] * 4, None),
        ([0.30, 0.29, 0.21, 0.20], None),
        ([0.9, 0.04, 0.03, 0.03], None),
    ):
        data = answer()
        data["answers"]["accuracy"]["probabilities"] = probs
        with patch.object(qe_jev, "_post", return_value=(200, data)):
            assert score()["reason"] == reason
    data["answers"]["accuracy"]["probabilities"] = [0.25] * 4
    assert qe_jev._level(data["answers"]["accuracy"], 0.6)[0] == 3


def fix_2_usage():
    for value, expected in (("123", 123), (123.0, 123), (-1, 0), (-0.5, 0), (None, 0)):
        data = answer()
        data["usage"]["input_tokens"] = value
        with (
            patch.object(qe_jev, "_post", return_value=(200, data)),
            patch.object(spend, "record_external_usd") as record,
            patch.object(qe_jev.log, "warning") as warning,
        ):
            card = score()
            assert not card["skipped"] and card["tokens_in"] == expected, card
            assert abs(card["cost_usd"] - expected * 0.042e-6) < 1e-12
            if expected:
                record.assert_called_once_with(card["cost_usd"])
                warning.assert_not_called()
            else:
                record.assert_not_called()
                warning.assert_called_once()
                assert "usage unreadable" in card["rationale"] + (card["error"] or "")

    data = answer()
    data["usage"]["input_tokens"] = None
    for responses in ([(429, data), (200, data)], [(200, {**data, "answers": {}})]):
        with (
            patch.object(qe_jev, "_post", side_effect=responses),
            patch.object(qe_jev.time, "sleep"),
            patch.object(spend, "record_external_usd") as record,
            patch.object(qe_jev.log, "warning") as warning,
        ):
            card = score()
            warning.assert_called_once()
            record.assert_not_called()
            assert card["tokens_in"] == 0 and "usage unreadable" in card["error"]


def store_legacy_floor(value):
    """Seed a historical value directly; current write doors must refuse it."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import Setting

    with Session(engine) as session:
        session.merge(Setting(key="qe_jev_confidence_floor", value=value))
        session.commit()
    settings.invalidate_cache()
    settings.apply_to_config()


def fix_3_floor():
    try:
        for value in ("garbage", "-0.1", "1.1", "nan"):
            store_legacy_floor(value)
            with (
                patch.object(qe_jev.log, "warning") as warning,
                patch.object(qe_jev, "_post") as post,
            ):

                def response(body):
                    warning.assert_called_once()  # fallback happens before HTTP
                    return 200, answer()

                post.side_effect = response
                assert score()["score"] == 100
                post.assert_called_once()
                warning.assert_called_once()
            try:
                settings.validate_value("qe_jev_confidence_floor", value)
            except settings.InvalidSettingValue:
                pass
            else:
                raise AssertionError("invalid floor accepted")
        for value in ("0.5", "0.6", "0.999"):
            settings.validate_value("qe_jev_confidence_floor", value)
    finally:
        settings.set_many({"qe_jev_confidence_floor": "0.6"})


def fix_5_switch():
    try:
        for value in ("0 ", "off", " FALSE ", "no"):
            settings.set_many({"qe_jev_enabled": value})
            with patch.object(qe_jev, "_post") as post:
                assert score()["reason"] == "jev_disabled"
                post.assert_not_called()
    finally:
        settings.set_many({"qe_jev_enabled": "1"})


def fix_6_transport_tap():
    from app.ports import telemetry

    with (
        patch.object(qe_jev, "_post", side_effect=httpx.ConnectTimeout("offline")),
        patch.object(qe_jev.time, "sleep"),
        patch.object(telemetry, "enabled", return_value=True),
        patch.object(telemetry, "emit") as emit,
    ):
        card = qe_jev.score_segment("s", "t", "en", "de", tap_meta={})
        assert card["reason"] == "provider_error" and emit.call_count == 2
        for call in emit.call_args_list:
            rec = call.args[0]
            assert rec["outcome"] == "provider-error"
            assert rec["error"].startswith("timed out after 10 s; "), rec


def fix_7_segmented_docs():
    sentence = (
        "The QE gate (both backends) runs only in the SEGMENTED workflow path; "
        "a full-text step with qe_gate on makes no judge call."
    )
    for path in ("docs/architecture/quality-lqa-evaluations.md", "app/templates/help.html"):
        assert sentence in " ".join((Path(_ROOT) / path).read_text(encoding="utf-8").split()), path
    architecture = (Path(_ROOT) / "docs/architecture/quality-lqa-evaluations.md").read_text(
        encoding="utf-8"
    )
    assert "winning level probability below it skips" not in " ".join(architecture.split())
    for token in ("p_none", "p_error", "p_err", "api_conf"):
        assert token in architecture, token


def fix_8_pairs():
    from app.cli.__main__ import main as cli

    missing = Path(_TMP) / "missing-pairs.jsonl"
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        assert cli(["lqa", "qe-compare", "--yes", "--pairs", str(missing)]) == 2
    assert len((out.getvalue() + err.getvalue()).splitlines()) == 1
    for error in (
        FileNotFoundError("missing"),
        PermissionError("denied"),
        UnicodeError("unreadable"),
    ):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(Path, "read_text", side_effect=error),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            assert cli(["lqa", "qe-compare", "--yes", "--pairs", str(missing)]) == 2
        assert len((out.getvalue() + err.getvalue()).splitlines()) == 1
    malformed = Path(_TMP) / "malformed-pairs.jsonl"
    malformed.write_text("not json\n", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli(["lqa", "qe-compare", "--yes", "--pairs", str(malformed)]) == 0
    assert "error: row 1:" in out.getvalue()


def fix_9_live_shape():
    # Live jev-1.13.0 Score answer: probabilities keyed by level INDEX as a string,
    # plus a "legend" key the client must ignore. `score` (the ladder position) is
    # present but unused when `probabilities` decides the winner.
    live_accuracy = {
        "type": "score",
        "score": 2.01,
        "confidence": 0.99,
        "probabilities": {"0": 0.0, "1": 0.0, "2": 0.99, "3": 0.01},
        "legend": {"0": {}, "1": {}, "2": {}, "3": {}},
    }
    data = answer()
    data["answers"]["accuracy"] = live_accuracy
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        card = score()
        assert not card["skipped"], card
        assert card["categories"]["accuracy"] == 2, card
        expected_score, _ = qe._score_from_categories({"accuracy": 2})
        assert card["score"] == expected_score, card

    # Integer keys 0..3 (not strings) must also be accepted.
    data = answer()
    data["answers"]["accuracy"]["probabilities"] = {0: 0.0, 1: 0.0, 2: 0.99, 3: 0.01}
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        assert not score()["skipped"]

    # The old offline shape (dict keyed by level name) must still work.
    data = answer()
    data["answers"]["accuracy"]["probabilities"] = {
        "none": 0.0,
        "minor": 0.0,
        "major": 0.99,
        "critical": 0.01,
    }
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        assert not score()["skipped"]

    # Only three index keys is still an incomplete/junk shape.
    data = answer()
    data["answers"]["accuracy"]["probabilities"] = {"0": 0.5, "1": 0.3, "2": 0.2}
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        assert score()["reason"] == "junk_json"


def fix_10_error_confidence():
    for probs, level, confidence in (
        ({"0": 0.19, "1": 0.30, "2": 0.31, "3": 0.20}, 2, 0.81),
        ({"0": 0.55, "1": 0.45, "2": 0, "3": 0}, None, 0.55),
        ({"0": 0.98, "1": 0.01, "2": 0.01, "3": 0}, 0, 0.98),
        ({"0": 0.25, "1": 0.25, "2": 0.25, "3": 0.25}, 3, 0.75),
    ):
        data = answer()
        dimension = data["answers"]["terminology"]
        dimension.update(probabilities=probs, confidence=0.19)
        with patch.object(qe_jev, "_post", return_value=(200, data)):
            card = score()
        if level is None:
            assert card["reason"] == "low_confidence", card
            detail = json.loads(card["error"])["terminology"]
            assert detail["p_none"] == 0.55
            assert abs(detail["p_error"] - 0.45) < 1e-12
            assert card["cost_usd"] > 0 and card["tokens_in"] == 1000
        else:
            assert not card["skipped"], card
            assert card["categories"]["terminology"] == level, card
            assert qe_jev._level(dimension, 0.6)[1] == confidence
            assert f"terminology={qe_jev.LEVELS[level]}(" in card["rationale"]
            assert f"p_none={probs['0']:.2f}" in card["rationale"]
            assert f"p_err={1 - probs['0']:.2f}" in card["rationale"]
            assert "api_conf=0.19" in card["rationale"]


def fix_11_floor_bounds():
    try:
        for value in ("0.3", "0", "-0.1"):
            store_legacy_floor(value)
            data = answer()
            data["answers"]["terminology"]["probabilities"] = [0.31, 0, 0, 0.69]
            with (
                patch.object(qe_jev.log, "warning") as warning,
                patch.object(qe_jev, "_post") as post,
            ):

                def response(body, data=data):
                    warning.assert_called_once()
                    return 200, data

                post.side_effect = response
                card = score()
                post.assert_called_once()
                warning.assert_called_once()
                assert not card["skipped"] and card["categories"]["terminology"] == 3, card
            # Distinguish clamping to 0.5 from falling back to 0.6.
            data["answers"]["terminology"]["probabilities"] = [0.45, 0, 0, 0.55]
            with patch.object(qe_jev, "_post", return_value=(200, data)):
                assert score()["categories"]["terminology"] == 3
            try:
                settings.validate_value("qe_jev_confidence_floor", value)
            except settings.InvalidSettingValue:
                pass
            else:
                raise AssertionError(f"below-minimum floor accepted: {value}")
        for value in ("0.5", "0.999"):
            settings.validate_value("qe_jev_confidence_floor", value)
        for value in ("1", "1.0"):
            store_legacy_floor(value)
            assert qe_jev.resolve_floors(qe_jev.resolve_target("jev"))[0] == 1.0
            try:
                settings.validate_value("qe_jev_confidence_floor", value)
            except settings.InvalidSettingValue:
                pass
            else:
                raise AssertionError("new floor 1.0 accepted")
        for path in ("app/services/settings.py", "docs/architecture/quality-lqa-evaluations.md"):
            assert "at least 0.5 and less than 1.0, default 0.6" in (Path(_ROOT) / path).read_text(
                encoding="utf-8"
            )
    finally:
        settings.set_many({"qe_jev_confidence_floor": "0.6"})


def fix_12_none_precedence():
    try:
        for floor, probs, expected in (
            ("0.5", [0.5, 0.5, 0, 0], 0),
            ("0.6", [0.41, 0.59, 0, 0], None),
        ):
            settings.set_many({"qe_jev_confidence_floor": floor})
            data = answer()
            data["answers"]["terminology"]["probabilities"] = probs
            with patch.object(qe_jev, "_post", return_value=(200, data)) as post:
                card = score()
                post.assert_called_once()
            if expected is None:
                assert card["reason"] == "low_confidence", card
            else:
                assert not card["skipped"] and card["categories"]["terminology"] == expected, card
    finally:
        settings.set_many({"qe_jev_confidence_floor": "0.6"})


def fix_13_error_mass_and_detail():
    data = answer()
    dimension = data["answers"]["terminology"]
    # Accepted normalization tolerance must not inflate the actual error mass.
    dimension.update(probabilities=[0.4, 0.59, 0, 0], confidence=0.9)
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        assert score()["reason"] == "low_confidence"
    dimension.update(
        probabilities=[0.5500000000000001, 0.44999999999999996, 0, 0],
        confidence=0.123456789,
    )
    with patch.object(qe_jev, "_post", return_value=(200, data)):
        card = score()
    assert card["reason"] == "low_confidence"
    assert json.loads(card["error"])["terminology"] == {
        "p_none": 0.55,
        "p_error": 0.45,
        "api_conf": 0.1235,
    }
    del dimension["probabilities"]
    dimension["score"] = 0
    assert qe_jev._level(dimension, 0.6)[2] == {"api_conf": 0.1235}


def fix_round_one():
    from app.services import qe_autofix
    from app.services import qe_jev_structural as structural
    from app.services.eval_metrics_judge_qe_qe_score_segmen import _judge_qe_jev

    negatives = [
        ("Start", "Starten", "en", "de"),
        ("Export", "Exportieren", "en", "de"),
        ("Test", "Testen", "en", "de"),
        ("On", "Mon nom", "en", "fr"),
        ("a", "la maison", "en", "fr"),
        ("Hello world", "Hello world", "en-US", "en-GB"),
        ('<abbr title="Application">App</abbr>', '<abbr title="Anwendung">App</abbr>', "en", "de"),
        ('<img alt="Cat"/>', '<img alt="Chat" />', "en", "fr"),
        ('<a href="/help">Help</a>', '<a href="/help">Aide</a>', "en", "fr"),
        ("One<br/>two", "Un<br />deux", "en", "fr"),
    ]
    # #719 (#719 gate, merged 9ed5b652b): `href` is a
    # non-translatable attribute for the placeholder gate, so a changed link is a placeholder
    # finding. The maintainer's 2026-10-13 decision: a link whose ONLY change is its language or
    # locale
    # part (`/en/help` -> `/fr/help`) passes; any other change, or a dropped href, is still held.
    for source, target in (
        ('<a href="/en/help">Help</a>', '<a href="/fr/help">Aide</a>'),
        ('<a href="/en-us/help">Help</a>', '<a href="/fr-fr/help">Aide</a>'),
        (
            '<a href="https://en.example.com/x">Help</a>',
            '<a href="https://fr.example.com/x">Aide</a>',
        ),
        (
            '<a href="https://x.com/p?lang=en">Help</a>',
            '<a href="https://x.com/p?lang=fr">Aide</a>',
        ),
    ):
        hits = structural.findings(source, target, "en", "fr")
        assert not [f for f in hits if f["dimension"] == "placeholders"], (target, hits)
    for target in (
        '<a href="/fr/aide">Aide</a>',  # the path changed, not just the language
        '<a href="/de/help">Aide</a>',  # another language than the target's
        '<a href="/help">Aide</a>',  # the language part was dropped
        "<a>Aide</a>",  # the href was dropped
    ):
        hits = structural.findings('<a href="/en/help">Help</a>', target, "en", "fr")
        flagged = [
            f for f in hits if f["dimension"] == "placeholders" and "HTML tag" in f["reason"]
        ]
        assert flagged, (target, hits)
    for source, target in (
        ("5:30 PM", "17 h 30"),
        ("5:30 PM", "17h30"),
        ("5:30 PM", "5:30 p. m."),
        ("5:30 AM", "5:30 a. m."),
        ("5:30 PM", "5:30 p.m."),
        ("5:30 AM", "5:30 a.m."),
        ("5:30 PM", "午後5時30分"),
        ("5:30 PM", "cinq heures et demie"),
    ):
        negatives.append(("Meet at " + source, "Rendez-vous " + target, "en", "fr"))
    # A following word starting with a/p + m is not a meridiem (12:15 a mi casa != 00:15).
    negatives.append(("Arrive at 12:15 at home", "Llega a las 12:15 a mi casa", "en", "es"))
    # German "am" ("on the") after a time is not AM, and a meridiem never applies to
    # a 0 or 13+ hour: correct German must not be hard-blocked or sent to auto-fix.
    negatives += [
        ("Meet at 17:30 at the station", "Treffen um 17:30 am Bahnhof", "en", "de"),
        ("Open until 18:00 on Monday", "Geöffnet bis 18:00 am Montag", "en", "de"),
        ("Doors open at 12:00", "Einlass um 12:00 am Haupteingang", "en", "de"),
    ]
    # An HTML void element is one kind whether written <br>, <br/> or <br />.
    negatives.append(("One<br>two", "Un<br/>deux", "en", "fr"))
    plural = "{count, plural, one {{name} has # file} other {{name} has # files}}"
    for locale, branches in (
        ("ru", "one few many other"),
        ("ja", "other"),
        ("ar", "zero one two few many other"),
    ):
        target = (
            "{count, plural, " + " ".join(b + " {{name} fichier #}" for b in branches.split()) + "}"
        )
        negatives.append((plural, target, "en", locale))
        dropped = target.replace("{name}", "")
        assert any(
            f["dimension"] == "placeholders"
            for f in structural.findings(plural, dropped, "en", locale)
        )
    for args in negatives:
        assert not structural.findings(*args), args
    for word in ("OK", "PDF", "USB", "Wi-Fi", "Status", "Name", "Hotel", "Dashboard", "iPhone"):
        for terms in ((), [{"source_term": word, "dnt": True}]):
            with patch.object(qe_jev, "_post", return_value=(200, answer())):
                card = qe_jev.score_segment(word, word, "en", "fr", glossary_terms=terms)
            assert qe_autofix.gate_decision(card, 70) == "passed", card
            assert card["structural_findings"][0]["severity"] == "minor", card
            assert not qe_autofix.confident_failures(card)
    hits = structural.findings("设置", "设置", "zh", "en")
    assert hits and not any(f["severity"] == "major" for f in hits), hits
    for args in (
        ("Meet at 5:30 PM", "Rendez-vous 18h45", "en", "fr"),
        ("Meet at 5:30 AM", "Rendez-vous 17h30", "en", "fr"),
        ("Please restart now", " please  RESTART now ", "en", "fr"),
        ("Save <b>now</b>", "Sauver maintenant", "en", "fr"),
        ("Save now", "Sauver <b>maintenant</b>", "en", "fr"),
        # Scripts without word spaces: a copied sentence is multi-word text (MAJOR),
        # only a short unpunctuated word stays a single-token advisory.
        ("请重新启动应用程序。", "请重新启动应用程序。", "zh", "en"),
        ("กรุณารีสตาร์ทแอปพลิเคชัน", "กรุณารีสตาร์ทแอปพลิเคชัน", "th", "en"),
    ):
        assert any(f["severity"] == "major" for f in structural.findings(*args)), args
    for reason in ("jev_disabled", "not_configured", "junk_json", "low_confidence"):
        base = qe._skipped(reason)
        hits = structural.findings("Hello {name}", "Bonjour", "en", "fr")
        card = structural.attach(base, hits)
        assert card["score"] is None and card["judge_skipped"]
        assert qe_autofix.gate_decision(card, 0) == "failed"
        flag = qe.qe_flag(None, card["rationale"], 70, False, False, reason=reason)
        assert "judge did not score" in flag["message"] and "100/100" not in flag["message"]
        with patch.object(qe_jev, "score_segment", return_value=card):
            result = _judge_qe_jev("Hello {name}", "Bonjour", None, {})
        assert result.value == 0, result
        with patch.object(qe_jev, "score_segment", return_value=base):
            result = _judge_qe_jev("Hello", "Bonjour", None, {})
        assert result.not_applicable, result
    for enabled, key, data in (
        ("0", "fake", answer()),
        ("1", "", answer()),
        ("1", "fake", {"answers": {}}),
    ):
        with (
            patch.object(config, "TYPESAFE_API_KEY", key),
            patch.object(qe_jev, "_post", return_value=(200, data)) as post,
        ):
            settings.set_many({"qe_jev_enabled": enabled})
            card = qe_jev.score_segment("Hello {name}", "Bonjour", "en", "fr")
            assert card["score"] is None and card["structural_blocked"], card
            assert post.call_count == (enabled == "1" and bool(key))
    settings.set_many({"qe_jev_enabled": "1"})
    with patch.object(qe_jev, "_post", return_value=(200, answer())):
        for source, target, expected in (
            ("Hello {name}", "Bonjour", 0),
            ("PDF", "PDF", 100),
        ):
            result = _judge_qe_jev(
                source, target, None, {"source_locale": "en", "target_locale": "fr"}
            )
            assert result.value == expected, result


if __name__ == "__main__":
    main()
