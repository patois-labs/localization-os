"""GitHub #502 (followups-b): numeric inputs that used to 500 are a clean refusal instead.

The huge-integer-at-a-SQLite-bind shapes (/tm page, /jobs page, keys cursor, ...) were already
answered by the app-wide OverflowError handler (422). This suite pins the shapes that were NOT:

  parser        ids.parse_finite_number: finite only, never OverflowError, int64-bounded.
  workflows     lqa_threshold inf, tm scalar and max_visits past any real value are a
                WorkflowError on add_step and update_step; setup_author's _coerce_int survives
                "1e999".
  retention     retention_days past the datetime range is refused at the settings door on the
                web, and a value already stored is clamped on read so purge cutoffs never raise.

Final line: "NUMERIC INPUT BOUNDS OK".
"""

import os
import sys
from datetime import UTC, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
for _s in (sys.stdout, sys.stderr):
    try:  # noqa: SIM105
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
from tests._bootstrap import init_env

init_env("loctms_numeric_bounds_")

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import StepType
from app.services import ids, retention, settings
from app.services import workflows as wf_service
from app.services.setup_author_helpers import _coerce_int
from app.services.workflows_common import WorkflowError
from app.services.workflows_part2_leaf import (
    _require_valid_lqa_threshold,
    _require_valid_tm_scalar,
)
from tests._csrf import csrf_for

init_db()
FAILS = []


def check(cond, label):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    except BaseException as e:
        print("   wrong exception:", type(e).__name__, str(e)[:80])
        return False
    return False


# ---- parser ----------------------------------------------------------------------------
check(ids.parse_finite_number("12.5") == 12.5, "parser: a float string parses")
check(ids.parse_finite_number("7", integer=True) == 7, "parser: int string parses")
check(ids.parse_finite_number("7.9", integer=True) == 7, "parser: integer truncates toward zero")
for bad in ("abc", "inf", "-inf", "nan", "1e999", float("inf"), float("nan"), True, None, "²"):
    check(
        raises(ValueError, ids.parse_finite_number, bad, field="f"),
        f"parser: {bad!r} -> ValueError",
    )
check(
    raises(ValueError, ids.parse_finite_number, "1e30", integer=True),
    "parser: integer refuses a value past int64",
)

client = TestClient(app, raise_server_exceptions=False)
client.get("/")
csrf_for(client)

# ---- workflow step overrides -----------------------------------------------------------
check(
    raises(WorkflowError, _require_valid_lqa_threshold, float("inf")), "lqa_threshold inf refused"
)
check(
    raises(WorkflowError, _require_valid_tm_scalar, "9" * 40, "tm fill"),
    "tm scalar 40 digits refused",
)
check(_require_valid_tm_scalar("85", "tm fill") == 85, "tm scalar 85 still accepted")
check(_coerce_int("1e999", 3) == 3, "setup_author _coerce_int survives 1e999")

with Session(engine) as s:
    wf = wf_service.create_workflow(s, "NB flow")
    wid = wf.id
    for bad in (10**30, 1_000_001, float("inf")):
        check(
            raises(
                WorkflowError,
                wf_service.add_step,
                s,
                wid,
                name="x",
                step_type=StepType.LLM_AGENT,
                max_visits=bad,
            ),
            f"add_step: max_visits={bad!r} refused",
        )
    step = wf_service.add_step(s, wid, name="ok", step_type=StepType.LLM_AGENT, max_visits=3)
    check(getattr(step, "max_visits", None) == 3, "add_step: max_visits=3 accepted")
    for bad in (10**30, float("inf")):
        check(
            raises(WorkflowError, wf_service.update_step, s, step.id, max_visits=bad),
            f"update_step: max_visits={bad!r} refused",
        )

# ---- retention -------------------------------------------------------------------------
check(
    raises(settings.InvalidSettingValue, settings.validate_value, "retention_days", "1000000"),
    "retention_days 1e6 refused",
)
check(
    raises(settings.InvalidSettingValue, settings.validate_value, "retention_days", "²"),
    "retention_days unicode digit refused",
)
check(
    not raises(settings.InvalidSettingValue, settings.validate_value, "retention_days", "30"),
    "retention_days 30 accepted",
)
check(
    not raises(settings.InvalidSettingValue, settings.validate_value, "retention_days", ""),
    "retention_days blank accepted",
)
r = client.post(
    "/settings/retention",
    data={"retention_enabled": "0", "retention_days": "1000000"},
    follow_redirects=False,
)
check(r.status_code == 400, f"web retention save: 1000000 days is a 400 (got {r.status_code})")
check(
    settings.get("retention_days") != "1000000", "web retention save: the huge value was not stored"
)
# A value stored before the validator existed (a direct DB write) must not break the cutoff.
from app.models import Setting

with Session(engine) as s:
    row = s.get(Setting, "retention_days")
    if row is None:
        row = Setting(key="retention_days", value="9999999")
    else:
        row.value = "9999999"
    s.add(row)
    s.commit()
settings.invalidate_cache()
check(
    retention.retention_days() == settings.RETENTION_DAYS_MAX, "stored 9999999 reads back clamped"
)
try:
    cutoff = retention._cutoff(datetime.now(UTC).replace(tzinfo=None))
    check(cutoff is not None, "retention cutoff computes for a clamped window")
except OverflowError:
    check(False, "retention cutoff computes for a clamped window")

if FAILS:
    print("FAILED:", len(FAILS))
    sys.exit(1)
print("NUMERIC INPUT BOUNDS OK")
