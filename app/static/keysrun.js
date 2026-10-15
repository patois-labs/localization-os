// Live progress for the runs a key-grid "Translate" just launched (GitHub #47),
// and the surface that makes a FAILED run visible at all (GitHub #44).
//
// THE PROBLEM. Translating a key project fans out one background run per locale and
// redirects to a STATIC grid. Write-back is deliberately end-of-run and all-or-
// nothing (a failed run must leave zero partial targets), so the grid shows nothing
// for minutes and then everything at once -- and if a run FAILS it shows nothing
// forever, indistinguishable from "never started". A 12-minute fr-FR run died at its
// fix step in prod and no surface ever said so.
//
// WHAT THIS DOES. For each run id the server rendered into the strip, tail the
// EXISTING read-only observer /runs/stream?run_id= (the same channel run_view.html
// uses; it starts nothing and enforces its own project-visibility ring) and report
// what it says: running / N of M steps / completed / paused / FAILED with the step
// error.
//
// FOUR RULES IT KEEPS, three of them from review findings.
//  * EVERY server/provider string goes in through textContent, never innerHTML. A
//    step error is provider-supplied text and lands on an authenticated page.
//  * The grid auto-reloads ONCE, and only when every tracked run ended CLEANLY. A
//    reload wipes the strip, so reloading past a FAILED or PAUSED run would erase
//    the one message this widget exists to show (F5: an all-paused fan-out silently
//    reloaded away "waiting on human review", recreating #44 for the pause case).
//    Those get a manual "Reload the grid" link instead. The run ids arrived in a
//    one-shot cookie the grid has already consumed, so the reloaded page renders no
//    strip and cannot loop.
//  * A run that outlives the observer's 15-minute window is NOT abandoned in
//    silence (F6): the stream closes with a non-terminal status, so reconnect once
//    for a second window, and if that also expires say so with a manual link.
//  * At most MAX_LIVE streams are open at a time (F12): each EventSource holds an
//    HTTP connection (a browser allows ~6 per host) and parks a server-side thread
//    for the whole observer window. Rows past the limit wait for a slot.
(function () {
  "use strict";

  // Terminal for the RUN. `paused` closes the STREAM too, but the run is NOT
  // finished -- it is waiting on a person -- which is why it is handled separately
  // everywhere below.
  var TERMINAL = { completed: 1, failed: 1, cancelled: 1 };
  // Concurrent EventSources. One below the ~6-per-host HTTP/1.1 cap so the page
  // always keeps a connection free for navigation and its own fetches.
  var MAX_LIVE = 5;

  function label(state) {
    if (state === "completed") { return "completed"; }
    if (state === "failed") { return "FAILED"; }
    if (state === "paused") { return "paused for review"; }
    if (state === "queued") { return "waiting for a slot"; }
    return state || "running";
  }

  function wire(strip) {
    var streamUrl = strip.getAttribute("data-stream-url") || "/runs/stream";
    var gridUrl = strip.getAttribute("data-grid-url") || window.location.pathname;
    var rows = [].slice.call(strip.querySelectorAll("[data-run-id]"));
    var foot = strip.querySelector("[data-run-foot]");
    if (!rows.length) { return; }

    var tracked = [];
    var pending = [];
    var live = 0;
    var reloaded = false;
    // True once at least one tracked run was seen IN FLIGHT. Without it, a strip
    // whose runs had all already finished server-side (a back-button, a slow POST)
    // would reload a grid that is already current -- a free page load for nothing.
    var sawLive = false;

    function reloadLink() {
      if (!foot) { return; }
      foot.appendChild(document.createTextNode(". "));
      var a = document.createElement("a");
      a.href = gridUrl;
      a.textContent = "Reload the grid";
      foot.appendChild(a);
    }

    function settle() {
      var done = 0;
      var failed = 0;
      var held = 0;
      tracked.forEach(function (t) {
        if (t.settled) { done += 1; } else { sawLive = true; }
        if (t.state === "failed") { failed += 1; }
        // A run the operator still has to DO something about: paused for review, or
        // one whose stream we gave up tailing while it was still going.
        if (t.state === "paused" || t.stalled) { held += 1; }
      });
      if (foot) {
        foot.textContent = done + " of " + tracked.length + " run(s) finished"
          + (failed ? " -- " + failed + " failed" : "")
          + (held ? " -- " + held + " still needs you" : "");
      }
      if (done < tracked.length || reloaded || !sawLive) { return; }
      reloaded = true;
      if (failed || held) {
        // Keep the failure / the "waiting on a human" on screen; offer the refresh
        // instead of taking it.
        reloadLink();
        return;
      }
      // All clean: the values were written at the end of each run, so the static
      // grid behind this strip is stale. One reload, then never again (the run ids
      // came from a cookie the server already cleared).
      window.location.reload();
    }

    function release() {
      live -= 1;
      while (pending.length && live < MAX_LIVE) {
        var next = pending.shift();
        live += 1;
        next();
      }
    }

    rows.forEach(function (row) {
      var runId = row.getAttribute("data-run-id");
      var stateEl = row.querySelector("[data-run-state]");
      var stepsEl = row.querySelector("[data-run-steps]");
      var errEl = row.querySelector("[data-run-error]");
      var t = {
        id: runId,
        state: (stateEl && stateEl.textContent || "").trim().toLowerCase(),
        settled: false,     // nothing more will arrive for this row
        stalled: false,     // still executing, but we stopped being told about it
        seen: {},           // step identities already counted (F11)
        steps: 0,
        total: 0,
        attempt: 0,         // observer windows opened so far (F6 allows two)
        conn: null,
        released: false
      };
      tracked.push(t);
      if (TERMINAL[t.state]) { t.settled = true; }

      function paint() {
        if (stateEl) { stateEl.textContent = label(t.state); }
        row.setAttribute("data-run-status", t.state || "");
        if (stepsEl) {
          stepsEl.textContent = t.total
            ? t.steps + " of " + t.total + " steps"
            : (t.steps ? t.steps + " step(s)" : "");
        }
      }

      function note(message) {
        if (stepsEl) { stepsEl.textContent = message; }
      }

      function fail(message) {
        t.state = "failed";
        t.settled = true;
        if (errEl) {
          // textContent: a step error is provider text and must never be markup.
          errEl.textContent = message || "The run failed. Open the run for details.";
          errEl.removeAttribute("hidden");
        }
        paint();
      }

      // F11: observe_run REPLAYS every recorded step on each (re)connect, so a
      // running total counted with += 1 read "7 of 3" after one reconnect. Count
      // DISTINCT step identities instead -- step_index when the server sends one,
      // else the name -- which is idempotent across any number of replays.
      function countStep(ev) {
        var key = (ev.step_index === undefined || ev.step_index === null)
          ? "n:" + (ev.step_name || "")
          : "i:" + ev.step_index;
        if (t.seen[key]) { return; }
        t.seen[key] = 1;
        t.steps += 1;
      }

      function finish() {
        if (t.released) { return; }
        t.released = true;
        if (t.conn) { t.conn.close(); }
        release();
        settle();
      }

      function onMessage(e) {
        var ev;
        try { ev = JSON.parse(e.data); } catch (err) { return; }
        if (ev.type === "run_started") {
          t.total = ev.steps || 0;
          if (!t.settled) { t.state = "running"; }
          paint();
        } else if (ev.type === "step" || ev.type === "step_skipped") {
          countStep(ev);
          if (!t.settled) { t.state = "running"; }
          paint();
          if (ev.error) { t.lastError = String(ev.error); }
        } else if (ev.type === "paused") {
          t.state = "paused";
          t.settled = true;
          paint();
        } else if (ev.type === "done") {
          t.state = "completed";
          t.settled = true;
          paint();
        } else if (ev.type === "closed") {
          // The verdict rides on 'closed' (orchestrator.observe_run): status, plus
          // the failing step's error for a FAILED run. Falling back to the last
          // step error we saw keeps an older server honest too.
          var status = (ev.status || "").toLowerCase();
          if (status === "failed") {
            fail(ev.error || t.lastError);
          } else if (status === "paused") {
            t.state = "paused";
            t.settled = true;
            paint();
          } else if (TERMINAL[status]) {
            t.state = status;
            t.settled = true;
            paint();
          } else if (t.settled) {
            // A CLOSE THAT CARRIES NO VERDICT, on a row whose verdict we already
            // have from a 'paused' or 'done' event on this same stream. An older
            // server closes bare here, and the naive reading -- "no status, so the
            // observer window must have expired" -- reconnected, burned the second
            // window and relabelled a known-PAUSED run "still running - reload to
            // check". Trust what we were told; a verdict is never downgraded to a
            // guess.
            paint();
          } else {
            // NON-TERMINAL CLOSE (F6): the observer's own window expired while the
            // run is still going. Silence here froze the row on "running" forever.
            // Open one more window; if that expires too, say so and hand over.
            if (t.attempt < 2) {
              if (t.conn) { t.conn.close(); }
              open();
              return;
            }
            t.stalled = true;
            t.settled = true;
            t.state = status || "running";
            paint();
            note("still running - reload to check");
          }
          finish();
        }
      }

      function open() {
        t.attempt += 1;
        t.conn = connectSSE(streamUrl + "?run_id=" + encodeURIComponent(runId), {
          onMessage: onMessage,
          onGiveUp: function () {
            t.stalled = true;
            t.settled = true;
            // paint() BEFORE note(): paint rewrites the steps slot with the counter,
            // so noting first would leave the operator looking at "1 of 3 steps"
            // while the connection is in fact gone.
            paint();
            note("connection lost - reload to see the latest");
            finish();
          }
        });
      }

      paint();
      if (live < MAX_LIVE) {
        live += 1;
        open();
      } else {
        // Beyond the connection budget: hold the row honestly rather than opening a
        // stream the browser would queue invisibly anyway.
        if (!t.settled) { t.state = "queued"; paint(); }
        pending.push(function () {
          if (!t.settled) { t.state = "running"; paint(); }
          open();
        });
      }
    });

    settle();
  }

  function boot() {
    [].slice.call(document.querySelectorAll("[data-keysrun]")).forEach(wire);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
