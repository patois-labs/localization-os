#!/bin/sh
# Localization OS TMS container entrypoint (Phase 4-C).
#
# POSIX sh (no bashisms) so it runs under the slim image's /bin/sh (dash).
#
# Responsibilities:
#   1. On Postgres (DATABASE_URL set and pointing at postgres/postgresql),
#      apply schema migrations with `alembic upgrade head` before the app boots.
#      On Postgres, Alembic OWNS the schema (see docs/DEPLOY.md / app/db.py).
#   2. On SQLite (the zero-config default, DATABASE_URL unset), do nothing -
#      app.main:startup calls init_db() which create_all()s + runs the additive
#      SQLite migrations itself.
#   3. exec the passed command (uvicorn) so it becomes PID 1 and receives signals
#      cleanly (graceful shutdown -> the app's shutdown hook runs).
set -e

# Case-insensitive-ish check for a Postgres DSN. Matches both
# postgresql+psycopg://... and postgres://...
case "${DATABASE_URL:-}" in
    postgres*://* | postgresql*://*)
        echo "[entrypoint] Postgres detected -> running 'alembic upgrade head'"
        alembic upgrade head
        echo "[entrypoint] migrations applied"
        ;;
    "")
        echo "[entrypoint] No DATABASE_URL -> SQLite mode (init_db handles schema)"
        ;;
    *)
        echo "[entrypoint] DATABASE_URL set to a non-Postgres URL -> skipping alembic"
        ;;
esac

# Optional headless first-admin bootstrap (default OFF; see docs/CLI.md + DEPLOY.md).
# Fires ONLY when BOTH LOC_TMS_ADMIN_EMAIL and LOC_TMS_ADMIN_PASSWORD are set; the
# `init` command is idempotent (no-ops once an account exists), so it is safe to
# leave enabled across restarts. Uncomment to enable:
#
# if [ -n "${LOC_TMS_ADMIN_EMAIL:-}" ] && [ -n "${LOC_TMS_ADMIN_PASSWORD:-}" ]; then
#     echo "[entrypoint] seeding first admin from env (init --from-env)"
#     python -m app.cli init --from-env
# fi

# --- OPT-IN Tailscale sidecar (private egress to a home LM Studio server) -------
# Starts ONLY when TS_AUTHKEY is set and non-empty. NO TS_AUTHKEY -> not a single
# new process spawns and behavior is byte-identical to a container without this
# block. When it IS set we bring up a USERSPACE tailscaled (no TUN device / no
# NET_ADMIN needed) exposing a SOCKS5 proxy on localhost:1055; the app routes only
# tailnet-bound ENGINE calls through it (app/services/tailnet.py). State + socket
# live on the /data volume so the node persists across restarts (an ephemeral node
# re-joins with the key on next boot). Failure is NON-FATAL: LM Studio being
# unreachable must NEVER take down cloud translations, so we log and keep booting.
if [ -n "${TS_AUTHKEY:-}" ]; then
    echo "[entrypoint] TS_AUTHKEY set -> starting userspace tailscaled (SOCKS5 :1055)"
    TS_STATE_DIR="${LOC_TMS_DATA_DIR:-/data}/tailscale"
    mkdir -p "$TS_STATE_DIR" || echo "[entrypoint] WARN: could not create $TS_STATE_DIR"
    TS_SOCK="$TS_STATE_DIR/tailscaled.sock"
    env -u TS_AUTHKEY tailscaled \
        --state="$TS_STATE_DIR/tailscaled.state" \
        --socket="$TS_SOCK" \
        --tun=userspace-networking \
        --socks5-server=localhost:1055 \
        >&2 &
    ts_daemon_pid=$!
    # Keep the join key out of argv and child environments. Remove the private file
    # after the bounded bootstrap, including interrupted startup.
    ts_key_file=$(umask 077; mktemp) || ts_key_file=""
    trap 'rm -f "$ts_key_file"' EXIT
    trap 'exit 1' HUP INT TERM
    if [ -n "$ts_key_file" ]; then
        printf '%s' "$TS_AUTHKEY" > "$ts_key_file"
    fi
    unset TS_AUTHKEY
    # Bring the node up. --hostname makes it identifiable in the admin console; the
    # key is only needed for the first join / an ephemeral re-join.
    #
    # COLD-START RACE: on a slow boot, tailscaled's socket may not exist yet when the
    # first `up` runs, so a single attempt can silently fail (and stay failed until a
    # redeploy). Retry up to TS_UP_MAX_TRIES with a 1s pause between attempts. Still
    # NON-FATAL on final failure: log and continue to exec so cloud translations are
    # never blocked by an unreachable home box. The retry lives inside this if/else so
    # `set -e` never aborts on a losing attempt (the loop condition swallows it).
    TS_UP_MAX_TRIES=15
    ts_up_ok=0
    ts_shielded=0
    ts_try=1
    while [ "$ts_try" -le "$TS_UP_MAX_TRIES" ]; do
        if tailscale --socket="$TS_SOCK" set --shields-up; then
            ts_shielded=1
        fi
        if [ -n "$ts_key_file" ] && tailscale --socket="$TS_SOCK" up \
                --auth-key="file:$ts_key_file" \
                --shields-up \
                --hostname=loc-studio-fly; then
            echo "[entrypoint] tailscale up OK (hostname=loc-studio-fly, attempt $ts_try)"
            ts_up_ok=1
            ts_shielded=1
            break
        fi
        echo "[entrypoint] 'tailscale up' attempt $ts_try/$TS_UP_MAX_TRIES failed" \
             "(daemon may still be starting) -> retrying in 1s"
        ts_try=$((ts_try + 1))
        sleep 1
    done
    rm -f "$ts_key_file"
    trap - EXIT HUP INT TERM
    if [ "$ts_shielded" -ne 1 ]; then
        # Persisted daemon state may predate shields-up. Do not leave it serving.
        kill "$ts_daemon_pid" 2>/dev/null || true
        wait "$ts_daemon_pid" 2>/dev/null || true
        echo "[entrypoint] WARN: 'tailscale up' failed after $TS_UP_MAX_TRIES tries ->" \
             "continuing WITHOUT tailnet (cloud providers unaffected; local/tailnet" \
             "engine will be unreachable)"
    elif [ "$ts_up_ok" -ne 1 ]; then
        echo "[entrypoint] WARN: 'tailscale up' failed; shielded daemon left running to recover"
    fi
    # The app used to infer the sidecar from TS_AUTHKEY, which is unset below so the join
    # key never reaches it; that silently sent every tailnet engine call direct (connect
    # timeout). Name the proxy explicitly whenever the daemon is left running. An operator
    # value (Fly secret) wins.
    if [ "$ts_shielded" -eq 1 ]; then
        export LOC_TMS_TAILNET_PROXY="${LOC_TMS_TAILNET_PROXY:-socks5://localhost:1055}"
        echo "[entrypoint] tailnet engine calls routed through the sidecar proxy"
    fi
fi

# The join key is needed only during bootstrap, never by the application.
unset TS_AUTHKEY

# --- OPT-IN web + N workers in one container (engine redesign S3b) --------------
# LOC_TMS_WORKER_PROCESSES unset or 0 (the default): none of this runs and the container is
# one exec'd command process exactly as before. N > 0: this shell stays PID 1 as a small
# supervisor. It starts N `python -m app.worker` processes (LOC_TMS_ROLE=worker) and the web
# command (LOC_TMS_ROLE=web), then:
#   * SIGTERM/SIGINT: TERM to every child (each drains jobs for LOC_TMS_DRAIN_SECONDS), wait
#     up to LOC_TMS_STOP_WAIT_SECONDS (default drain + 10), KILL stragglers, exit 0.
#   * a child dies by itself: stop the others the same way and exit nonzero with that
#     child's status. No in-shell restart: the container restart policy relaunches the whole
#     group and boot recovery reclaims expired leases. Simple beats clever here.
# Needs the durable queue (job_queue_backend=db, the default). See
# docs/guides/postgres-migration.md.
_alive() {
    kill -0 "$1" 2>/dev/null || return 1
    # A finished child stays a zombie until waited for, and kill -0 still succeeds on it.
    if [ -r "/proc/$1/stat" ]; then
        _st=$(sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | cut -c1)
        [ "$_st" = "Z" ] && return 1
    fi
    return 0
}

run_web_and_workers() {
    case "$LOC_TMS_WORKER_PROCESSES" in
        *[!0-9]*|"")
            echo "[entrypoint] LOC_TMS_WORKER_PROCESSES must be a whole number, got '$LOC_TMS_WORKER_PROCESSES'" >&2
            exit 2 ;;
    esac
    if [ "$LOC_TMS_WORKER_PROCESSES" -gt 64 ]; then
        echo "[entrypoint] LOC_TMS_WORKER_PROCESSES is capped at 64" >&2
        exit 2
    fi
    # S3c (ADR 0097), checked before any worker starts: LOC_TMS_WEB_PROCESSES=N (default 1)
    # serves HTTP from N uvicorn worker processes (uvicorn --workers N, one listening socket).
    # The web role only: the background work stays in the worker processes started below.
    web_procs="${LOC_TMS_WEB_PROCESSES:-1}"
    case "$web_procs" in
        *[!0-9]*|""|0)
            echo "[entrypoint] LOC_TMS_WEB_PROCESSES must be a whole number of at least 1, got '$web_procs'" >&2
            exit 2 ;;
    esac
    if [ "$web_procs" -gt 32 ]; then
        echo "[entrypoint] LOC_TMS_WEB_PROCESSES is capped at 32" >&2
        exit 2
    fi
    if [ "$web_procs" -gt 1 ]; then
        set -- "$@" --workers "$web_procs"
    fi
    stop_wait="${LOC_TMS_STOP_WAIT_SECONDS:-$(( ${LOC_TMS_DRAIN_SECONDS:-8} + 10 ))}"
    sup_pids=""
    sup_stopping=0
    trap 'sup_stopping=1' TERM INT
    i=1
    while [ "$i" -le "$LOC_TMS_WORKER_PROCESSES" ]; do
        LOC_TMS_ROLE=worker ${LOC_TMS_WORKER_CMD:-python -m app.worker} &
        sup_pids="$sup_pids $!"
        echo "[entrypoint] worker $i/$LOC_TMS_WORKER_PROCESSES started (pid $!)"
        i=$((i + 1))
    done
    LOC_TMS_ROLE=web "$@" &
    sup_web_pid=$!
    sup_pids="$sup_pids $sup_web_pid"
    echo "[entrypoint] web started (pid $sup_web_pid) with $LOC_TMS_WORKER_PROCESSES worker process(es)"
    sup_dead=""
    while [ "$sup_stopping" -eq 0 ] && [ -z "$sup_dead" ]; do
        for p in $sup_pids; do
            if ! _alive "$p"; then sup_dead=$p; break; fi
        done
        [ -z "$sup_dead" ] && sleep 1
    done
    sup_rc=0
    if [ -n "$sup_dead" ]; then
        wait "$sup_dead" 2>/dev/null || sup_rc=$?
        [ "$sup_rc" -eq 0 ] && sup_rc=1
        echo "[entrypoint] child $sup_dead exited (status $sup_rc) -> stopping the group" >&2
    else
        echo "[entrypoint] stop requested -> signalling children"
    fi
    trap '' TERM INT
    for p in $sup_pids; do kill -TERM "$p" 2>/dev/null || true; done
    waited=0
    while [ "$waited" -lt "$stop_wait" ]; do
        any=0
        for p in $sup_pids; do _alive "$p" && any=1; done
        [ "$any" -eq 0 ] && break
        sleep 1
        waited=$((waited + 1))
    done
    for p in $sup_pids; do
        if _alive "$p"; then
            echo "[entrypoint] pid $p still running after ${stop_wait}s -> KILL" >&2
            kill -KILL "$p" 2>/dev/null || true
        fi
        wait "$p" 2>/dev/null || true
    done
    exit "$sup_rc"
}

# Disable uvicorn proxy headers for default and custom uvicorn commands.
# The application boundary owns forwarding via trusted_proxy_ips alone.
# The flag is added once: the Dockerfile CMD already carries it, so it is not repeated.
case " $* " in
    *" --no-proxy-headers "*) _no_proxy="" ;;
    *) _no_proxy="--no-proxy-headers" ;;
esac
case "${1:-}" in
    uvicorn|*/uvicorn) set -- "$@" $_no_proxy --no-access-log --limit-concurrency 256 ;;
    python|python3|*/python|*/python3)
        if [ "${2:-}" = "-m" ] && [ "${3:-}" = "uvicorn" ]; then
            set -- "$@" $_no_proxy --no-access-log --limit-concurrency 256
        fi ;;
esac

if [ "${LOC_TMS_WORKER_PROCESSES:-0}" != "0" ]; then
    run_web_and_workers "$@"
fi

# Several web processes need the split roles (ADR 0097): refuse instead of starting N copies
# of the single `all` process (each would run the job pools and the scheduler).
if [ "${LOC_TMS_WEB_PROCESSES:-1}" != "1" ]; then
    echo "[entrypoint] LOC_TMS_WEB_PROCESSES=${LOC_TMS_WEB_PROCESSES} needs LOC_TMS_WORKER_PROCESSES of at least 1" >&2
    exit 2
fi

echo "[entrypoint] starting: $*"
exec "$@"
