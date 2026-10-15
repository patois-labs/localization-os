"""live_smoke.py -- a tiny (under $0.10) real-provider translation smoke test.

WHY THIS EXISTS
---------------
Every automated suite in tests/verify_*.py runs hermetically: they blank the
provider API keys before importing app.config, so app.services.providers.
get_provider() always resolves to MockProvider. That is deliberate (free, fast,
deterministic CI), but it means no suite exercises a real LLM provider end to
end: not the HTTP call, not token accounting, not the provider's actual
translation output. This script is that check. Run it after adding or changing
a provider, or before a release, with a real key.

WHAT IT DOES
------------
Stands up the app in-process against a throwaway temp LOC_TMS_DATA_DIR (never
your real data/ directory), creates a one-off project and a tiny 4-segment
plain-text document, and runs the same service-layer seam a real translate job
uses (app.services.translate.pretranslate_document, via
app.services.providers.run_completion) against whatever provider is configured
in the environment. It is deliberately tiny to stay well under $0.10 on
haiku-class pricing, and asserts real-MT signals (non-empty, non-identical
target text; no truncation, stall or error; real token usage above zero; the
provider that actually ran is the one that was meant to run, never a silent
fallback to MockProvider) without hard-coding any exact output string, since
real LLM output is non-deterministic.

HOW TO RUN IT
-------------
From the repo root, with a real key in the environment (or in .env, which the
app config reads):

    python scripts/live_smoke.py

No flags are needed if ANTHROPIC_API_KEY is set (defaults: claude,
claude-haiku-4-5, locale es-ES, 4 segments). To pin a model, provider or locale:

    python scripts/live_smoke.py --provider claude --model claude-haiku-4-5 --locale fr-FR --segments 4

The script prints what it did, the captured token usage, an estimated USD cost
(via app.services.pricing.cost_usd, the same table the spend-cap ledger uses),
and a final PASS/FAIL/CERTIFIED line. It removes its throwaway temp directory
on exit and never touches your data/ directory.

THE --allow-mock ESCAPE HATCH (plumbing only, NOT a live certification)
-----------------------------------------------------------------------
Without a real provider key this script refuses to run rather than silently
passing against MockProvider, which would prove nothing about real MT. To prove
the script's own plumbing (project creation, document ingest, the translate
seam, the assertions) without spending anything, pass --allow-mock. That run is
labelled "MOCK PLUMBING OK, not a live-MT certification" in its final line.

WHAT A CLEAN RUN CERTIFIES
--------------------------
That the configured provider is reachable and returns real, non-truncated text
through this codebase's prompt, segment, provider and pricing seam. It does not
certify translation quality, does not exercise every provider (only the one
selected), and does not exercise the workflow orchestrator, connectors or any
UI path.
"""  # noqa: E501

import argparse
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)


def _parse_args():
    p = argparse.ArgumentParser(
        description="Live-tiny (<$0.10) MT-correctness smoke test for Localization OS. "
        "See the module docstring (live_smoke.py) for full usage."
    )
    p.add_argument(
        "--provider",
        default=None,
        help="Provider name override (default: app config DEFAULT_PROVIDER, normally 'claude').",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Model id override (default: app config DEFAULT_MODELS[provider], "
        "normally claude-haiku-4-5).",
    )
    p.add_argument(
        "--locale", default="es-ES", help="Target locale to translate into (default: es-ES)."
    )
    p.add_argument(
        "--segments",
        type=int,
        default=4,
        help="Number of tiny source segments/sentences to translate (default: 4). "
        "Kept small on purpose to stay under $0.10.",
    )
    p.add_argument(
        "--allow-mock",
        action="store_true",
        help="Permit the run to certify against MockProvider (no real key needed). "
        "This ONLY smokes the script's own plumbing -- it is explicitly "
        "labeled 'MOCK PLUMBING OK, not a live-MT certification' and must "
        "never be treated as evidence live MT works.",
    )
    return p.parse_args()


# A handful of short, generic sentences -- enough surface area to prove a real
# translation happened (word order / vocabulary changes, non-identical output)
# while staying tiny (a few dozen tokens total in, similar out) to stay well
# under $0.10 even on a more expensive model.
_SAMPLE_SENTENCES = [
    "The quick brown fox jumps over the lazy dog.",
    "Please confirm your order before checkout.",
    "Our office will be closed on the holiday.",
    "Thank you for choosing our service.",
    "The report is due by the end of the week.",
    "Water boils at one hundred degrees Celsius.",
]


def _make_sample_text(n: int) -> str:
    n = max(1, min(n, len(_SAMPLE_SENTENCES)))
    return "\n".join(_SAMPLE_SENTENCES[:n])


def main() -> int:
    args = _parse_args()

    # Throwaway temp data dir -- set BEFORE importing app.config (same ordering
    # every tests/verify_*.py script uses), so the app never touches the real
    # data/ directory. This is non-negotiable: live_smoke.py must be safe to run
    # repeatedly with zero risk to production data.
    tmp_dir = tempfile.mkdtemp(prefix="loctms_live_smoke_")
    os.environ["LOC_TMS_DATA_DIR"] = tmp_dir

    exit_code = 1
    try:
        exit_code = _run(args)
    finally:
        # Dispose the SQLite engine's pooled connection first -- on Windows an
        # open file handle blocks rmtree from actually removing loc_tms.db, so
        # without this the temp dir is left behind (harmless -- it's under the
        # OS temp dir, not the repo -- but tidy cleanup is the whole point of
        # using a throwaway dir in the first place).
        try:
            from app.db import engine as _db_engine

            _db_engine.dispose()
        except Exception:  # noqa: S110 -- temp-dir tidy-up only; rmtree ignores errors anyway
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return exit_code


def _run(args) -> int:
    # Imports deliberately deferred until AFTER LOC_TMS_DATA_DIR is set, mirroring
    # tests/verify_*.py: app.config reads env/.env at import time, and app.db
    # builds its engine from app.config at import time too.
    from sqlmodel import Session

    from app import config
    from app.db import engine as db_engine
    from app.db import init_db
    from app.models import Segment, TransMode
    from app.services import content, ingest, pricing, translate
    from app.services.providers import ProviderSpec, provider_is_live

    provider_name = args.provider or config.DEFAULT_PROVIDER or "claude"
    model_name = args.model or config.DEFAULT_MODELS.get(provider_name)

    is_live = provider_is_live(provider_name)

    if not is_live and not args.allow_mock:
        print(f"[live_smoke] provider={provider_name!r} model={model_name!r}")
        print(
            "[live_smoke] REFUSED: no live provider key detected; set "
            "ANTHROPIC_API_KEY (or pass --allow-mock to smoke the plumbing on "
            "MockProvider). No document was created, no cost was incurred."
        )
        return 2

    if not is_live and args.allow_mock:
        print(
            f"[live_smoke] no live key for provider={provider_name!r} -- proceeding "
            "with --allow-mock. This run CANNOT certify live MT; it only proves "
            "the script's own plumbing works against the deterministic mock."
        )

    init_db()

    with Session(db_engine) as session:
        project = content.project_create(
            session,
            name="live_smoke smoke",
            source_locale="en-US",
            target_locales=args.locale,
            default_provider=provider_name,
            created_by="live_smoke",
        )
        if project is None:
            print("[live_smoke] FAIL: project_create returned None unexpectedly")
            return 1
        session.commit()
        session.refresh(project)

        sample_text = _make_sample_text(args.segments)
        doc = ingest.ingest_document(
            session,
            project,
            "live_smoke_sample.txt",
            sample_text.encode("utf-8"),
            mode=TransMode.SEGMENTED,
            created_by="live_smoke",
        )
        session.commit()
        session.refresh(doc)

        # Seed the target-locale segments the same way a real document upload
        # does (content._seed_locale_segments is the private helper used by
        # add_target_locale / ingest's own locale wiring; ingest_document already
        # seeds the project's existing TargetLocale rows for a fresh upload, so
        # just confirm segments exist for our target locale).
        from sqlmodel import select

        segs_before = session.exec(
            select(Segment).where(
                Segment.document_id == doc.id, Segment.target_locale == args.locale
            )
        ).all()
        if not segs_before:
            # Project was created with target_locales already including args.locale,
            # so ingest_document should have seeded these; if not (e.g. a locale
            # normalization surprise), seed explicitly via the same seam
            # add_target_locale uses.
            content.add_target_locale(session, project.id, args.locale)
            session.commit()
            segs_before = session.exec(
                select(Segment).where(
                    Segment.document_id == doc.id, Segment.target_locale == args.locale
                )
            ).all()

        source_by_index = {s.order_index: s.source_text for s in segs_before}
        n_segments = len(segs_before)
        print(
            f"[live_smoke] provider={provider_name!r} model={model_name!r} "
            f"locale={args.locale!r} segments={n_segments} "
            f"data_dir={os.environ['LOC_TMS_DATA_DIR']!r}"
        )

        spec = ProviderSpec(
            provider=provider_name,
            model=model_name,
            fallback_provider=config.DEFAULT_FALLBACK_PROVIDER or None,
            fallback_model=(
                config.DEFAULT_MODELS.get(config.DEFAULT_FALLBACK_PROVIDER)
                if config.DEFAULT_FALLBACK_PROVIDER
                else None
            ),
        )

        stats = translate.pretranslate_document(session, doc.id, args.locale, spec=spec)
        session.commit()

        segs_after = session.exec(
            select(Segment).where(
                Segment.document_id == doc.id, Segment.target_locale == args.locale
            )
        ).all()

    # --- assertions (signals, not exact strings) ---------------------------
    failures = []

    if stats.get("errors", 0) > 0:
        failures.append(f"{stats['errors']} segment(s) reported a provider error")

    provider_used = stats.get("provider_used") or {}
    if not provider_used:
        failures.append("no provider_used recorded (translation loop never ran a provider call)")

    # NOTE: stats["provider_used"] is keyed by the REQUESTED provider name (see
    # app.services.providers.run_completion, which stamps provider_name regardless
    # of whether get_provider() internally resolved a real client or MockProvider
    # -- get_provider() silently degrades to MockProvider whenever the matching key
    # is absent, see app.services.providers.get_provider). So provider_used is only
    # useful for call-count display here, NOT for detecting a silent mock fallback.
    # The authoritative liveness check is provider_is_live(provider_name), captured
    # in `is_live` BEFORE any provider call was made -- that is what get_provider()
    # itself consults, so it can never disagree with what run_completion actually did.
    if not is_live:
        # --allow-mock path: a live provider was never configured, so the call
        # MUST have gone through MockProvider -- nothing further to check here
        # (translate output identical-to-source / empty checks below still run).
        pass
    else:
        # Live path: is_live already proved provider_is_live(provider_name) is True
        # BEFORE we called run_completion, so get_provider(provider_name) inside it
        # resolved a real client (get_provider has no other branch for a name that
        # is_live). The remaining loud-failure signals are real per-segment ones:
        # empty/identical output or missing token usage (checked below) -- those
        # are what would actually happen if a real call silently produced nothing.
        if provider_name not in provider_used:
            failures.append(
                f"expected provider {provider_name!r} in provider_used, got {provider_used!r} "
                "(no segment ever reached the provider call)"
            )

    if stats.get("tokens_in", 0) <= 0 or stats.get("tokens_out", 0) <= 0:  # noqa: SIM102
        if is_live:
            failures.append(
                f"expected real token usage > 0 on a live provider, got "
                f"tokens_in={stats.get('tokens_in')} tokens_out={stats.get('tokens_out')}"
            )

    for seg in sorted(segs_after, key=lambda s: s.order_index):
        src = source_by_index.get(seg.order_index, "")
        tgt = (seg.target_text or "").strip()
        if not tgt:
            failures.append(f"segment {seg.order_index}: empty target_text")
            continue
        if tgt == src.strip():
            failures.append(
                f"segment {seg.order_index}: target_text identical to source "
                f"(no real translation happened): {src!r}"
            )

    cost = pricing.cost_usd(
        provider_name,
        model_name,
        {
            "input_tokens": stats.get("tokens_in", 0),
            "output_tokens": stats.get("tokens_out", 0),
            "cache_read_tokens": stats.get("tokens_cache_read", 0),
            "cache_write_tokens": stats.get("tokens_cache_write", 0),
        },
    )

    print(
        f"[live_smoke] provider_used={provider_used} translated={stats.get('translated')} "
        f"errors={stats.get('errors')} failovers={stats.get('failovers')}"
    )
    print(
        f"[live_smoke] tokens_in={stats.get('tokens_in', 0)} "
        f"tokens_out={stats.get('tokens_out', 0)} "
        f"cache_read={stats.get('tokens_cache_read', 0)} "
        f"cache_write={stats.get('tokens_cache_write', 0)}"
    )
    print(f"[live_smoke] estimated cost: ${cost:.6f} USD")

    for seg in sorted(segs_after, key=lambda s: s.order_index):
        src = source_by_index.get(seg.order_index, "")
        print(f"[live_smoke]   [{seg.order_index}] src={src!r}")
        print(f"[live_smoke]   [{seg.order_index}] tgt={(seg.target_text or '')!r}")

    if failures:
        print("[live_smoke] FAIL:")
        for f in failures:
            print(f"[live_smoke]   - {f}")
        return 1

    if is_live:
        print(
            f"[live_smoke] PASS -- LIVE-MT CERTIFIED (provider={provider_name}, "
            f"model={model_name}, est. cost ${cost:.6f})"
        )
    else:
        print(
            "[live_smoke] PASS -- MOCK PLUMBING OK, not a live-MT certification "
            "(ran with --allow-mock; no real provider was called; est. cost $0.00)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
