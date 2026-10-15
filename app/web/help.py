"""ASK + DRAFT web surface (SPEC-ask-draft sections 4-6) + the per-module `/help`
card pages.

THIN ROUTES over `app.services.assist`. Nothing here decides anything: the seam
owns the grounding contract, the engine resolution, the JSON tolerance and the
visibility-scoped inventory; these routes only translate HTTP to it and back.

THE WRITE STORY (do not widen): there is NO write route in this file.

KILL SWITCH: `assist_enabled` off -> every route here that CALLS A MODEL 404s (not
403 -- a switched-off feature should not be enumerable) and every button is hidden
by the templates. The `/help/<module>` card pages deliberately stay up: they are
plain repo-tracked documentation with no model call in them, and `/help` is a
long-standing, deep-linked docs page.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from app import features
from app.kernel import assist, security
from app.web.deps import (
    _error_page,
    _my_org_id,
    feature_block,
    templates,
)

router = APIRouter()

# "Is Ask/Draft available?" is answered app-wide by the single `feature_enabled('assist')`
# Jinja global (app/web/deps.py); the per-module `assist_enabled` alias is gone (Wave 2).
templates.env.globals["assist_starters"] = lambda: list(assist.STARTER_QUESTIONS)


def assist_can_write(request) -> bool:
    """May this caller POST at all?

    THE SAME predicate the app-wide middleware uses to refuse a mutating request
    (app/main.py: `if mutating and not security.can_write(user)` -> 403). Ask is a
    POST, so a VIEWER shown an "Ask about this page" button gets a 403 the moment
    they press it. Offering a control that cannot work is a lie about what the role
    can do, so the panel is hidden for them. The read-only "?" links to
    /help/<module> stay visible to everyone -- they are plain GETs of plain
    documentation with no model call behind them."""
    if not security.auth_required():
        return True
    return security.can_write(security.session_user(request))


templates.env.globals["assist_can_write"] = assist_can_write


def _assist_gate(request):
    """404 (never 403) when the kill switch is off -- a switched-off feature must not be
    enumerable. Delegated to the ONE shared deps.feature_block, whose features.is_enabled
    uses the SAME strict `str(get(...)) == "1"` predicate `assist.enabled()` now uses --
    behaviour-identical for every value (a non-canonical value fails toward OFF at both);
    the call sites below are unmoved (defense in depth)."""
    return feature_block(request, "assist")


# ============================================================================
# /help/<module> -- the rendered capability card
# ============================================================================
@router.get("/help/{module}")
def help_module(module: str, request: Request):
    """One capability card, rendered, plus its `connects` edges as "How this fits
    together". Read-only and login-walled like the rest of the app; no RBAC beyond
    that, because a card contains no data -- only documentation."""
    card = assist.load_card(module)
    # Full-removal doctrine for OWNED cards (plan §3): a card owned by a switched-off
    # feature 404s exactly like a card that never existed -- the card is the feature's
    # manual, so it goes dark with the feature (even for writes_off features, whose
    # data reads stay up but whose docs do not). features.card_is_off is the one
    # authority; the /help index uses filtered all_cards() so it never links here.
    # Disabled and nonexistent cards share the same JSON refusal as the card API.
    if features.card_is_off(module):
        return JSONResponse({"ok": False, "error": features.REMOVED_REFUSAL}, status_code=404)
    if card is None:
        return JSONResponse({"ok": False, "error": features.REMOVED_REFUSAL}, status_code=404)
    edges = []
    for edge in assist.connects_of(card):
        other = assist.load_card(edge["module"])
        # A neighbour whose card does not load -- disabled (card_is_off) OR nonexistent
        # -- is not linkable: skip it so the "How this fits together" list never renders
        # an href to a 404. A disabled card is thereby indistinguishable from a missing
        # one here too (non-enumerable through a neighbour's edge list).
        if other is None:
            continue
        edges.append(
            {
                "module": edge["module"],
                "how": edge["how"],
                "title": other.get("title") or edge["module"],
            }
        )
    return templates.TemplateResponse(
        request,
        "help_card.html",
        {"request": request, "card": card, "edges": edges, "cards": assist.all_cards()},
    )


# ============================================================================
# Ask
# ============================================================================
@router.post("/assist/ask")
def assist_ask(request: Request, module: str = Form(""), question: str = Form("")):
    """One grounded answer, as JSON, for the "Ask about this page" panel.

    JSON rather than a page render because the panel is an in-place answer area; the
    route stays thin either way. The reply carries `grounding` (the honesty footer
    naming which guide answered) and `refused` (the honest-refusal shape) so the
    panel can render a not-covered answer calmly instead of as a failure."""
    blocked = _assist_gate(request)
    if blocked:
        return blocked
    try:
        result = assist.ask(module, question, org_id=_my_org_id(request))
    except assist.AssistDisabled:
        return _error_page(request, 404, "Not found.")
    except assist.AssistError as exc:
        return JSONResponse({"ok": False, "error": exc.message}, status_code=exc.status)
    return JSONResponse({"ok": True, **result})
