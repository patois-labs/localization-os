"""Read-only compatibility check against the candidate product on PYTHONPATH."""

import argparse
import json
import re
from pathlib import Path

from alembic.migration import MigrationContext
from packaging.requirements import Requirement
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app import config
from app.release import BASE_DIR, release_manifest


def check(profile):
    manifest = release_manifest()
    errors = []
    url = make_url(config.DB_URL)
    if config.IS_SQLITE and not Path(url.database or "").is_file():
        raise ValueError("Installed SQLite database missing")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            installed = sorted(MigrationContext.configure(connection).get_current_heads())
        heads = sorted(manifest["alembic_heads"])
        if len(heads) != 1 or installed != heads:
            errors.append("Installed schema does not match candidate Alembic head; migrate a copy")
    finally:
        engine.dispose()
    locks = ["requirements.lock"]
    if profile != "base":
        locks.append(f"requirements-{profile}.lock")
    if (profile == "postgres") != (not config.IS_SQLITE):
        errors.append("Storage backend and requested release-lock profile differ")
    for lock in locks:
        pins = manifest["dependencies"].get(lock)
        if not pins:
            errors.append(f"Missing release lock: {lock}")
        for pin in pins or []:
            if pin["active"] and (
                not pin["installed_version"]
                or not Requirement(pin["requirement"]).specifier.contains(pin["installed_version"])
            ):
                errors.append(f"Installed dependency differs from {lock}: {pin['requirement']}")
    # Read the CANDIDATE's integration matrix, not a stale copy in the host.
    guide = (Path(BASE_DIR) / "docs/embedded.md").read_text(encoding="utf-8")
    matrix = guide.split("## 2. Environment matrix", 1)[1].split("## 3.", 1)[0]
    keys = sorted(set(re.findall(r"`((?:LOC_TMS_|DATABASE_)[A-Z_]+)`", matrix)))
    if not {"LOC_TMS_EMBEDDED", "LOC_TMS_DATA_DIR", "DATABASE_URL"} <= set(keys):
        errors.append("Candidate embedded environment matrix is incomplete")
    return {
        "profile": profile,
        "installed_schema": installed,
        "candidate_heads": heads,
        "environment_keys": keys,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("base", "postgres", "ldap", "saml"), default="base")
    args = parser.parse_args()
    try:
        result = check(args.profile)
    except Exception:
        # Never echo a database URL or connection exception containing credentials.
        print("Upgrade check failed: schema, release inventory or environment matrix unavailable")
        return 1
    print(json.dumps(result, indent=2))
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
