"""Bind a confirmed spend call to the plan the user saw (issue #757).

Before this, `confirm=true` was a bare boolean the model could set: it could plan a
3-locale run, get the user's yes, then call confirm=true with 9 locales or another
connection and nothing noticed. Now the planning call returns a `plan_id`, and a
confirmed call must present it:

  * the plan_id is random (unguessable) and remembered with the tool name, a digest of
    the normalized arguments, a fingerprint of the caller's token and an expiry;
  * the confirmed call must come from the same caller, for the same tool, with the
    same arguments (anything but `confirm` / `plan_id`), before the expiry;
  * a plan_id is single use: a second confirm with it is refused (no replayed spend).

Every refusal is a `PlanRefused` with code `spend_refused`, raised BEFORE the handler,
so nothing is sent and nothing is spent. Stdlib only, like registry.py.

The store is per process: a restarted stdio server forgets its plans (the agent plans
again), and the streamable-http listener is one process. It is bounded (oldest plans
are dropped first) so an unauthenticated flood of planning calls cannot grow it.
"""

from __future__ import annotations

import collections
import hashlib
import json
import secrets
import threading
import time

DEFAULT_TTL_S = 30 * 60
DEFAULT_MAX_PLANS = 2000
#: Arguments that are not part of what the user approved.
#: `idempotency_key` (m-jobs) is a retry token, not a choice the user approved: a plan made
#: without one is confirmed with one, and a replay with the SAME key passes (see _check).
_NOT_BOUND = ("confirm", "plan_id", "idempotency_key")


class PlanRefused(ValueError):
    """A confirmed spend call whose plan_id does not hold. The message is user-facing."""

    code = "spend_refused"


def normalized_args(args: dict) -> dict:
    """The arguments the plan covers: no confirm / plan_id, and an omitted argument
    equals one passed as null (both mean "the default")."""
    return {k: v for k, v in sorted((args or {}).items()) if k not in _NOT_BOUND and v is not None}


def args_digest(args: dict) -> str:
    blob = json.dumps(
        normalized_args(args),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def caller_fingerprint(token: str | None) -> str:
    return hashlib.sha256(("loc-plan:" + str(token or "")).encode("utf-8")).hexdigest()[:32]


class PlanStore:
    def __init__(self, ttl_s: float = DEFAULT_TTL_S, max_plans: int = DEFAULT_MAX_PLANS):
        self.ttl_s = float(ttl_s)
        self.max_plans = int(max_plans)
        self._plans: collections.OrderedDict = collections.OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._plans)

    def issue(self, tool: str, args: dict, caller: str, summary: str = "") -> str:
        plan_id = "plan_" + secrets.token_urlsafe(18)
        with self._lock:
            self._plans[plan_id] = {
                "tool": tool,
                "digest": args_digest(args),
                "args": normalized_args(args),
                "caller": caller,
                "expires": time.monotonic() + self.ttl_s,
                "used": False,
                "summary": str(summary or "")[:500],
            }
            while len(self._plans) > self.max_plans:
                self._plans.popitem(last=False)
        return plan_id

    def _check(self, tool: str, args: dict, caller: str, plan_id, idem=None) -> dict:
        pid = str(plan_id or "").strip() if isinstance(plan_id, str) else ""
        if not pid:
            raise PlanRefused(
                f"spend_refused: {tool} was called with confirm=true but no plan_id. Call it "
                "once WITHOUT confirm (same arguments) to get a plan and its plan_id, tell the "
                "user what it will spend, then call again with confirm=true and that plan_id."
            )
        rec = self._plans.get(pid)
        # A plan another caller made answers exactly like an unknown one (no oracle).
        if rec is None or rec["caller"] != caller:
            raise PlanRefused(
                f"spend_refused: plan_id {pid[:40]!r} is not a plan this server issued to you "
                "(or the server restarted). Plan again without confirm."
            )
        if rec["tool"] != tool:
            raise PlanRefused(
                f"spend_refused: plan_id {pid[:40]!r} is a plan for {rec['tool']}, not {tool}. "
                "Plan this call without confirm."
            )
        # A used plan re-presented with the idempotency_key it was redeemed with is the
        # agent retrying after a lost answer: the API dedups that key, so it is let through.
        replay = bool(idem) and rec.get("idem") == idem
        if rec["used"] and not replay:
            raise PlanRefused(
                f"spend_refused: plan_id {pid[:40]!r} was already used for a confirmed call. "
                "Plan again without confirm if the user wants to run it again."
            )
        if time.monotonic() > rec["expires"]:
            raise PlanRefused(
                f"spend_refused: plan_id {pid[:40]!r} has expired (plans last "
                f"{int(self.ttl_s // 60)} minutes). Plan again without confirm."
            )
        if args_digest(args) != rec["digest"]:
            now = normalized_args(args)
            planned = rec["args"]
            changed = sorted(k for k in set(now) | set(planned) if now.get(k) != planned.get(k))
            raise PlanRefused(
                f"spend_refused: the arguments differ from the plan the user saw "
                f"(changed: {', '.join(changed)}). Nothing was sent. Plan the new arguments "
                "without confirm and ask the user again."
            )
        return rec

    def check(self, tool: str, args: dict, caller: str, plan_id, idem=None) -> str:
        """Validate without consuming (an MRTR round that still waits for the human).
        Returns the plan's one-line summary (what the human is asked to approve)."""
        with self._lock:
            return self._check(tool, args, caller, plan_id, idem)["summary"]

    def redeem(self, tool: str, args: dict, caller: str, plan_id, idem=None) -> None:
        """Validate and consume. After this the plan_id confirms again only as a replay with
        the same idempotency_key (`idem`), which the API answers without spending again."""
        with self._lock:
            rec = self._check(tool, args, caller, plan_id, idem)
            if not rec["used"]:
                rec["used"] = True
                rec["idem"] = idem or None


# --------------------------------------------------------------------------- MRTR
#: The input-request key of the human confirmation (server-assigned, echoed back).
CONFIRM_KEY = "loc_confirm_spend"
HUMAN_CONFIRM_MODES = ("off", "ask", "require")


def human_confirm_mode(raw: str | None) -> str:
    v = str(raw or "").strip().lower()
    return v if v in HUMAN_CONFIRM_MODES else "off"


def elicitation_request(message: str) -> dict:
    """The `elicitation/create` input request (2026-07-28 MRTR, form mode)."""
    return {
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": message,
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "approve": {
                        "type": "boolean",
                        "title": "Approve this spend",
                        "description": "Yes runs it now and bills this account.",
                    }
                },
                "required": ["approve"],
            },
        },
    }


def human_answer(input_responses, plan_id: str, request_state) -> bool | None:
    """True = the human approved, False = declined/cancelled/said no, None = no answer
    for THIS plan in the retried request (ask again)."""
    if not isinstance(input_responses, dict):
        return None
    resp = input_responses.get(CONFIRM_KEY)
    if not isinstance(resp, dict):
        return None
    if str(request_state or "") != plan_id:
        return None
    if resp.get("action") != "accept":
        return False
    content = resp.get("content")
    return bool(isinstance(content, dict) and content.get("approve") is True)
