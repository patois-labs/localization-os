"""One list of every install lock in the repo (GitHub #558).

`hash_locks.py` (hash coverage) and `audit_runtime_profiles.py` (advisory audit) both read
this list, so a lock added under `scripts/locks/` cannot be hashed but never audited, or the
reverse. Pure path logic: nothing here installs, resolves or reads package data.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_LOCK = "tools/vendor_studio/requirements.lock"
AUX_LOCK_DIR = "scripts/locks"
DEV_LOCK = "requirements-dev.lock"  # audited by its own advisory-only pass (GitHub #412)


def install_lock_paths(root=ROOT):
    """Root locks (dev lock included), the Vendor Studio lock and every scripts/locks lock."""
    root = Path(root)
    return [
        *sorted(root.glob("requirements*.lock")),
        root / VENDOR_LOCK,
        *sorted((root / AUX_LOCK_DIR).glob("*.lock")),
    ]


def aux_lock_paths(root=ROOT):
    """The auxiliary install inputs (build tools, CI check tools, the SAML lxml fallback)."""
    return [p for p in install_lock_paths(root) if p.parent == Path(root) / AUX_LOCK_DIR]
