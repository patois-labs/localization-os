"""Seed the dedicated showcase demo backend, through service write seams only.

Creates a customer, an approved Acme Cloud glossary (8 terms x 5 locales), and
two universal workflows. Studio is a per-visitor mission created by the helper
below, never an install-time mission. No provider calls or user creation.

WARNING: LOC_TMS_DATA_DIR decides which database gets written. --data-dir points
at an install BEFORE app imports; pointing it at a real install writes there.
Run as a single operator, with a backup and no concurrent seed process.

Additive and idempotent: the customer's showcase_seed settings manifest owns
parent IDs, workflows additionally carry created_via=showcase-seed, and terms
and steps belong to those parents. Other customer settings are always merged.
Existing parents are preserved; missing parents and missing steps are repaired.
--dry-run prints the plan without writes. --settings opts into install settings.
--mint-key EMAIL prints a newly minted scoped key ONCE; existing active seed
keys are reused without revealing their unrecoverable plaintext.

--cleanup revokes keys, deletes workflows, deletes the manifest glossary, then
asks the customer service to delete the customer. Visitor projects and missions
belong to retention; references can refuse customer deletion (exit 2). Settings
are not reverted. No SQL writes, migrations, or live provider credentials.
All console output uses the CLI's ASCII-safe writer.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

SHOWCASE_MARKER = "showcase-seed"
CUSTOMER_NAME = "Showcase (seeded)"
GLOSSARY_NAME = "Showcase Brand Terms (seeded)"
KEY_NAME = "Showcase BFF (seeded)"
ADVERTISED_LOCALES = ("en-US", "es-ES", "fr-FR", "de-DE", "ja-JP", "zh-CN", "pt-BR", "it-IT")
GLOSSARY_LOCALES = ("es-ES", "fr-FR", "de-DE", "ja-JP", "zh-CN")
TIERS = (
    ("quick", "Quick Translate", ("llm_agent",)),
    ("reviewed", "Reviewed Translate", ("llm_agent", "lqa")),
    ("studio", "Studio Translate", ("committee",)),
)
STUDIO_ROSTER = {
    "model_roster": {
        "provider": "mock",
        "mode": "conductor",
        "committee": {"text": [{"provider": "mock"}, {"provider": "mock"}]},
    },
    "tool_allowlist": [
        "tm.examples",
        "glossary.plan",
        "provider.translate",
        "target.propose",
        "deliver",
    ],
    "budget_usd": 1.0,
    "max_tokens": 20000,  # pregate: allow-secret token budget, not a credential
    "max_tool_calls": 100,
    "max_provider_calls_per_unit": 6,
    "wall_clock_seconds": 60,
}


def _studio_available() -> bool:
    """Studio is an Advanced Workflows mission: absent in the open-core edition."""
    from app import editions

    return editions.addon_installed("advanced_workflows")


SETTINGS_KEYS = (
    "enabled_locales",
    "spend_cap_usd",
    "retention_enabled",
    "retention_days",
    *(("advanced_workflows_enabled",) if _studio_available() else ()),
    "security_allowed_hosts",
)


def _emit(text):
    from app.cli._common import _write_ascii_safe

    _write_ascii_safe(sys.stdout, str(text) + "\n")


def create_studio_mission(
    session_or_none, project_id, document_ids=None, *, org_id=None, customer_id=None
) -> dict:
    """Trusted in-process S2 helper for one single-pair visitor project.

    Call after committing project/documents. None rings mean a trusted in-process
    caller. HTTP/BFF callers must pass their resolved org_id and showcase
    customer_id, and enforce visitor ownership before using this helper. A multi-pair project
    is refused rather than silently choosing a visitor's target language.
    """
    if not _studio_available():
        _emit("Studio skipped: the Advanced Workflows add-on is not installed.")
        return {}

    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Project, TargetLocale
    from app.services import advanced

    def create(session):
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"project {project_id} does not exist")
        locales = session.exec(
            select(TargetLocale).where(TargetLocale.project_id == project_id)
        ).all()
        if len(locales) != 1:
            raise ValueError("Studio requires a visitor project with exactly one target locale")
        options = copy.deepcopy(STUDIO_ROSTER)
        options["model_roster"].update(
            source_locale=project.source_locale,
            target_locale=locales[0].locale,
        )
        return advanced.create_mission(
            project_id=project_id,
            name=TIERS[2][1],
            brief="Translate the visitor text; preserve brand terms and placeholders.",
            document_ids=document_ids,
            created_by=SHOWCASE_MARKER,
            org_id=org_id,
            customer_id=customer_id,
            **options,
        )

    if session_or_none is not None:
        return create(session_or_none)
    with Session(engine) as session:
        return create(session)


def _customer(session):
    from sqlmodel import select

    from app.models import Customer

    rows = session.exec(select(Customer).where(Customer.name == CUSTOMER_NAME)).all()
    if len(rows) > 1:
        raise ValueError(
            "multiple showcase customers found; resolve duplicate names before seeding"
        )
    if not rows:
        return None
    manifest = (rows[0].settings or {}).get("showcase_seed")
    if not isinstance(manifest, dict) or manifest.get("marker") != SHOWCASE_MARKER:
        raise ValueError("showcase customer name already exists without a seed manifest; refusing")
    if manifest.get("version") != 1:
        raise ValueError("unsupported showcase seed manifest version")
    return rows[0]


def _save(session, cid, manifest):
    from app.services import customers

    customers.update_customer_settings(session, cid, {"showcase_seed": copy.deepcopy(manifest)})


def _seed(session, customer):
    from sqlmodel import select

    from app.models import ApiKey, Glossary, GlossaryEntry, GlossaryPair, Workflow, WorkflowStep
    from app.services import content, glossary_module, lqa, workflows
    from scripts.seed_demo_content_common import ALL_LOCALES, GLOSSARY_TERMS

    if customer is None:
        customer = content.customer_create(session, CUSTOMER_NAME)
        manifest = {
            "marker": SHOWCASE_MARKER,
            "version": 1,
            "glossary_id": None,
            "workflow_ids": [],
            "api_key_ids": [],
            "seeded_at": datetime.now(timezone.utc).isoformat(),
        }
        _save(session, customer.id, manifest)
    else:
        manifest = copy.deepcopy(customer.settings["showcase_seed"])
    cid = customer.id
    _emit(f"customer {CUSTOMER_NAME!r} (id={cid}).")
    gid = manifest.get("glossary_id")
    glossary = session.get(Glossary, gid) if gid else None
    if glossary is None:
        glossary = glossary_module.create(session, GLOSSARY_NAME, created_by=SHOWCASE_MARKER)
        manifest["glossary_id"] = glossary.id
        _save(session, cid, manifest)
    current_pairs = {
        (p.source_locale, p.target_locale)
        for p in session.exec(select(GlossaryPair).where(GlossaryPair.glossary_id == glossary.id))
    }
    expected_pairs = {("en-US", t) for t in GLOSSARY_LOCALES}
    changed = not expected_pairs.issubset(current_pairs)
    if changed:
        glossary_module.set_pairs(session, glossary.id, sorted(current_pairs | expected_pairs))
    current_terms = {
        (e.source_term, e.source_locale, e.target_locale)
        for e in session.exec(select(GlossaryEntry).where(GlossaryEntry.glossary_id == glossary.id))
    }
    for source, translations, dnt in [*GLOSSARY_TERMS[:5], *GLOSSARY_TERMS[-3:]]:
        for target, translation in zip(ALL_LOCALES, translations, strict=True):
            if (source, "en-US", target) not in current_terms:
                glossary_module.add_term(
                    session,
                    glossary.id,
                    source_term=source,
                    target_term=translation,
                    source_locale="en-US",
                    target_locale=target,
                    dnt=dnt,
                )
                changed = True
    if changed or glossary.status != "approved":
        glossary_module.approve(session, glossary.id, approver_email=SHOWCASE_MARKER)
    _emit(f"glossary {GLOSSARY_NAME!r} (id={glossary.id}): 8 terms x 5 locales, 3 DNT.")
    old_ids = list(manifest.get("workflow_ids", []))
    new_ids = []
    for idx, (_, name, shape) in enumerate(TIERS[:2]):
        wid = old_ids[idx] if idx < len(old_ids) else None
        wf = session.get(Workflow, wid) if wid else None
        if wf is None:
            wf = workflows.create_workflow(
                session, name, scope="global", customer_id=str(cid), created_via=SHOWCASE_MARKER
            )
        new_ids.append(wf.id)
        # Preserve later IDs in the checkpoint until they have been inspected.
        manifest["workflow_ids"] = new_ids + old_ids[idx + 1 :]
        _save(session, cid, manifest)
        existing = session.exec(
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == wf.id)
            .order_by(WorkflowStep.order_index)
        ).all()
        for step_index, kind in enumerate(shape):
            name = "MT" if kind == "llm_agent" else "Gate"
            bound_gid = glossary.id if idx == 1 and kind == "llm_agent" else None
            step = next((st for st in existing if st.name == name and st.step_type == kind), None)
            if step is None:
                step = workflows.add_step(
                    session,
                    wf.id,
                    name=name,
                    step_type=kind,
                    provider="",
                    model="",
                    prompt_text="Translate: {text}" if kind == "llm_agent" else "",
                    glossary_id=bound_gid,
                    lqa_threshold=lqa.LQA_PASS_THRESHOLD if kind == "lqa" else None,
                )
                if step.order_index != step_index:
                    workflows.reorder_step(session, wf.id, step.id, step_index)
            elif bound_gid and step.glossary_id != bound_gid:
                workflows.update_step(session, step.id, glossary_id=bound_gid)
        _emit(f"workflow {wf.name!r} (id={wf.id}): universal, {len(shape)} step(s).")
    manifest["workflow_ids"] = new_ids
    manifest["api_key_ids"] = [
        kid for kid in manifest.get("api_key_ids", []) if session.get(ApiKey, kid)
    ]
    _save(session, cid, manifest)
    return cid, manifest


def _cleanup(session, customer, dry_run):
    from sqlmodel import select

    from app.models import Glossary, Workflow
    from app.services import apikeys, customers, glossary_module, workflows

    manifest = customer.settings["showcase_seed"] if customer else {}
    key_ids = set(manifest.get("api_key_ids", []))
    workflow_ids = set(manifest.get("workflow_ids", []))
    if customer:
        workflow_ids.update(
            session.exec(
                select(Workflow.id).where(
                    Workflow.created_via == SHOWCASE_MARKER,
                    Workflow.customer_id == customer.id,
                )
            ).all()
        )
    workflow_ids = {
        wid
        for wid in workflow_ids
        if customer
        and (wf := session.get(Workflow, wid)) is not None
        and wf.customer_id == customer.id
    }
    prefix = "[dry-run] would " if dry_run else ""
    revoke = "revoke" if dry_run else "revoked"
    delete = "delete" if dry_run else "deleted"
    for kid in sorted(key_ids):
        if not dry_run:
            apikeys.revoke(kid)
        _emit(f"{prefix}{revoke} key id={kid}.")
    for wid in sorted(workflow_ids):
        if session.get(Workflow, wid) is None:
            continue
        # LOW-2: the SAME read-only eligibility check the live delete raises on, covering
        # EVERY delete_workflow guard (memory-suggestion tombstone, vendor dispatch,
        # empty-held tm_match orphan, transient blockers) in the same order -- so the
        # dry-run forecast can never disagree with the outcome, and it STOPS at the first
        # refusing workflow exactly as the live path returns there. No preview/outcome
        # asymmetry: neither branch forecasts or performs the glossary/customer removals
        # once a workflow refuses.
        reason = workflows.workflow_delete_block_reason(session, wid)
        if reason is not None:
            if dry_run:
                _emit(f"[dry-run] would refuse workflow id={wid}: {reason}")
                _emit(
                    f"[dry-run] would stop here (workflow id={wid} refuses); the glossary and "
                    "customer are left untouched, matching the live --cleanup."
                )
                return 0
            # Live: the seed must never surface this as the generic exit-1 'unexpected error':
            # print the service's own actionable reason and stop with the same exit code the
            # customer refusal uses. Every API key in the manifest was revoked above, so the
            # install is already safe to leave in this half-cleaned state.
            _emit(reason)
            _emit(
                f"showcase cleanup: workflow id={wid} stays; archive it or clear the "
                "blocker, then re-run --cleanup. Keys are already revoked; the glossary "
                "and customer were left untouched."
            )
            return 2
        if not dry_run:
            workflows.delete_workflow(session, wid)
        _emit(f"{prefix}{delete} workflow id={wid}.")
    gid = manifest.get("glossary_id")
    if gid and session.get(Glossary, gid) is not None:
        if not dry_run:
            glossary_module.delete(session, gid)
        _emit(f"{prefix}{delete} glossary id={gid}.")
    _emit("Settings are not reverted: " + ", ".join(SETTINGS_KEYS) + ".")
    _emit("Reset each saved value with: python -m app.cli config set <key> <before-value>")
    if customer:
        cid = customer.id
        if dry_run:
            refs = customers.reference_count(session, cid)
            # Project the service's counts after the parent removals above.
            refs["workflows"] -= len(workflow_ids)
            glossary = session.get(Glossary, gid) if gid else None
            if glossary and glossary.customer_id == cid:
                refs["glossaries"] -= 1
            refs["total"] = sum(v for k, v in refs.items() if k != "total")
            if refs["total"]:
                detail = ", ".join(
                    f"{v} {k.replace('_', ' ')}" for k, v in refs.items() if k != "total" and v
                )
                _emit(f"[dry-run] would refuse (still referenced by {detail}) customer id={cid}.")
                return 0
        else:
            try:
                customers.delete_customer(session, cid)
            except customers.CustomerDeleteBlocked as exc:
                _emit(str(exc))
                _emit(
                    "showcase cleanup: the customer stays until retention purge removes "
                    "the visitor projects; re-run --cleanup afterwards"
                )
                return 2
        _emit(f"{prefix}{delete} customer id={cid}.")
    return 0


def _settings(args):
    from app.services import settings

    if not args.settings:
        return {}
    if not math.isfinite(args.spend_cap) or args.spend_cap <= 0:
        raise ValueError("--spend-cap must be > 0 and finite (ON and finite)")
    if args.retention_days <= 0:
        raise ValueError("--retention-days must be > 0")
    values = {
        "enabled_locales": json.dumps(list(ADVERTISED_LOCALES)),
        "spend_cap_usd": str(args.spend_cap),
        "retention_enabled": "1",
        "retention_days": str(args.retention_days),
    }
    if _studio_available():
        values["advanced_workflows_enabled"] = "1"
    if args.allowed_hosts is not None:
        values["security_allowed_hosts"] = args.allowed_hosts
    for key, value in values.items():
        settings.validate_value(key, value)
    return values


def _settings_table(values, before, planned=False):
    _emit("setting | before | after" + (" (planned)" if planned else ""))
    for key, value in values.items():
        _emit(f"{key} | {before[key]!r} | {value!r}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--settings", action="store_true")
    parser.add_argument("--spend-cap", type=float, default=25.0)
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--allowed-hosts")
    parser.add_argument("--mint-key", metavar="EMAIL")
    args = parser.parse_args(argv)
    if args.data_dir:
        if "app.db" in sys.modules:
            current = os.environ.get("LOC_TMS_DATA_DIR", "data")
            if Path(current).resolve() != Path(args.data_dir).resolve():
                _emit("--data-dir must be selected before app imports; start a fresh process")
                return 2
        os.environ["LOC_TMS_DATA_DIR"] = args.data_dir
    try:
        from sqlmodel import Session, select

        from app.db import engine, init_db
        from app.models import ApiKey, User
        from app.services import apikeys, settings

        if args.cleanup and (args.settings or args.mint_key):
            raise ValueError("--cleanup cannot be combined with --settings or --mint-key")
        _emit("WARNING: LOC_TMS_DATA_DIR selects the database; target=" + str(engine.url))
        fresh = engine.url.get_backend_name() == "sqlite" and not Path(engine.url.database).exists()
        if not args.dry_run:
            init_db()
        if args.dry_run and args.settings:
            from sqlalchemy import inspect

            if not inspect(engine).has_table("setting"):
                init_db()
                _emit(
                    "[dry-run] initialized schema for public settings validation; "
                    "no seed rows or settings values written."
                )
        values = _settings(args)
        before = {key: settings.get(key) for key in values}
        if args.dry_run and values:
            _settings_table(values, before, planned=True)
        if args.dry_run and args.cleanup and fresh:
            _emit("[dry-run] no database exists; nothing to clean up. Settings are not reverted.")
            return 0
        if args.dry_run and not args.cleanup:
            if not fresh:
                with Session(engine) as session:
                    _customer(session)
                    if args.mint_key:
                        user = session.exec(
                            select(User).where(
                                User.email == args.mint_key.strip().lower(),
                                User.active.is_(True),
                            )
                        ).first()
                        if user is None:
                            raise ValueError(
                                "--mint-key: active user does not exist; no user created"
                            )
            elif args.mint_key:
                raise ValueError("--mint-key: active user does not exist; no user created")
            _emit("[dry-run] no changes will be written.")
            _emit(f"[dry-run] create/reuse customer {CUSTOMER_NAME!r} and its seed manifest.")
            _emit(
                f"[dry-run] create missing glossary {GLOSSARY_NAME!r}: "
                "8 terms x 5 locales, 3 DNT, approved General."
            )
            for _, name, shape in TIERS[:2]:
                _emit(f"[dry-run] create/repair {name!r}: universal, {' + '.join(shape)}.")
            if args.mint_key:
                _emit(
                    "[dry-run] would mint/reuse scoped api key for existing user "
                    f"{args.mint_key!r}."
                )
            if _studio_available():
                _emit("Studio roster: " + json.dumps(STUDIO_ROSTER, sort_keys=True))
            return 0
        with Session(engine) as session:
            customer = _customer(session)
            if args.cleanup:
                return _cleanup(session, customer, args.dry_run)
            user = None
            if args.mint_key:
                user = session.exec(
                    select(User).where(User.email == args.mint_key.strip().lower())
                ).first()
                if user is None or not user.active:
                    raise ValueError(
                        f"--mint-key: active user {args.mint_key!r} does not exist; no user created"
                    )
            cid, manifest = _seed(session, customer)
            if values:
                settings.set_many(values)
                settings.apply_to_config()
                _settings_table(values, before)
            if user is not None:
                existing = [session.get(ApiKey, kid) for kid in manifest["api_key_ids"]]
                if any(
                    k
                    and k.active
                    and k.user_id == user.id
                    and {"api", "docs"}.issubset(apikeys.scope_set(k.scopes))
                    for k in existing
                ):
                    _emit("existing active seed API key reused; plaintext cannot be recovered.")
                else:
                    key, plaintext = apikeys.create_key(user.id, name=KEY_NAME, scopes="api docs")
                    manifest["api_key_ids"].append(key.id)
                    try:
                        _save(session, cid, manifest)
                    except Exception:
                        apikeys.revoke(key.id)
                        raise
                    for previous in existing:
                        if previous and previous.active and previous.user_id == user.id:
                            apikeys.revoke(previous.id)
                    _emit(
                        f"API key id={key.id}: {plaintext} STORE THIS NOW - "
                        "it is shown only once and cannot be recovered."
                    )
        if _studio_available():
            _emit("Studio roster: " + json.dumps(STUDIO_ROSTER, sort_keys=True))
            _emit("Studio needs --settings or advanced_workflows_enabled=1.")
        _emit("done.")
        return 0
    except ValueError as exc:
        _emit(str(exc))
        return 2
    except Exception as exc:
        # #137: a service REFUSAL is an expected, actionable outcome (exit 2 with the reason),
        # never the opaque exit-1 'unexpected error'. _cleanup guards its own workflow deletes;
        # this keeps the generic handler from masking one that reached here from anywhere else.
        from app.services.workflows import WorkflowError

        if isinstance(exc, WorkflowError):
            _emit(str(exc))
            _emit(
                "showcase cleanup: archive the workflow or clear the blocker, then re-run "
                "--cleanup."
            )
            return 2
        # Never echo unexpected exception payloads that might include plaintext keys.
        _emit(
            f"showcase seed unexpected error: {type(exc).__name__}; inspect the install and re-run."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
