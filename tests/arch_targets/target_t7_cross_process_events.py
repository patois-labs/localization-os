"""TARGET T7: live progress events reach the browser even when the work runs in another process.

Why (2026-10-22): a multi-process design publishes notifications and progress through Postgres NOTIFY;
every replica keeps one LISTEN connection and fans events out to its SSE subscribers. Our live updates (run progress, SSE streams) are in-process: once work moves to a worker
process (T6) or a second instance, the web process never hears about it. Needed before T6 can ship.

Done when every check passes:
  1. app.services.events exposes publish(channel, payload) and subscribe(channel) (an iterator of payloads).
  2. An event published from ANOTHER process reaches a subscriber in this process within 2 s
     (Postgres: LISTEN/NOTIFY; SQLite: a small events table polled at most every 500 ms).
  3. 200 events published in a burst from another process all arrive, in order, with none duplicated.
"""

import importlib
import os
import subprocess
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests._bootstrap import init_env

DATA = init_env("loctms_t7_")

from _targets import check, finish, need  # noqa: E402

from app.db import init_db  # noqa: E402

init_db()
EV = {"mod": None}
PUBLISHER = r"""
import os, sys, json
sys.path.insert(0, sys.argv[1])
from app.services import events
n = int(sys.argv[2])
for i in range(n):
    events.publish("t7", {"i": i})
"""


def _events():
    try:
        EV["mod"] = importlib.import_module("app.services.events")
    except ModuleNotFoundError:
        raise NotImplementedError("app.services.events") from None
    return EV["mod"]


def t_api():
    m = _events()
    need(m, "publish")
    need(m, "subscribe")
    return True, "publish/subscribe present"


def _collect(n, timeout):
    got = []
    sub = EV["mod"].subscribe("t7")
    done = threading.Event()

    def reader():
        for payload in sub:
            got.append(payload.get("i") if isinstance(payload, dict) else payload)
            if len(got) >= n:
                break
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.3)
    t0 = time.time()
    env = dict(os.environ, LOC_TMS_DATA_DIR=DATA)
    subprocess.run([sys.executable, "-c", PUBLISHER, _ROOT, str(n)], env=env, cwd=_ROOT, timeout=60, check=True)
    done.wait(timeout)
    return got, time.time() - t0


def t_cross_process():
    if EV["mod"] is None:
        raise NotImplementedError("app.services.events (check 1)")
    got, secs = _collect(1, 5)
    return (got == [0] and secs <= 2.0 + 1.5), f"received {got} {secs:.1f}s after the publisher started " \
        "(publisher start-up included; limit 2 s + 1.5 s)"


def t_burst():
    if EV["mod"] is None:
        raise NotImplementedError("app.services.events (check 1)")
    got, secs = _collect(200, 15)
    return got == list(range(200)), f"received {len(got)} of 200 in order={got == sorted(got)} " \
        f"dupes={len(got) - len(set(got))} in {secs:.1f}s"


check("1 events publish/subscribe API", t_api)
check("2 an event crosses processes within 2 s", t_cross_process)
check("3 a 200-event burst arrives complete and in order", t_burst)
finish("T7", "cross-process events")
