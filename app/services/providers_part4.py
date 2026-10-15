"""providers: part 4 (layered pure move)

Documented in: docs/architecture/engines-and-providers.md
"""

from app.services.providers_common import ProviderSpec, VisionCapabilityResolution
from app.services.providers_part3 import _vision_capable_resolved


def resolve_vision_capability(spec_or_ref, org_id=None, model=None) -> VisionCapabilityResolution:
    """Resolve recognition, ring visibility, and vision capability with one authority read.

    Explicitly scoped callers use this result so an unreadable sacred gate cannot be confused
    with a genuinely non-vision engine.  Under an active gate, ``org_id=None`` is an install-level
    mission scope and admits only shared profiles; it is never a cross-org bypass.
    """
    if isinstance(spec_or_ref, ProviderSpec):
        ref = spec_or_ref.provider or ""
        if model is None:
            model = spec_or_ref.model
    else:
        ref = spec_or_ref or ""
    name = ref.strip().lower()
    if not name:
        return VisionCapabilityResolution(False, False, False)
    if not name.startswith("engine:"):
        # These are the recognized vision-slot built-ins. Other provider names (including NMT
        # names and typos) remain refusals at the Advanced tool seam.
        resolved = name in ("claude", "openai", "gemini", "local", "mock")
        return VisionCapabilityResolution(
            resolved,
            resolved,
            resolved and _vision_capable_resolved(name, model=model),
        )
    try:
        pid = int(name.split(":", 1)[1])
    except (ValueError, IndexError):
        return VisionCapabilityResolution(False, False, False)
    from app.services import engines

    try:
        prof = engines.get_profile(pid)
    except Exception:
        return VisionCapabilityResolution(False, False, False, authority_ok=False)
    if prof is None:
        return VisionCapabilityResolution(False, False, False)
    try:
        from app.services import access

        gate_on = access.multi_org_active()
    except Exception:
        return VisionCapabilityResolution(True, False, False, authority_ok=False)
    visible = not gate_on or access.engine_profile_permitted(getattr(prof, "org_id", None), org_id)
    return VisionCapabilityResolution(
        True,
        visible,
        visible and _vision_capable_resolved(name, model=model, profile=prof),
    )


def _authorized_ref_is_vision_capable(spec_or_ref, model=None) -> bool:
    """Capability check for the common provider funnel after admission happened upstream.

    This deliberately performs no org/gate decision: ``run_completion`` receives an already
    authorized ProviderSpec and must not reinterpret its omitted org as install-level admission.
    The scoped public helper below remains the admission-aware API.
    """
    if isinstance(spec_or_ref, ProviderSpec):
        ref = spec_or_ref.provider or ""
        if model is None:
            model = spec_or_ref.model
    else:
        ref = spec_or_ref or ""
    name = ref.strip().lower()
    if not name.startswith("engine:"):
        return _vision_capable_resolved(name, model=model)
    try:
        pid = int(name.split(":", 1)[1])
    except (ValueError, IndexError):
        return False
    from app.services import engines

    prof = engines.get_profile(pid)
    return _vision_capable_resolved(name, model=model, profile=prof)


def is_vision_capable(spec_or_ref, org_id=None, model=None) -> bool:
    """Whether the requested ref/model can carry images, independent of provider doubles
    and key presence. Reads the multi-org gate and, for engine refs, the profile row.
    Gate OFF: every existing profile is visible regardless of org_id, so a formerly
    rejected org-owned profile now reads capable if its kind/model supports images.
    Gate ON: NULL-org and same-org profiles are visible; foreign profiles are not.
    org_id=None admits ONLY install-level profiles under an active gate and no longer
    widens to org-owned profiles. Unreadable gate or profile reads return False (fail closed).

    Built-in claude/openai/gemini support images; local/openai_compatible support them
    unless the resolved model uses _local_prompt_adapter. Mock, NMT kinds, cohere and
    local adapter models are not capable. spec_or_ref may be a ProviderSpec or a bare
    ref; model overrides the resolved model when supplied. Callers needing to distinguish
    non-capability from unreadable authority must use resolve_vision_capability and
    VisionCapabilityResolution.authority_ok, resolved and visible.
    """
    return resolve_vision_capability(spec_or_ref, org_id=org_id, model=model).capable
