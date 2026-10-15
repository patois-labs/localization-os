"""#757 test helper: a confirmed MCP spend call must carry its own plan's plan_id.

A real agent plans first (same arguments, no confirm), shows the user the plan, then
confirms with the plan_id the plan returned. Suites that drive a spend tool through the
MCP dispatcher do the same through these helpers instead of passing a bare confirm=true.
Planner errors propagate: they are what an agent would meet first.
"""

from __future__ import annotations

import asyncio
import json


def _plan_args(arguments: dict) -> dict:
    return {k: v for k, v in dict(arguments or {}).items() if k not in ("confirm", "plan_id")}


async def aplanned(server, name: str, arguments: dict) -> dict:
    """The arguments with confirm=True and the plan_id of a fresh plan (raw seam)."""
    args = _plan_args(arguments)
    plan = json.loads(await server.call_tool_unvalidated(name, args))
    return {**args, "confirm": True, "plan_id": plan["plan_id"]}


async def aconfirmed(server, name: str, arguments: dict):
    """Plan, then run the confirmed call through the raw seam (call_tool_unvalidated)."""
    return await server.call_tool_unvalidated(name, await aplanned(server, name, arguments))


def confirmed(server, name: str, arguments: dict):
    return asyncio.run(aconfirmed(server, name, arguments))


async def aplanned_validated(server, name: str, arguments: dict) -> dict:
    """Like aplanned, through FastMCP's validated path (server.mcp.call_tool)."""
    args = _plan_args(arguments)
    res = await server.mcp.call_tool(name, args)
    text = "".join(getattr(b, "text", "") for b in res.content)
    return {**args, "confirm": True, "plan_id": json.loads(text)["plan_id"]}


def with_plan(server, name: str, arguments: dict) -> dict:
    """For suites that test what a CONFIRMED call does (transport errors, background jobs,
    marking) rather than planning: when `arguments` confirm without a plan_id, issue the
    plan the planning call would have issued (same store, same caller, same argument
    binding) without running the planner, and return the arguments with its plan_id."""
    args = dict(arguments or {})
    if args.get("confirm") is not True or args.get("plan_id"):
        return args
    caller = server.loc_plan_binding.caller_fingerprint(server._active_token())
    args["plan_id"] = server._PLANS.issue(name, _plan_args(args), caller, "test plan")
    return args
