/* Session timeout warning (a11y T9, WCAG 2.2.1 Timing Adjustable).
 *
 * Loaded by base.html ONLY when the login wall is on, a real user is signed in and a
 * session limit is active (the loc-session-idle / loc-session-max meta tags). Markup is
 * _session_timeout.html; strings come from window.I18N (js.session_*).
 *
 * Timing model: the SERVER is the clock. Every decision starts from `remaining_seconds`
 * reported by GET /auth/session (read-only: asking never extends the session) or
 * POST /auth/keepalive, anchored to performance.now() (monotonic, immune to the user
 * changing the wall clock). remaining_seconds is already min(idle, absolute) and `ending`
 * says which limit is nearer.
 *   - More than WARN_S + RECHECK_S left: sleep until (remaining - WARN_S - RECHECK_S),
 *     then ASK THE SERVER AGAIN (another tab, a save or a poller may have moved it).
 *   - Within the window: open the warning <dialog> (role="alertdialog") with a visible
 *     mm:ss countdown that is NOT a live region; a separate polite region speaks only at
 *     60 and 30 seconds. "Stay signed in" (focused first) POSTs /auth/keepalive with the
 *     X-CSRF-Token header. When the ABSOLUTE limit is the one ending there is nothing to
 *     extend: Stay is hidden and "Sign out now" is focused.
 *   - At zero: ask the server once more; if the session is really gone, keep the page's
 *     unsaved text (drafts, below) and switch the dialog to "You have been signed out"
 *     with "Sign in again" -> /auth/login?next=<this path and query>. The signed-out
 *     dialog cannot be dismissed and the page behind it is hidden (body.session-ended),
 *     so what was on screen is not left readable at an unattended machine (#260).
 * Other tabs hear about extensions and sign-outs on BroadcastChannel('loc-session') and
 * re-ask the server rather than trusting the message.
 *
 * Typing keeps the session alive: TRUSTED (event.isTrusted) keydown / pointerdown / input
 * events send a keepalive at most once per KEEPALIVE_MS, only while this tab is visible,
 * never while the warning is showing (extending is then an explicit choice) and never
 * from a timer or a background poller. The server caps it at the absolute limit.
 *
 * Drafts: containers marked data-preserve-draft keep their unsaved text-like values
 * (text inputs, textareas, selects; never password, file or hidden fields, never
 * csrf_token) in sessionStorage (this tab only; it can hold confidential customer text)
 * when the signed-out state is reached, and on unload while the warning shows. On the
 * next load of the same path a notice offers Restore / Discard; nothing is filled in
 * without the person asking. Each draft carries the signed-in user id (loc-session-uid);
 * a draft whose owner is not the current user is dropped unseen, so the next person to
 * sign in at a shared machine never sees it. "Sign out now" is an explicit choice and
 * keeps no draft.
 *
 * Expired fetch calls: a same-origin fetch answered 401 {"error": "session_expired"}
 * switches straight to the signed-out state; a 401 / 403 of any other shape re-asks the
 * server first.
 */
(function () {
  "use strict";

  var WARN_S = 120;
  var RECHECK_S = 30;
  var KEEPALIVE_MS = 5 * 60 * 1000;
  var RETRY_MS = 30 * 1000;
  var DRAFT_PREFIX = "loc-draft:";

  var dlg = document.getElementById("session-dialog");
  if (!dlg || typeof window.fetch !== "function" || typeof dlg.showModal !== "function") {
    return;
  }
  var titleEl = document.getElementById("session-dialog-title");
  var descEl = document.getElementById("session-dialog-desc");
  var countEl = document.getElementById("session-countdown");
  var stayBtn = document.getElementById("session-stay");
  var outLink = document.getElementById("session-sign-out");
  var inLink = document.getElementById("session-sign-in");
  var announcer = document.getElementById("session-announce");
  var dlgAnnouncer = document.getElementById("session-dialog-announce");
  var draftBox = document.getElementById("session-draft");
  // The signed-in user's id (never the email), stamped on every saved draft so a draft left
  // in a shared tab is only ever offered back to the SAME account.
  var ownerMeta = document.querySelector('meta[name="loc-session-uid"]');
  var owner = ownerMeta ? String(ownerMeta.getAttribute("content") || "") : "";
  var leaving = false; // an explicit "Sign out now": keep no draft

  var I = window.I18N || {};
  var nativeFetch = window.fetch.bind(window);

  function tr(key, fallback) {
    var v = I[key];
    return typeof v === "string" && v ? v : fallback;
  }

  // --- state -------------------------------------------------------------------------
  var mode = "watch"; // "watch" | "warning" | "out"
  var deadline = 0; // performance.now() ms at which the session ends
  var ending = "idle"; // which limit ends first: "idle" | "max"
  var checkTimer = 0;
  var tickTimer = 0;
  var lastKeepalive = monotonic(); // loading this page already touched the session
  var inflight = false;
  var spoken = {};
  var returnFocus = null;
  var channel = null;
  try {
    channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("loc-session") : null;
  } catch (e) {
    channel = null;
  }

  function monotonic() {
    return window.performance && performance.now ? performance.now() : Date.now();
  }

  function secondsLeft() {
    return Math.max(0, Math.ceil((deadline - monotonic()) / 1000));
  }

  function clearTimers() {
    if (checkTimer) { clearTimeout(checkTimer); checkTimer = 0; }
    if (tickTimer) { clearInterval(tickTimer); tickTimer = 0; }
  }

  function announce(text) {
    // a11y T11 (WCAG 4.1.3): while the dialog is open (modal) everything outside it is
    // hidden from the accessibility tree, so speak through the region INSIDE it then.
    var region = dlg.open ? dlgAnnouncer : announcer;
    if (!region) { return; }
    if (announcer) { announcer.textContent = ""; }
    if (dlgAnnouncer) { dlgAnnouncer.textContent = ""; }
    setTimeout(function () { region.textContent = text; }, 50);
  }

  function quietDialog() {
    if (dlgAnnouncer) { dlgAnnouncer.textContent = ""; }
  }

  function post(message) {
    if (!channel) { return; }
    try { channel.postMessage(message); } catch (e) { /* closed channel */ }
  }

  // --- server --------------------------------------------------------------------------
  function ask() {
    return nativeFetch("/auth/session", {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Accept": "application/json" }
    }).then(function (r) {
      if (!r.ok) { throw new Error("session HTTP " + r.status); }
      return r.json();
    });
  }

  function keepalive() {
    lastKeepalive = monotonic();
    return nativeFetch("/auth/keepalive", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "X-CSRF-Token": window.CSRF_TOKEN || "", "Accept": "application/json" }  // # pregate: allow-secret CSRF header name, value is the page session variable
    }).then(function (r) {
      if (r.status === 401) { return { signed_in: false }; }
      if (!r.ok) { throw new Error("keepalive HTTP " + r.status); }
      return r.json();
    });
  }

  function check() {
    if (mode === "out" || inflight) { return; }
    inflight = true;
    ask().then(function (s) {
      inflight = false;
      apply(s);
    }, function () {
      inflight = false;
      schedule(RETRY_MS); // a network blip is not a sign-out: ask again shortly
    });
  }

  function schedule(ms) {
    if (checkTimer) { clearTimeout(checkTimer); }
    checkTimer = setTimeout(function () { checkTimer = 0; check(); }, Math.max(1000, ms));
  }

  // Apply a server report ({signed_in, remaining_seconds, ending}).
  function apply(s) {
    if (!s || !s.signed_in) { signedOut(); return; }
    if (typeof s.remaining_seconds !== "number") { closeWarning(false); return; } // no limit
    ending = s.ending === "max" ? "max" : "idle";
    deadline = monotonic() + s.remaining_seconds * 1000;
    var until = s.remaining_seconds - WARN_S - RECHECK_S;
    if (until > 0) {
      closeWarning(false);
      schedule(until * 1000);
    } else if (s.remaining_seconds > WARN_S) {
      // Inside the re-check window but not yet at 2 minutes: ask once more AT 2 minutes.
      closeWarning(false);
      schedule((s.remaining_seconds - WARN_S) * 1000);
    } else if (s.remaining_seconds > 0) {
      openWarning();
    } else {
      atZero();
    }
  }

  // --- the warning dialog ------------------------------------------------------------
  function mmss(total) {
    var m = Math.floor(total / 60);
    var sec = total % 60;
    return m + ":" + (sec < 10 ? "0" : "") + sec;
  }

  function paintCountdown() {
    var left = secondsLeft();
    countEl.textContent = tr("session_time_left", "Time left: {time}").replace("{time}", mmss(left));
    if (left <= 60 && left > 30 && !spoken[60]) {
      spoken[60] = true;
      announce(tr("session_left_60", "1 minute left before you are signed out."));
    } else if (left <= 30 && left > 0 && !spoken[30]) {
      spoken[30] = spoken[60] = true;
      announce(tr("session_left_30", "30 seconds left before you are signed out."));
    }
    if (left <= 0) { atZero(); }
  }

  function openWarning() {
    var max = ending === "max";
    titleEl.textContent = max
      ? tr("session_title_max", "Your session is ending")
      : tr("session_title_idle", "Are you still there?");
    descEl.textContent = max
      ? tr("session_desc_max", "You have reached the maximum session length.")
      : tr("session_desc_idle", "You will be signed out because of inactivity.");
    stayBtn.hidden = max;
    outLink.hidden = false;
    inLink.hidden = true;
    countEl.hidden = false;
    if (mode !== "warning") {
      mode = "warning";
      spoken = {};
      returnFocus = document.activeElement;
      quietDialog(); // nothing stale in a freshly opened dialog
      if (!dlg.open) { dlg.showModal(); }
      dlg.setAttribute("aria-modal", "true");
      (max ? outLink : stayBtn).focus();
    }
    paintCountdown();
    if (!tickTimer) { tickTimer = setInterval(paintCountdown, 1000); }
    if (checkTimer) { clearTimeout(checkTimer); checkTimer = 0; }
  }

  function closeWarning(speak) {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = 0; }
    if (mode !== "warning") { return; }
    mode = "watch";
    if (dlg.open) { dlg.close(); }
    dlg.removeAttribute("aria-modal");
    if (returnFocus && typeof returnFocus.focus === "function" && document.contains(returnFocus)) {
      try { returnFocus.focus(); } catch (e) { /* element gone */ }
    }
    returnFocus = null;
    if (speak) { announce(tr("session_extended", "You are still signed in.")); }
  }

  function atZero() {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = 0; }
    if (mode === "out" || inflight) { return; }
    // One last question: another tab may have extended the session a moment ago.
    inflight = true;
    ask().then(function (s) {
      inflight = false;
      if (s && s.signed_in && s.remaining_seconds > 0) { apply(s); } else { signedOut(); }
    }, function () {
      inflight = false;
      signedOut();
    });
  }

  function signedOut() {
    if (mode === "out") { return; }
    clearTimers();
    saveDrafts();
    var wasOpen = dlg.open;
    mode = "out";
    titleEl.textContent = tr("session_out_title", "You have been signed out");
    descEl.textContent = tr("session_out_desc", "Your session ended.");
    countEl.hidden = true;
    countEl.textContent = "";
    stayBtn.hidden = true;
    outLink.hidden = true;
    inLink.hidden = false;
    inLink.setAttribute(
      "href",
      "/auth/login?next=" + encodeURIComponent(location.pathname + location.search)
    );
    if (!wasOpen) {
      returnFocus = document.activeElement;
      quietDialog(); // nothing stale in a freshly opened dialog
      dlg.showModal();
    } else {
      // a11y T11 (WCAG 4.1.3): the warning was already open, so the title change alone is
      // silent (focus lands on "Sign in again" inside the same dialog). Say it once through
      // the live region inside the open dialog; a newly opened alertdialog announces its
      // own title.
      announce(tr("session_out_title", "You have been signed out"));
    }
    dlg.setAttribute("aria-modal", "true");
    // #260: the timed-out page is not left readable behind the dialog. Every page region
    // (everything in <body> but the dialog) is hidden; drafts were read from it above.
    document.body.classList.add("session-ended");
    inLink.focus();
    post({ type: "signedout" });
  }

  // An explicit sign-out is a choice, not the timed-out state: nothing is kept for later.
  outLink.addEventListener("click", function () { leaving = true; });

  stayBtn.addEventListener("click", function () {
    keepalive().then(function (s) {
      if (s && s.signed_in) {
        closeWarning(true);
        apply(s); // reopens at once if the ABSOLUTE limit is now the one inside the window
        post({ type: "extended" });
      } else {
        signedOut();
      }
    }, function () { check(); });
  });

  // Escape during the WARNING would hide it while the countdown runs on unseen, so it is
  // refused there (the two buttons are the choices). Once SIGNED OUT it is refused too
  // (#260): the session has ended, so the page must not be readable again; "Sign in
  // again" is the way on, and the unsaved text is already kept. A browser may still
  // force a close (a repeated Escape with no user activation cannot be cancelled), so
  // the close handler reopens the dialog while signed out.
  dlg.addEventListener("cancel", function (e) {
    if (mode === "warning" || mode === "out") { e.preventDefault(); }
  });
  dlg.addEventListener("close", function () {
    dlg.removeAttribute("aria-modal");
    if (mode === "out") {
      dlg.showModal();
      dlg.setAttribute("aria-modal", "true");
      inLink.focus();
    }
  });

  if (channel) {
    channel.onmessage = function (e) {
      var type = e && e.data && e.data.type;
      if ((type === "extended" || type === "signedout") && mode !== "out") {
        if (type === "extended") { closeWarning(false); }
        check();
      }
    };
  }

  // --- typing keeps the session alive ---------------------------------------------------
  function onActivity(e) {
    if (!e || e.isTrusted !== true) { return; }
    if (mode !== "watch" || document.visibilityState !== "visible") { return; }
    if (monotonic() - lastKeepalive < KEEPALIVE_MS) { return; }
    keepalive().then(function (s) {
      if (s && s.signed_in) { apply(s); post({ type: "extended" }); } else { check(); }
    }, function () { /* the regular check will find out */ });
  }
  ["keydown", "pointerdown", "input"].forEach(function (type) {
    document.addEventListener(type, onActivity, { capture: true, passive: true });
  });

  document.addEventListener("visibilitychange", function () {
    // Timers are throttled in a hidden tab; re-ask the server when it comes back.
    if (document.visibilityState === "visible" && mode !== "out") { check(); }
  });

  // --- expired fetch calls ---------------------------------------------------------------
  window.fetch = function (input, init) {
    var p = nativeFetch(input, init);
    return p.then(function (r) {
      try {
        var same = !r.url || r.url.indexOf(location.origin + "/") === 0;
        if (same && (r.status === 401 || r.status === 403) && mode !== "out") {
          r.clone().json().then(function (body) {
            if (body && body.error === "session_expired") { signedOut(); } else { check(); }
          }, function () { check(); });
        }
      } catch (e) { /* never break the caller */ }
      return r;
    });
  };

  // --- drafts ----------------------------------------------------------------------------
  function draftKey() {
    return DRAFT_PREFIX + location.pathname + location.search;
  }

  function containers() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-preserve-draft]"));
  }

  function containerKey(box, index) {
    return box.getAttribute("data-preserve-draft") || box.id || "draft-" + index;
  }

  function fieldKey(el) {
    return el.id ? "#" + el.id : el.name ? "@" + el.name : "";
  }

  // Input types never kept in a draft (a password or a file must not land in storage).
  var SKIP_TYPES = { password: 1, file: 1, hidden: 1, submit: 1, button: 1, reset: 1, image: 1, checkbox: 1, radio: 1 };  // # pregate: allow-secret input type names, not credentials

  function draftable(el) {
    var tag = el.tagName.toLowerCase();
    if (el.disabled || el.name === "csrf_token") { return false; }
    if (tag === "textarea" || tag === "select") { return true; }
    return tag === "input" && !SKIP_TYPES[(el.type || "text").toLowerCase()];
  }

  function unsaved(el, box) {
    // An Editor row keeps its own state: only its dirty / save-failed rows are unsaved.
    var row = el.closest("tr");
    if (row && box.contains(row) && box.tagName.toLowerCase() === "table") {
      return row.classList.contains("dirty") || row.classList.contains("save-failed");
    }
    if (el.tagName.toLowerCase() === "select") {
      return Array.prototype.some.call(el.options, function (o) { return o.selected !== o.defaultSelected; });
    }
    return el.value !== el.defaultValue;
  }

  function saveDrafts() {
    if (leaving || !owner) { return; }
    var out = {};
    var any = false;
    containers().forEach(function (box, i) {
      var fields = {};
      Array.prototype.forEach.call(box.querySelectorAll("input, textarea, select"), function (el) {
        var k = fieldKey(el);
        if (k && draftable(el) && unsaved(el, box) && el.value !== "") {
          fields[k] = el.value;
          any = true;
        }
      });
      if (Object.keys(fields).length) { out[containerKey(box, i)] = fields; }
    });
    try {
      if (any) { sessionStorage.setItem(draftKey(), JSON.stringify({ v: 2, owner: owner, forms: out })); }
    } catch (e) { /* storage full or blocked: nothing more we can do */ }
  }

  function offerDrafts() {
    var raw = null;
    try { raw = sessionStorage.getItem(draftKey()); } catch (e) { return; }
    if (!raw || !draftBox) { return; }
    var data;
    try { data = JSON.parse(raw); } catch (e) { data = null; }
    // Another account's draft (or one with no owner) is dropped unseen, never offered.
    if (!data || !data.forms || !owner || data.owner !== owner) { dropDraft(); return; }
    var main = document.getElementById("main-content");
    if (main) { main.insertBefore(draftBox, main.firstChild); }
    draftBox.hidden = false;
    document.getElementById("session-draft-restore").addEventListener("click", function () {
      var first = null;
      containers().forEach(function (box, i) {
        var fields = data.forms[containerKey(box, i)];
        if (!fields) { return; }
        Array.prototype.forEach.call(box.querySelectorAll("input, textarea, select"), function (el) {
          var k = fieldKey(el);
          if (!k || !draftable(el) || !Object.prototype.hasOwnProperty.call(fields, k)) { return; }
          el.value = fields[k];
          // "input" only (never "change"): a page's own dirty tracking listens for it,
          // and no restore may trigger a change handler that submits or navigates.
          el.dispatchEvent(new Event("input", { bubbles: true }));
          if (!first) { first = el; }
        });
      });
      dropDraft();
      draftBox.hidden = true;
      announce(tr("session_draft_restored", "Your unsaved text is back. Check it, then save."));
      if (first) { first.focus(); }
    });
    document.getElementById("session-draft-discard").addEventListener("click", function () {
      dropDraft();
      draftBox.hidden = true;
      if (main) { main.focus(); }
    });
  }

  function dropDraft() {
    try { sessionStorage.removeItem(draftKey()); } catch (e) { /* blocked */ }
  }

  window.addEventListener("beforeunload", function () {
    if (mode === "warning" && !leaving) { saveDrafts(); }
  });

  offerDrafts();
  check();
})();
