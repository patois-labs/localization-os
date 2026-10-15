"""SecretStore port -- suite for composable-core phase 1, Train 4.

Proves the load-bearing contracts of the port that names the runtime-secret RESOLUTION
seam (see app/ports/secrets.py + the SPEC-ports-phase1 spec Train 4):

  A  STRUCTURAL / interface: the default self-registered adapter is EnvKeyfileSecretStore
     and satisfies the runtime_checkable SecretStore Protocol; a class MISSING resolve()
     does NOT (the load-bearing NEGATIVE control); secrets.get() is the SAME object as the
     raw registry ports.get("secrets") -- there is no wrapper (registry-bypass note).
  B  DEFAULT-ADAPTER EQUIVALENCE vs config._secret across all four resolution branches
     (prefixed env / existing keyfile / non-embedded unprefixed / generate): the adapter
     returns the identical (value, source) config._secret would -- wrap, don't move.
  C  EQUIVALENCE against config's ACTUAL live resolution: re-resolving the two real secrets
     through the DEFAULT adapter reproduces config.SESSION_SECRET / config.ENCRYPTION_KEY
     and their *_SOURCE markers -- proving config resolved them through this port by default.
  D  SEAM (in-process): a registered RECORDING fake adapter is consulted by secrets.get()
     with the exact call args and its return is honored; unregister restores the default.
  E  SEAM (load-bearing, subprocess): a fake override registered BEFORE config's first
     import is picked up by config.SESSION_SECRET / ENCRYPTION_KEY -- AND importing
     app.ports.secrets does NOT import config (the config-import-time inversion holds).
  F  TRAIN-S SEMANTICS survive the port indirection: an existing keyfile out-ranks a
     DIFFERING unprefixed env var (source 'keyfile', keyfile value) through the adapter --
     Train S's ordering is unchanged by the wrap.

Hermetic + offline (throwaway LOC_TMS_DATA_DIR). No DB needed. ASCII-only prints.
Final line: SECRETSTORE PORT OK.
"""

import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_secretstore_")
# init_env blanks the UNprefixed SESSION_SECRET / ENCRYPTION_KEY but deliberately leaves
# the LOC_TMS_-prefixed spellings alone (L6). Blank them too BEFORE config's first import,
# so on a box that exports LOC_TMS_SESSION_SECRET config still resolves to a throwaway
# keyfile -- otherwise test C's source=="keyfile" assertion would go red on that host.
os.environ["LOC_TMS_SESSION_SECRET"] = ""
os.environ["LOC_TMS_ENCRYPTION_KEY"] = ""

from app import config, ports
from app.ports import secrets as secrets_port

_SECRET_ENV_KEYS = (
    "LOC_TMS_SESSION_SECRET",
    "LOC_TMS_ENCRYPTION_KEY",
    "SESSION_SECRET",
    "ENCRYPTION_KEY",
)
_GEN = lambda: "GENVAL"  # noqa: E731


def _blank_secret_env():
    """Set all four secret env spellings to '' (config._get treats '' as unset). Returns
    the previous values so a caller can restore them."""
    prev = {k: os.environ.get(k) for k in _SECRET_ENV_KEYS}
    for k in _SECRET_ENV_KEYS:
        os.environ[k] = ""
    return prev


def _restore_env(prev):
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ============================================================================
# A. structural / interface -- isinstance + NEGATIVE control + registry identity
# ============================================================================
def test_structural_protocol_and_registry_identity():
    assert "secrets" not in ports._OVERRIDES, "no override for the default-adapter test"
    store = secrets_port.get()
    assert isinstance(store, secrets_port.EnvKeyfileSecretStore), (
        "the self-registered default must be EnvKeyfileSecretStore"
    )

    # STRUCTURAL CHECK: the default adapter satisfies the runtime_checkable SecretStore
    # Protocol; the load-bearing NEGATIVE control -- a class with NO resolve() does NOT --
    # is what makes resolve() an ENFORCED member of the interface, not merely prose.
    assert isinstance(store, secrets_port.SecretStore)

    class _NoResolve:
        pass

    class _HasResolve:
        def resolve(self, env_key, keyfile_name, generate, *, alt_env=None):
            return ("x", "env")

    assert not isinstance(_NoResolve(), secrets_port.SecretStore), (
        "a class missing resolve() must FAIL the structural SecretStore check -- resolve() "
        "must be a real named operation, not optional prose"
    )
    assert isinstance(_HasResolve(), secrets_port.SecretStore), (
        "a class WITH resolve() must satisfy the Protocol (positive control)"
    )

    # REGISTRY-BYPASS note (mirrors scheduler errata (b)): secrets.get() returns the raw
    # adapter -- no wrapper -- so it is the SAME object as ports.get("secrets"). The
    # sanctioned path is secrets.get() (which config's resolution seam uses); the raw
    # registry reaches the identical object and is harmless here (no redaction law).
    assert secrets_port.get() is ports.get("secrets"), (
        "secrets.get() must be the same object as the raw registry (no wrapper)"
    )
    print(
        "  (A) structural: default is EnvKeyfileSecretStore satisfying SecretStore; a "
        "resolve-less class does NOT (negative control); get() is ports.get('secrets') OK"
    )


# ============================================================================
# B. default-adapter equivalence vs config._secret across all four branches
# ============================================================================
def test_default_adapter_equals_config_secret_all_branches():
    adapter = secrets_port.EnvKeyfileSecretStore()
    prev = _blank_secret_env()
    tag = os.getpid()
    try:
        # (1) PREFIXED env wins -> ('env'). Read-only branch: same keyfile name is safe.
        os.environ["LOC_TMS_SESSION_SECRET"] = "pfx-val"
        kf = f".b1_{tag}"
        assert (
            adapter.resolve("LOC_TMS_SESSION_SECRET", kf, _GEN, alt_env="SESSION_SECRET")
            == config._secret("LOC_TMS_SESSION_SECRET", kf, _GEN, alt_env="SESSION_SECRET")
            == ("pfx-val", "env")
        ), "prefixed-env branch must match config._secret"
        assert not (config.DATA_DIR / kf).exists(), "env branch must not write a keyfile"
        os.environ["LOC_TMS_SESSION_SECRET"] = ""

        # (2) NON-EMBEDDED unprefixed only -> ('env'), no keyfile written.
        assert config.IS_EMBEDDED is False, "precondition: this process is not embedded"
        os.environ["SESSION_SECRET"] = "unpfx-val"
        kf = f".b2_{tag}"
        assert (
            adapter.resolve("LOC_TMS_SESSION_SECRET", kf, _GEN, alt_env="SESSION_SECRET")
            == config._secret("LOC_TMS_SESSION_SECRET", kf, _GEN, alt_env="SESSION_SECRET")
            == ("unpfx-val", "env")
        ), "non-embedded unprefixed branch must match config._secret"
        assert not (config.DATA_DIR / kf).exists(), (
            "the unprefixed value must NEVER be persisted to a keyfile"
        )
        os.environ["SESSION_SECRET"] = ""

        # (3) EXISTING keyfile, no env -> ('keyfile', the keyfile value). Read-only, so the
        #     same keyfile name for both callers reads the identical persisted value.
        kf = f".b3_{tag}"
        (config.DATA_DIR / kf).write_text("kf-val", encoding="utf-8")
        assert (
            adapter.resolve("LOC_TMS_SESSION_SECRET", kf, _GEN, alt_env="SESSION_SECRET")
            == config._secret("LOC_TMS_SESSION_SECRET", kf, _GEN, alt_env="SESSION_SECRET")
            == ("kf-val", "keyfile")
        ), "existing-keyfile branch must match config._secret"

        # (4) GENERATE (no env, no keyfile) -> ('keyfile', generated). Distinct fresh names
        #     per caller so each generates independently; deterministic _GEN makes them equal.
        kfa, kfb = f".b4a_{tag}", f".b4b_{tag}"
        r_adapter = adapter.resolve("LOC_TMS_SESSION_SECRET", kfa, _GEN, alt_env="SESSION_SECRET")
        r_secret = config._secret("LOC_TMS_SESSION_SECRET", kfb, _GEN, alt_env="SESSION_SECRET")
        assert r_adapter == r_secret == ("GENVAL", "keyfile"), (
            "generate branch must match config._secret"
        )
        assert (config.DATA_DIR / kfa).read_text(encoding="utf-8") == "GENVAL", (
            "the generate branch must persist the generated value (chmod 0o600 inside)"
        )
    finally:
        _restore_env(prev)
    print(
        "  (B) equivalence: EnvKeyfileSecretStore.resolve == config._secret across all "
        "four branches (prefixed / unprefixed / existing-keyfile / generate) OK"
    )


# ============================================================================
# C. equivalence against config's ACTUAL live resolution of the two real secrets
# ============================================================================
def test_adapter_reproduces_configs_live_secrets():
    # This test process booted with a throwaway DATA_DIR and no secret env, so config
    # resolved BOTH secrets to freshly-generated keyfiles (.session_secret / .enc_key)
    # THROUGH this port. Re-resolving through the DEFAULT adapter (env blanked) must
    # reproduce config's actual live constants + their provenance markers.
    assert "secrets" not in ports._OVERRIDES, "default adapter for the live-resolution test"
    assert config.SESSION_SECRET_SOURCE == "keyfile", config.SESSION_SECRET_SOURCE
    assert config.ENCRYPTION_KEY_SOURCE == "keyfile", config.ENCRYPTION_KEY_SOURCE
    adapter = secrets_port.get()
    prev = _blank_secret_env()
    try:
        ss = adapter.resolve(
            "LOC_TMS_SESSION_SECRET", ".session_secret", lambda: "unused", alt_env="SESSION_SECRET"
        )
        ek = adapter.resolve(
            "LOC_TMS_ENCRYPTION_KEY", ".enc_key", lambda: "unused", alt_env="ENCRYPTION_KEY"
        )
    finally:
        _restore_env(prev)
    assert ss == (config.SESSION_SECRET, config.SESSION_SECRET_SOURCE), (ss,)
    assert ek == (config.ENCRYPTION_KEY, config.ENCRYPTION_KEY_SOURCE), (ek,)
    print(
        "  (C) live resolution: the default adapter reproduces config.SESSION_SECRET / "
        "config.ENCRYPTION_KEY and their *_SOURCE markers -- config routed through the "
        "port by default OK"
    )


# ============================================================================
# D. seam -- a recording fake override is consulted with the exact args
# ============================================================================
def test_recording_override_is_consulted():
    calls = []

    class _RecordingSecretStore:
        def resolve(self, env_key, keyfile_name, generate, *, alt_env=None):
            calls.append((env_key, keyfile_name, alt_env))
            return ("RECORDED-VALUE", "env")

    fake = _RecordingSecretStore()
    ports.register("secrets", fake)
    try:
        assert secrets_port.get() is fake, "the override must resolve through secrets.get()"
        out = secrets_port.get().resolve(
            "LOC_TMS_SESSION_SECRET", ".session_secret", _GEN, alt_env="SESSION_SECRET"
        )
        assert out == ("RECORDED-VALUE", "env"), "the override's return must be honored"
        assert calls == [("LOC_TMS_SESSION_SECRET", ".session_secret", "SESSION_SECRET")], (
            "the override must be called with the exact resolve() args"
        )
    finally:
        ports.unregister("secrets")
    assert isinstance(secrets_port.get(), secrets_port.EnvKeyfileSecretStore), (
        "unregister must return resolution to the default EnvKeyfileSecretStore"
    )
    print(
        "  (D) seam: a recording override is consulted with the exact args and its return "
        "honored; unregister restores the default OK"
    )


# ============================================================================
# E. seam load-bearing -- config picks up an override; import inversion holds
# ============================================================================
_CHILD = "\n".join(
    (
        "import sys, json",
        "from app import ports",
        "from app.ports import secrets",
        # The inversion: importing the port module must NOT drag config into sys.modules...
        "assert 'app.config' not in sys.modules, 'port import must not import config'",
        # ...NOR app.db / app.services (L5): config now hard-depends on this port module
        # staying minimal, so pin that importing it pulls neither the DB engine nor any
        # service. A future edit adding such an import to secrets.py fails HERE, loudly.
        "assert 'app.db' not in sys.modules, 'port import must not import app.db'",
        "assert 'app.services' not in sys.modules, 'port import must not import app.services'",
        # Pin the M2 re-entrant IMPORT_FROM form: at config's first import below, the default
        # adapter's resolve() runs `from app import config` while app.config is mid-import and
        # the parent attribute is not yet bound -- it MUST resolve via the sys.modules fallback.
        # Exercise that exact shape here so a 'simplification' to `import app.config` is caught.
        "def _reentrant_from_form():",
        "    from app import config as _c",  # the load-bearing IMPORT_FROM form
        "    return _c",
        "class F:",
        "    def resolve(self, env_key, keyfile_name, generate, *, alt_env=None):",
        "        assert _reentrant_from_form() is not None",
        "        return ('CHILD-' + env_key, 'env')",
        "ports.register('secrets', F())",
        "from app import config",
        "print(json.dumps({",
        "    'ss': config.SESSION_SECRET, 'ek': config.ENCRYPTION_KEY,",
        "    'sss': config.SESSION_SECRET_SOURCE, 'eks': config.ENCRYPTION_KEY_SOURCE}))",
    )
)


def test_config_routes_through_the_port_subprocess():
    env = dict(os.environ)
    for k in _SECRET_ENV_KEYS:
        env[k] = ""
    env["LOC_TMS_DATA_DIR"] = tempfile.mkdtemp(prefix="loctms_secretstore_sub_")
    proc = subprocess.run([sys.executable, "-c", _CHILD], env=env, cwd=_ROOT, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    import json

    r = json.loads(proc.stdout.decode().strip().splitlines()[-1])
    assert r["ss"] == "CHILD-LOC_TMS_SESSION_SECRET", r
    assert r["ek"] == "CHILD-LOC_TMS_ENCRYPTION_KEY", r
    assert r["sss"] == "env" and r["eks"] == "env", r
    print(
        "  (E) seam load-bearing: config.SESSION_SECRET / ENCRYPTION_KEY resolve through "
        "an override registered before config's import; the port import does not pull "
        "config (inversion holds) OK"
    )


# ============================================================================
# F. Train-S ordering survives the port indirection
# ============================================================================
def test_train_s_keyfile_out_ranks_differing_unprefixed_through_port():
    # Train S: an EXISTING keyfile out-ranks a DIFFERING unprefixed env var (the rotation
    # trap) -- source 'keyfile', keyfile value, plus a differ-warning. Prove that ordering
    # is unchanged when reached THROUGH the port's default adapter.
    adapter = secrets_port.get()
    prev = _blank_secret_env()
    tag = os.getpid()
    try:
        kf = f".fkf_{tag}"
        (config.DATA_DIR / kf).write_text("persisted-key", encoding="utf-8")
        os.environ["SESSION_SECRET"] = "different-env-value"  # differs from the keyfile
        val, src = adapter.resolve("LOC_TMS_SESSION_SECRET", kf, _GEN, alt_env="SESSION_SECRET")
        assert (val, src) == ("persisted-key", "keyfile"), (
            "the keyfile must out-rank a differing unprefixed var through the port",
            val,
            src,
        )
    finally:
        _restore_env(prev)
    print(
        "  (F) Train-S ordering: an existing keyfile out-ranks a differing unprefixed var "
        "through the port (source 'keyfile', keyfile value) OK"
    )


def main():
    test_structural_protocol_and_registry_identity()
    test_default_adapter_equals_config_secret_all_branches()
    test_adapter_reproduces_configs_live_secrets()
    test_recording_override_is_consulted()
    test_config_routes_through_the_port_subprocess()
    test_train_s_keyfile_out_ranks_differing_unprefixed_through_port()
    print("SECRETSTORE PORT OK")


if __name__ == "__main__":
    main()
