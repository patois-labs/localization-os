"""Vendor CLI smoke (`vendor …` group, Wave A) — hermetic + offline.

Drives the REAL argparse dispatcher in-process (app.cli.__main__.main) over a throwaway
LOC_TMS_DATA_DIR, exercising the happy path of the new local-only vendor group:
  add -> list -> show -> set-secret -> rate-card set/show -> link-customer.

THE LOAD-BEARING ASSERTION: a vendor's secret VALUE is never printed on any surface
(the connector-credential invariant). We set a known secret and assert the literal value
appears in NO captured stdout — show reports only a `(set)`/has_secret presence flag.

No network, no provider keys (init_env clears them); the CLI opens only the local DB.
ASCII-only prints.
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._bootstrap import init_env

init_env("loctms_vendor_cli_")

from sqlmodel import Session

from app.cli.__main__ import main
from app.db import engine, init_db
from app.models import Customer

init_db()

_SECRET_VALUE = "sup3r-secret-signing-value"


def _run(argv):
    """Run the CLI in-process; return (exit_code, stdout_text, stderr_text)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _run_json(argv):
    """Run with --json (prepended as a root flag) and return the parsed payload."""
    code, out, err = _run(["--json", *argv])
    assert code == 0, f"expected exit 0 for {argv}, got {code}; stderr={err!r}"
    payload = json.loads(out)
    assert payload.get("ok") is True, f"expected ok=True for {argv}, got {payload}"
    return payload, out


def _make_customer(name="Acme Corp"):
    with Session(engine) as s:
        c = Customer(name=name, org_id=None)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


# ---------------------------------------------------------------------------
def test_vendor_cli_happy_path_and_secret_never_printed():
    printed = []  # every stdout chunk, checked for the secret value at the end

    # add ---------------------------------------------------------------------
    payload, out = _run_json(
        [
            "vendor",
            "add",
            "Acme LSP",
            "--description",
            "primary vendor",
            "--endpoint-url",
            "https://vendor.example.com/intake",
        ]
    )
    printed.append(out)
    vid = payload["id"]
    assert payload["name"] == "Acme LSP"
    assert payload["has_secret"] is False
    print("  vendor add -> #%d OK" % vid)  # noqa: UP031

    # list --------------------------------------------------------------------
    payload, out = _run_json(["vendor", "list"])
    printed.append(out)
    ids = [v["id"] for v in payload["vendors"]]
    assert vid in ids, "added vendor missing from list"
    print("  vendor list shows the vendor OK")

    # show (before secret) ----------------------------------------------------
    payload, out = _run_json(["vendor", "show", str(vid)])
    printed.append(out)
    assert payload["has_secret"] is False
    print("  vendor show (unset secret) OK")

    # set-secret --------------------------------------------------------------
    # The KEY=VALUE form warns to stderr (argv exposure) but works headlessly with no
    # tty; the value must appear on NO stdout surface.
    code, out, err = _run(
        ["vendor", "set-secret", str(vid), "--secret", f"signing_secret={_SECRET_VALUE}"]
    )
    printed.append(out)
    assert code == 0, f"set-secret exit {code}; stderr={err!r}"
    assert _SECRET_VALUE not in out, "secret value leaked into set-secret stdout"

    payload, out = _run_json(["vendor", "show", str(vid)])
    printed.append(out)
    assert payload["has_secret"] is True, "secret was not stored"
    print("  vendor set-secret stores it, shows only presence OK")

    # rate-card set + show ----------------------------------------------------
    payload, out = _run_json(
        ["vendor", "rate-card", "set", str(vid), "--band", "new", "--rate", "0.2"]
    )
    printed.append(out)
    assert payload["rate_card"]["bands"]["new"] == 0.2

    payload, out = _run_json(["vendor", "rate-card", "show", str(vid)])
    printed.append(out)
    assert payload["rate_card"]["bands"]["new"] == 0.2
    # merge (not replace): an untouched band is still present at its default.
    assert "fuzzy_high" in payload["rate_card"]["bands"]
    print("  vendor rate-card set/show (merged band) OK")

    # link-customer -----------------------------------------------------------
    cust_id = _make_customer()
    payload, out = _run_json(["vendor", "link-customer", str(vid), str(cust_id)])
    printed.append(out)
    assert payload["linked"] is True
    assert payload["customer_id"] == cust_id
    # the link shows on the vendor detail
    payload, out = _run_json(["vendor", "show", str(vid)])
    printed.append(out)
    assert cust_id in payload["customer_ids"]
    print("  vendor link-customer OK")

    # THE load-bearing assertion: the secret value appears on no captured surface.
    for chunk in printed:
        assert _SECRET_VALUE not in chunk, "secret value leaked into CLI stdout"
    print("  secret value never printed on any surface OK")


def test_vendor_group_refuses_remote():
    # LOCAL-only: a --remote vendor command must fail cleanly (exit 2), never run local.
    code, _out, err = _run(
        ["--remote", "https://example.com", "--token", "lsu_x", "vendor", "list"]
    )
    assert code == 2, f"expected exit 2 for --remote vendor, got {code}"
    assert "remote" in err.lower()
    print("  vendor group refuses --remote OK")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL {name}: {exc}")
    if failures:
        print("verify_vendor_cli FAILED (%d)" % failures)  # noqa: UP031
        raise SystemExit(1)
    print("verify_vendor_cli OK")
