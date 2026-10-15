"""workflows_part2: shared leaf helpers (layered pure move)

Documented in: docs/architecture/workflow-engine.md
"""

from app.models import (
    StepType,
)
from app.services.workflows_common import (
    _RC_STEP_PREFIX,
    _TM_PROTECT_FALSE,
    _TM_PROTECT_TRUE,
    WorkflowError,
    _coerce_tm_pool_selection,
    _is_step_ref,
    _step_attr,
    slugify_key,
)

#: GitHub #502: ceiling for a workflow step's integer overrides (tm_fill_threshold,
#: tm_context_floor, max_visits). Far above any real value (the engine clamps TM scalars to
#: [50, 100]; max_visits is a loop guard defaulting to 5) and far below the int column's range.
MAX_STEP_INT_OVERRIDE = 1_000_000


class WorkflowForeignCustomerError(WorkflowError):
    """chip task_56b8b2bf: a workflow-create named a customer the caller may not scope
    to -- a customer-BOUND caller's foreign customer, or (multi-org) a customer that
    lives in another ORG. Raised at the shared create_workflow seam so BOTH the web and
    /api surfaces refuse the cross-tenant plant (the web POST /workflows route skipped
    the check the /api _api_workflow_customer funnel already did -- a bound key could
    plant a workflow into another customer's ring, a 200 it could then never see). A
    WorkflowError subclass so a generic `except WorkflowError` still catches it; the
    surfaces map it to the foreign-tenant convention (404, not enumerable)."""

    def __init__(self, customer_id):
        super().__init__(f"customer {customer_id} is not one you can scope a workflow to.")
        self.customer_id = customer_id


def _require_valid_lqa_threshold(n):
    """Validate an INBOUND lqa_threshold at the add/update seam, REJECTING an
    out-of-band or clearly-junk value instead of silently clamping it (BUG-A202: a
    typo'd -5 clamped to 0 = an LQA gate that passes everything, with a cheerful 200).

    * None -> None: the unset case. The NULL path is byte-identical to before — the
      step inherits the built-in LQA_PASS_THRESHOLD (90) at scoring time. Preserved
      exactly so the lqa_threshold wave's NULL contract does not regress.
    * a blank / whitespace-only STRING -> None: a web form posts the field empty when
      the user leaves it unset, which must stay "inherit", not become a 400.
    * a value that parses to an int in [0, 100] -> that int.
    * anything else (a number outside [0, 100], or non-numeric junk like "ninety")
      -> WorkflowError (a clean 400 on every surface), naming the valid band.

    Returned value is already an int-or-None, so the downstream `_clamp_lqa_threshold`
    calls are then idempotent (clamp of an in-band int is identity; None stays None)."""
    if n is None:
        return None
    if isinstance(n, str) and not n.strip():
        return None
    try:
        v = int(n)
    except (TypeError, ValueError, OverflowError) as e:  # OverflowError: JSON 1e999 -> inf
        raise WorkflowError(
            "LQA pass threshold must be a whole number from 0 to 100 "
            "(or blank to inherit the default)."
        ) from e
    if v < 0 or v > 100:
        raise WorkflowError("LQA pass threshold must be from 0 to 100 (got %d)." % v)  # noqa: UP031
    return v


def _coerce_tm_tristate_bool(v):
    """A nullable tristate bool override (tm_lock_ice / tm_hundreds_enter_lqa). Mirrors
    _coerce_tm_protect: None/""/"auto"/"inherit"/junk -> None (inherit the global); a real
    bool passes through; a recognised true/false string coerces accordingly."""
    if v is None or isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("", "auto", "inherit"):
        return None
    if s in _TM_PROTECT_TRUE:
        return True
    if s in _TM_PROTECT_FALSE:
        return False
    return None


def _require_valid_tm_scalar(v, label: str):
    """N2: validate an inbound tm_match int override (tm_fill_threshold / tm_context_floor)
    at the authoring seam, mirroring _require_valid_lqa_threshold. None/blank/'auto'/
    'inherit' -> None (inherit the global); a parseable int -> that int (UNCLAMPED here --
    the engine clamps [50,100] / [50,99] at read); anything else (e.g. '85%', 'abc', '8O')
    -> WorkflowError. Refused on EVERY surface, never silently cleared to inherit (the CLI
    regression the M5 fix introduced by dropping argparse type=int)."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("auto", "inherit"):
        return None
    try:
        n = int(s)
    except (TypeError, ValueError) as e:
        raise WorkflowError(
            f"{label} {v!r} must be a whole number (or blank / 'auto' to inherit the global)."
        ) from e
    # GitHub #502: still UNCLAMPED here (the engine clamps at read), but a value no engine band
    # could ever want must not reach the int column (OverflowError at commit on a 40-digit paste).
    if abs(n) > MAX_STEP_INT_OVERRIDE:
        raise WorkflowError(f"{label} {v!r} is out of range (use a whole number from 0 to 100).")
    return n


def _require_valid_tm_pool_selection(v):
    """N4: validate an inbound tm_pool_selection at the authoring seam. None/blank/'auto'/
    'inherit' -> None (all readable); the '(none)' sentinel (or an explicit empty list) ->
    [] (read NO pool); a list / comma-string of ids -> the de-duped list of VALID positive
    row ids (via ids.coerce_id, N3). A NON-empty input that NAMES ids but yields ZERO valid
    ones (e.g. 'garbage', 'abc,def', '1|2', '0', '-5') -> WorkflowError: a typo must not
    silently collapse 'all readable' into 'match nothing'. This makes the '(none)' sentinel
    branch LOAD-BEARING (it is the ONLY way an empty selection is accepted from a string)."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s.lower() in ("auto", "inherit"):
            return None
        if s.lower() in ("(none)", "none"):
            return []
    if isinstance(v, (list, tuple)) and len(v) == 0:
        return []  # an explicit empty selection from /api or MCP (read no pool)
    coerced = _coerce_tm_pool_selection(v)
    # coerced is None only for blank/auto/inherit (handled above); here it is a list. An
    # empty list means the input named ids but NONE were valid -> refuse (fail-closed).
    if not coerced:
        raise WorkflowError(
            f"TM pool selection {v!r} names no valid project id; use ids like '1,2', "
            "blank / 'auto' to read all readable pools, or '(none)' to read no pool."
        )
    return coerced


def _step_ref_key(ref) -> str:
    """The key part of a `step:<key>` ref, slugified. '' when it is blank/unslugifiable."""
    return slugify_key(ref[len(_RC_STEP_PREFIX) :]) if _is_step_ref(ref) else ""


def parse_run_condition_str(s: str) -> dict:
    """Parse the compact CLI form into a {ref, op, value} dict for validate_run_condition:
      'prev_lqa:failed'            -> {ref:'prev_lqa', op:'failed',    value:None}
      'prev_lqa:score_lt:90'       -> {ref:'prev_lqa', op:'score_lt',  value:90.0}
      'step:lqa_main:failed'       -> {ref:'step:lqa_main', op:'failed',   value:None}
      'step:lqa_main:score_lt:90'  -> {ref:'step:lqa_main', op:'score_lt', value:90.0}
    Raises WorkflowError on a shape the split cannot interpret (a non-numeric value or
    the wrong field count). Allowlist membership is enforced by validate_run_condition.

    The `step:` form (SPEC-parallel-routing §5.5) carries a colon INSIDE the ref, so it
    is destructured first: without that, 'step:lqa_main:failed' would split as
    ref='step', op='lqa_main', value='failed' and be rejected with a confusing
    "value must be numeric" naming a step key the author never meant as a number."""
    parts = (s or "").split(":")
    if parts and parts[0].strip() == "step":
        # 'step' ':' <key> ':' <op> [':' <value>]  -> 3 or 4 fields.
        if len(parts) in (3, 4):
            ref = _RC_STEP_PREFIX + parts[1].strip()
            op = parts[2].strip()
            if len(parts) == 3:
                return {"ref": ref, "op": op, "value": None}
            raw = parts[3].strip()
            try:
                val = float(raw)
            except ValueError as e:
                raise WorkflowError(
                    f"run condition value {raw!r} must be numeric (e.g. step:lqa_main:score_lt:90)."
                ) from e
            return {"ref": ref, "op": op, "value": val}
        raise WorkflowError(
            "a step run condition must be 'step:<key>:op' or 'step:<key>:op:value' "
            "(e.g. step:lqa_main:failed or step:lqa_main:score_lt:90)."
        )
    if len(parts) == 2:
        return {"ref": parts[0].strip(), "op": parts[1].strip(), "value": None}
    if len(parts) == 3:
        raw = parts[2].strip()
        try:
            val = float(raw)
        except ValueError as e:
            raise WorkflowError(
                f"run condition value {raw!r} must be numeric (e.g. prev_lqa:score_lt:90)."
            ) from e
        return {"ref": parts[0].strip(), "op": parts[1].strip(), "value": val}
    raise WorkflowError(
        "run condition must be 'ref:op' or 'ref:op:value' "
        "(e.g. prev_lqa:failed, prev_lqa:score_lt:90, or step:<key>:failed)."
    )


def _rc_ref_qualifies(s, ref: str) -> bool:
    """Does step `s` PRODUCE the outcome a short ref names? (prev_lqa -> an LQA step;
    prev_qe -> a qe_gate=True step.) The single definition shared by resolution and the
    parallel-group ambiguity check, so the two can never disagree about candidacy."""
    if ref == "prev_lqa":
        return _step_attr(s, "step_type") == StepType.LQA
    if ref == "prev_qe":
        return bool(_step_attr(s, "qe_gate", False))
    return False
