"""Presence-sensitive form contracts, independent of FastAPI's empty folding.

Documented in: docs/architecture/browser-surface.md
"""

from typing import Annotated, Any

from fastapi import Depends, Form, HTTPException, Request


def supplied_form(name: str, *, title: str | None = None):
    """None is the absent sentinel; a supplied empty string remains an edit.

    Request.form() reuses the route's parsed form cache. This tiny async dependency
    leaves synchronous handlers and their database work in the anyio threadpool.
    Reject uploads where these scalar text fields are expected.
    """

    async def read(
        request: Request,
        declared: Any = Form(
            None,
            alias=name,
            title=title,
            json_schema_extra={"anyOf": [{"type": "string"}, {"type": "null"}]},
        ),
    ) -> str | None:
        # declared retains the text schema and parse ordering; validate raw values
        # here so uploads reach the browser's HTTPException HTML refusal handler.
        # Raw presence also preserves supplied-empty despite FastAPI's default fold.
        del declared  # Pydantic body models disallow a leading-underscore field name.
        value = (await request.form()).get(name, None)
        if value is not None and not isinstance(value, str):
            raise HTTPException(400, f"{name} must be a text form field")
        return value

    return read


# Absent keeps the lane; empty leaves it.
ParallelGroupForm = Annotated[str | None, Depends(supplied_form("parallel_group"))]
# Absent permits ordinary settings; any supplied value refuses this guarded key.
EnvelopeGuardForm = Annotated[
    str | None,
    Depends(supplied_form("prompt_tap_envelopes_enabled", title="Ledger envelopes enabled")),
]
# Absent keeps the protocol; empty resets it to native.
VendorProtocolForm = Annotated[str | None, Depends(supplied_form("protocol"))]
