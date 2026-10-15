"""admin: part 3 (layered pure move)

Documented in: docs/architecture/configuration.md
"""

from fastapi import Request

from app.kernel import (
    segmentation_profiles as segprof,
)
from app.web.admin_common import _parse_custom_instructions
from app.web.admin_part2 import _seg_redirect
from app.web.deps import (
    _org_block,
)


def _engine_cfg_from_form(
    kind: str,
    formality: str,
    region: str,
    model_type: str = "",
    context: str = "",
    custom_instructions: str = "",
    *,
    access_key_secret: str = "",
    style: str = "",
    instructions: str = "",
    adapt_to: str = "",
    glossaries: str = "",
    confidence_floor: str = "",
    meaning_floor: str = "",
    timeout_s: str = "",
    max_parallel_jobs: str = "",
) -> dict:
    """Per-kind non-secret config from the Add/Edit form: DeepL formality/model_type/
    context/custom_instructions, Microsoft/Amazon region, Amazon formality, Lara
    style/instructions/adapt_to/glossaries, and the quality-judge floors + timeout
    (jev / jev_local; coerced and range-checked at the service,
    engines._normalize_judge_cfg). Blank values are dropped so an empty field
    never overwrites with junk. The PAIRED secret (Lara/Amazon access_key_secret) is
    passed as PLAINTEXT here and Fernet-encrypted at the config layer
    (engines._normalize_cfg); it follows the api_key rule (blank = keep) via the
    caller, so a blank is simply omitted here."""
    cfg: dict = {}
    if kind == "deepl":
        if (model_type or "").strip():
            cfg["model_type"] = model_type.strip()
        if (formality or "").strip() and formality != "default":
            cfg["formality"] = formality.strip()
        if (context or "").strip():
            cfg["context"] = context.strip()
        instrs = _parse_custom_instructions(custom_instructions)
        if instrs:
            cfg["custom_instructions"] = instrs
    if kind in ("microsoft", "amazon") and (region or "").strip():
        cfg["region"] = region.strip()
    if kind == "amazon" and (formality or "").strip() and formality != "default":
        cfg["formality"] = formality.strip()
    if kind == "lara":
        if (style or "").strip():
            cfg["style"] = style.strip()
        instrs = _parse_custom_instructions(instructions)  # reuse: line-per-instruction
        if instrs:
            cfg["instructions"] = instrs
        if (adapt_to or "").strip():
            cfg["adapt_to"] = adapt_to.strip()
        if (glossaries or "").strip():
            cfg["glossaries"] = glossaries.strip()
    # The paired secret: plaintext -> encrypted at the config layer. Only set when a
    # value is given (blank = keep the stored secret, like api_key).
    if kind in ("lara", "amazon") and (access_key_secret or "").strip():
        cfg["access_key_secret"] = access_key_secret.strip()
    if kind == "local" and (max_parallel_jobs or "").strip():
        cfg["max_parallel_jobs"] = max_parallel_jobs.strip()
    if kind in ("jev", "jev_local"):
        for key, raw in (
            ("confidence_floor", confidence_floor),
            ("meaning_floor", meaning_floor),
            ("timeout_s", timeout_s),
        ):
            if (raw or "").strip():
                cfg[key] = raw.strip()
    return cfg


def _owned_profile_or_redirect(request: Request, pid: int):
    """Fetch a segmentation profile and enforce the multi-org gate — the preamble
    shared by the 4 /settings/segprofiles/{pid}/* mutations (clone/default/delete/
    rename). Returns (src, blocked) where `blocked` is a ready _seg_redirect or None.
    A missing profile AND a foreign org's profile both surface the IDENTICAL
    'No such profile.' redirect: a foreign org's profile must look not-found, never
    enumerable (the leak vector — otherwise any org's admin could reach another org's
    profile). No-op while the org gate is off (same_org always True)."""
    src = segprof.get_profile(pid)
    if src is None:
        return None, _seg_redirect(err="No such profile.")
    if _org_block(request, getattr(src, "org_id", None)):
        return None, _seg_redirect(err="No such profile.")
    return src, None
