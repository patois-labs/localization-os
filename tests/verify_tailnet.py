"""Tailnet routing seam verification (offline — no tailscale, no network, no docker).

Covers the OPT-IN Tailscale sidecar egress feature (app/services/tailnet.py + the
provider/engine client seams + the Docker/entrypoint statics):

  * is_tailnet_host() classifier — the 100.64.0.0/10 CGNAT boundary cases, the
    *.ts.net MagicDNS suffix, and the many NOs (public host, LAN IP, loopback, junk).
  * tailnet_client_kwargs() selection — proxy kwargs ONLY for a tailnet URL AND when
    a sidecar proxy is configured; {} for every other combination (this is what keeps
    the default path — cloud providers, connectors, LAN engines — byte-identical).
  * static entrypoint gate — docker-entrypoint.sh starts tailscaled ONLY under a
    TS_AUTHKEY guard, in userspace mode with the SOCKS5 proxy, and CONTINUES booting
    on failure (a grep-style guard like the other static checks in the suite).
  * static Dockerfile — the tailscale binaries are COPYed from a PINNED tag (never
    :latest).
  * requirements sanity — socksio importable + httpx[socks] declared + locked.
  * connector SSRF posture untouched — tailnet.py never imports connectors, no
    connector file references the seam, and base._validate_target still rejects
    private/metadata IPs.

ASCII-only prints (the cp1252 console throws on non-ASCII).
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_tailnet_")

import httpx

from app import config
from app.services import providers, tailnet

_PROXY = "socks5://localhost:1055"


def _classifier() -> None:
    # --- 100.64.0.0/10 CGNAT boundary cases ---
    # Range is 100.64.0.0 .. 100.127.255.255 inclusive.
    assert tailnet.is_tailnet_host("http://100.63.255.255:1234") is False, "just below range"
    assert tailnet.is_tailnet_host("http://100.64.0.0:1234") is True, "range low bound"
    assert tailnet.is_tailnet_host("http://100.64.0.1:1234/v1") is True, "typical tailnet IP"
    assert tailnet.is_tailnet_host("http://100.127.255.255:1234") is True, "range high bound"
    assert tailnet.is_tailnet_host("http://100.128.0.0:1234") is False, "just above range"

    # --- *.ts.net MagicDNS suffix (case-insensitive) ---
    assert tailnet.is_tailnet_host("http://my-pc.tail1234.ts.net:1234") is True, "ts.net host"
    assert tailnet.is_tailnet_host("http://MY-PC.TAILNET.TS.NET/v1") is True, "ts.net uppercased"
    assert tailnet.is_tailnet_host("https://box.ts.net") is True, "https ts.net no port"

    # --- plain LAN / loopback / public IPs and hostnames -> NO ---
    for no in (
        "http://192.168.1.50:1234",  # LAN
        "http://10.0.0.5:1234",  # LAN
        "http://172.16.0.9:1234",  # LAN
        "http://127.0.0.1:1234",  # loopback
        "http://169.254.169.254",  # cloud metadata
        "http://100.63.0.1",  # adjacent-but-outside CGNAT
        "https://api.openai.com",  # public cloud host
        "http://ts.net.evil.com:80",  # ts.net NOT a suffix here
        "http://localhost:1234",  # hostname, not an IP
        "http://lm-studio:1234",  # bare hostname
        "",  # empty
        "not-a-url",  # junk
    ):
        assert tailnet.is_tailnet_host(no) is False, f"should be NON-tailnet: {no!r}"
    print("  is_tailnet_host: 100.64/10 boundaries + .ts.net YES; LAN/loopback/public/junk NO")


def _kwargs_selection() -> None:
    tail = "http://100.64.0.7:1234/v1"
    lan = "http://192.168.1.50:1234/v1"
    cloud = "https://api.anthropic.com"

    # Sidecar PRESENT (proxy configured).
    config.TAILNET_PROXY = _PROXY
    assert tailnet.tailnet_proxy_url() == _PROXY
    assert tailnet.tailnet_client_kwargs(tail) == {"proxy": _PROXY}, "tailnet + sidecar -> proxy"
    assert tailnet.tailnet_client_kwargs(lan) == {}, "LAN engine never proxied"
    assert tailnet.tailnet_client_kwargs(cloud) == {}, "cloud provider never proxied"

    # Sidecar ABSENT (no proxy configured) -> nothing is ever proxied.
    config.TAILNET_PROXY = ""
    assert tailnet.tailnet_proxy_url() is None
    assert tailnet.tailnet_client_kwargs(tail) == {}, "tailnet URL but NO sidecar -> {}"
    assert tailnet.tailnet_client_kwargs(lan) == {}
    assert tailnet.tailnet_client_kwargs(cloud) == {}
    print("  tailnet_client_kwargs: proxy ONLY for (tailnet URL AND sidecar); {} otherwise")


def _cloud_bases_never_proxied() -> None:
    """Regression lock (reviewer): the exact cloud-provider base URLs must get NO
    proxy kwargs even with the sidecar UP — this is the byte-identical guarantee for
    Anthropic/OpenAI/Gemini and must survive any future classifier edit."""
    config.TAILNET_PROXY = _PROXY
    try:
        for cloud in (
            "https://api.anthropic.com",
            "https://api.openai.com",
            "https://generativelanguage.googleapis.com",
        ):
            assert tailnet.is_tailnet_host(cloud) is False, f"{cloud} must NOT classify tailnet"
            assert tailnet.tailnet_client_kwargs(cloud) == {}, (
                f"{cloud} must never be proxied, even with the sidecar up"
            )
    finally:
        config.TAILNET_PROXY = ""
    print("  cloud provider bases (anthropic/openai/gemini) NEVER proxied even with sidecar up")


def _default_path_clients_unproxied() -> None:
    """The load-bearing regression: with a sidecar CONFIGURED, building the local /
    openai-compatible engine client for a NON-tailnet base must NOT attach a proxy —
    only a genuine tailnet base is routed. We assert via the pure seam every client
    builder calls, and prove the seam is actually wired into _local_client by building
    one for a tailnet base without error (socks transport constructible)."""
    config.TAILNET_PROXY = _PROXY
    try:
        # Cloud + LAN bases get no proxy kwargs even with the sidecar up.
        assert providers.LocalProvider is not None  # module import sanity
        for base in ("https://api.openai.com/v1", "http://192.168.1.5:1234/v1"):
            assert tailnet.tailnet_client_kwargs(base) == {}, base
        # A tailnet base + sidecar builds a real OpenAI SDK client (socks wired in).
        client = providers._local_client("http://100.64.0.9:1234", "lm-studio")
        assert client is not None, "tailnet local client should construct"
    finally:
        config.TAILNET_PROXY = ""
    # And a proxied httpx client is constructible at all (socksio present + wired).
    with httpx.Client(proxy=_PROXY) as c:
        assert c is not None
    print("  default-path (cloud/LAN) clients stay UNPROXIED even with sidecar up; socks wired")


def _static_entrypoint_gate() -> None:
    with open(os.path.join(_ROOT, "docker-entrypoint.sh"), encoding="utf-8") as f:
        sh = f.read()
    assert 'if [ -n "${TS_AUTHKEY:-}" ]; then' in sh, "entrypoint must gate on TS_AUTHKEY"
    assert "--tun=userspace-networking" in sh, "must run userspace tailscaled"
    assert "--socks5-server=localhost:1055" in sh, "must expose SOCKS5 on :1055"
    assert "--hostname=loc-studio-fly" in sh, "must set the tailnet hostname"
    assert "--shields-up" in sh, "egress-only sidecar must reject inbound tailnet connections"
    assert sh.index("unset TS_AUTHKEY") < sh.index('exec "$@"'), "app must not inherit join key"
    # 2026-09-29 prod regression: with TS_AUTHKEY unset the app could no longer infer the
    # sidecar, so the entrypoint must name the proxy for the app before exec.
    assert "export LOC_TMS_TAILNET_PROXY=" in sh, "entrypoint must hand the app the proxy URL"
    assert sh.index("export LOC_TMS_TAILNET_PROXY=") < sh.index('exec "$@"')
    assert "tailscaled.state" in sh, "state must persist on the volume"
    # Non-fatal: a failed 'tailscale up' must log + CONTINUE booting (else branch).
    assert "continuing WITHOUT tailnet" in sh, "tailscale-up failure must NOT abort boot"
    # Cold-start race: 'tailscale up' must be retried in a bounded loop (the daemon
    # socket may not exist on the first attempt of a slow boot).
    assert "TS_UP_MAX_TRIES" in sh, "entrypoint must bound the tailscale-up retries"
    assert 'while [ "$ts_try" -le "$TS_UP_MAX_TRIES" ]' in sh, "must retry 'up' in a loop"
    print("  docker-entrypoint.sh: TS_AUTHKEY gate + userspace SOCKS5 + bounded retry + non-fatal")


def _entrypoint_shell():
    for name in ("dash", "busybox", "bash"):
        executable = shutil.which(name)
        if executable and not any(p in executable.lower() for p in ("system32", "windowsapps")):
            return [executable, "sh"] if name == "busybox" else [executable]
    if os.name == "nt":
        return ["C:/Program Files/Git/bin/bash.exe", "--noprofile", "--norc"]
    raise AssertionError("No dash, busybox sh or bash available")


def _entrypoint_egress_only() -> None:
    """Execute the real bootstrap with offline commands on all four exit paths."""
    sh = Path("docker-entrypoint.sh").read_text(encoding="utf-8")
    block = sh.split('if [ -n "${TS_AUTHKEY:-}" ]; then', 1)[1]
    block = 'if [ -n "${TS_AUTHKEY:-}" ]; then' + block
    block = block.split("# Disable uvicorn proxy headers", 1)[0]
    shell = _entrypoint_shell()
    print("  entrypoint shell: " + " ".join(shell))
    for available, expected in (
        ({"dash": "/bin/dash", "bash": "/bin/bash"}, ["/bin/dash"]),
        ({"busybox": "/bin/busybox", "bash": "/bin/bash"}, ["/bin/busybox", "sh"]),
        ({"bash": "/bin/bash"}, ["/bin/bash"]),
    ):
        with patch.object(shutil, "which", side_effect=available.get):
            assert _entrypoint_shell() == expected
    fixture = "offline-join-fixture"  # pregate: allow-secret synthetic join key
    for mode in ("absent", "success", "failure", "up_failure"):
        env = dict(os.environ)
        env["TS_AUTHKEY"] = "" if mode == "absent" else fixture
        env.pop("LOC_TMS_TAILNET_PROXY", None)
        env["MODE"] = mode
        stubs = """
set -eu
mkdir() { :; }
env() {
    [ "$1" = -u ] && [ "$2" = TS_AUTHKEY ] || return 1
    [ "$3" = tailscaled ] || return 1
    echo DAEMON_CLEAN
}
sleep() { :; }
kill() { echo STOPPED; }
tailscale() {
    case " $* " in
        *" --shields-up "*) echo SHIELDED ;;
        *) echo UNSHIELDED; return 1 ;;
    esac
    case " $* " in
        *" set "*) [ "$MODE" != failure ]; return ;;
    esac
    case " $* " in
        *" --auth-key=file:"*)
            [ "${TS_AUTHKEY+x}" != x ] || return 1
            for arg in "$@"; do
                case "$arg" in
                    --auth-key=file:*)
                        key_path=${arg#--auth-key=file:}
                        [ "$(cat "$key_path")" = offline-join-fixture ] || return 1
                        [ "$(stat -c %a "$key_path")" = 600 ] || return 1 ;;
                esac
            done
            echo KEY_FILE ;;
        *) echo KEY_ARGV ;;
    esac
    [ "$MODE" = success ]
}
"""
        # Keep the log redirection in a throwaway harness directory.
        env["LOC_TMS_DATA_DIR"] = os.environ["LOC_TMS_DATA_DIR"]
        Path(env["LOC_TMS_DATA_DIR"], "tailscale").mkdir(exist_ok=True)
        # Bytes, not text: text-mode stdin on Windows writes CRLF, which dash rejects.
        proc = subprocess.run(
            shell,
            input=(
                stubs
                + block
                + (
                    '\n[ "${TS_AUTHKEY+x}" != x ]\n'
                    '[ ! -e "${ts_key_file:-/nonexistent}" ]\necho APP_CLEAN\n'
                    'echo "APP_PROXY=${LOC_TMS_TAILNET_PROXY:-none}"\n'
                )
            ).encode("utf-8"),
            env=env,
            capture_output=True,
            timeout=20,
        )
        result = subprocess.CompletedProcess(
            proc.args,
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "APP_CLEAN" in result.stdout
        assert "UNSHIELDED" not in result.stdout
        assert ("SHIELDED" in result.stdout) == (mode != "absent")
        assert ("STOPPED" in result.stdout) == (mode == "failure")
        assert "KEY_ARGV" not in result.stdout
        # The app learns the proxy exactly when the daemon is left running (joined, or
        # shielded and recovering); never when there is no sidecar or it was stopped.
        want = "socks5://localhost:1055" if mode in ("success", "up_failure") else "none"
        assert f"APP_PROXY={want}" in result.stdout, (mode, result.stdout)
        if mode != "absent":
            # #270 moved the daemon's output from tailscaled.log to stderr (the container log).
            assert "DAEMON_CLEAN" in result.stderr, "daemon inherits join key"


def _static_dockerfile_pinned() -> None:
    with open(os.path.join(_ROOT, "Dockerfile"), encoding="utf-8") as f:
        df = f.read()
    # Issue #236: the stage takes its image from TAILSCALE_BASE (tag, plus digest once pinned
    # by scripts/pin_base_images.py); the shape rules live in that script.
    assert "FROM ${TAILSCALE_BASE} AS tailscale" in df, "tailscale stage must use TAILSCALE_BASE"
    assert re.search(r"(?m)^ARG TAILSCALE_BASE=tailscale/tailscale:v\d", df), (
        "tailscale image must be pinned to a version tag"
    )
    assert "tailscale/tailscale:latest" not in df, ":latest is forbidden (reproducible builds)"
    assert "COPY --from=tailscale /usr/local/bin/tailscaled" in df, "must copy tailscaled"
    assert "COPY --from=tailscale /usr/local/bin/tailscale " in df, "must copy tailscale cli"
    print("  Dockerfile: tailscale binaries COPYed from a PINNED tag (not :latest)")


def _requirements_sanity() -> None:
    import socksio  # noqa: F401

    with open(os.path.join(_ROOT, "requirements.txt"), encoding="utf-8") as f:
        req = f.read()
    assert "httpx[socks]==" in req, "requirements.txt must declare the [socks] extra"
    with open(os.path.join(_ROOT, "requirements.lock"), encoding="utf-8") as f:
        lock = f.read()
    assert "socksio==" in lock, "requirements.lock must pin socksio"
    print("  requirements: socksio importable + httpx[socks] declared + locked")


def _connector_ssrf_untouched() -> None:
    # 1. tailnet.py must NOT import the connector package (scope isolation). Check
    #    import statements specifically (the docstring legitimately names connectors).
    with open(os.path.join(_ROOT, "app", "services", "tailnet.py"), encoding="utf-8") as f:
        tsrc = f.read()
    for line in tsrc.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "connectors" not in stripped, "tailnet seam must not import connectors"

    # 2. No connector module may import the tailnet seam (egress scope is engine-only).
    conn_dir = os.path.join(_ROOT, "app", "services", "connectors")
    for name in os.listdir(conn_dir):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(conn_dir, name), encoding="utf-8") as f:
            body = f.read()
        assert "tailnet" not in body, f"connector {name} must not reference the tailnet seam"

    # 3. The connector SSRF guard still rejects private / metadata IP literals.
    from unittest.mock import patch

    from app.services import egress
    from app.services.connectors.base import ConnectorError, _validate_target

    # This suite intentionally has no settings table. An unresolved name can no
    # longer imply public under a store outage; supply a public DNS answer offline.
    with patch.object(egress, "_RESOLVER", lambda host: ["8.8.8.8"]):
        _validate_target("https://api.airtable.com")  # resolved public host still allowed
    for bad in ("http://127.0.0.1", "http://169.254.169.254", "http://10.0.0.1"):
        try:
            _validate_target(bad)
            raise AssertionError(f"connector SSRF guard should block {bad}")
        except ConnectorError:
            pass
    print("  connector SSRF posture untouched (no tailnet import; base._validate_target intact)")


def main() -> None:
    _classifier()
    _kwargs_selection()
    _cloud_bases_never_proxied()
    _default_path_clients_unproxied()
    _static_entrypoint_gate()
    _entrypoint_egress_only()
    _static_dockerfile_pinned()
    _requirements_sanity()
    _connector_ssrf_untouched()
    # Leave global state clean for any other suite sharing the interpreter.
    config.TAILNET_PROXY = ""
    print("TAILNET OK")


if __name__ == "__main__":
    main()
