// Vendor Studio CAT editor — vanilla JS. Save-per-segment on blur (PATCH per row).
(function () {
  "use strict";

  function project() {
    var t = document.querySelector("[data-project]");
    return t ? t.getAttribute("data-project") : null;
  }

  async function api(method, url, body) {
    var opts = { method: method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    var r = await fetch(url, opts);
    var data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    return { ok: r.ok, status: r.status, data: data };
  }

  function row(el) { return el.closest("tr[data-seg]"); }
  function segId(el) { return row(el).getAttribute("data-seg"); }
  function pid() { return project(); }

  function setStatus(tr, seg) {
    var st = tr.querySelector("[data-status]");
    if (st && seg && seg.status) { st.textContent = seg.status; }
    if (seg && seg.badge) {
      var b = tr.querySelector(".badge");
      if (b) { b.textContent = seg.badge.label; b.className = "badge b-" + seg.badge.cls; }
    }
  }

  // --- per-segment target save on blur ---
  // #521: a blur only saves when the text differs from the last value saved to the server
  // (data-saved), so the blur that follows Ctrl+Enter (focus moves to the next row) can
  // never overwrite the confirm it just made. The server also treats an unchanged PATCH as
  // a no-op (editor.patch_target).
  function markSaved(ta) { ta.setAttribute("data-saved", ta.value); }
  function isDirty(ta) { return ta.getAttribute("data-saved") !== ta.value; }

  function initSaved() {
    var all = document.querySelectorAll("textarea[data-target]");
    for (var i = 0; i < all.length; i++) markSaved(all[i]);
  }
  initSaved();

  function saveTarget(ta) {
    var tr = row(ta);
    var prev = ta.getAttribute("data-saved");
    markSaved(ta);
    // The in-flight save is kept on the textarea so a confirm issued right after a blur
    // (click on Confirm) waits for it instead of racing it to the server.
    var p = api("PATCH", "/projects/" + pid() + "/segments/" + segId(ta),
        { target: ta.value }).then(function (res) {
      if (res.ok) setStatus(tr, res.data); else ta.setAttribute("data-saved", prev);
      return res;
    }, function () {
      ta.setAttribute("data-saved", prev);
      return { ok: false, status: 0, data: null };
    });
    ta._pendingSave = p;
    p.then(function () { if (ta._pendingSave === p) ta._pendingSave = null; });
    return p;
  }

  document.addEventListener("blur", function (e) {
    if (!e.target.matches("textarea[data-target]")) return;
    if (!isDirty(e.target)) return;
    saveTarget(e.target);
  }, true);

  // --- keyboard: Ctrl+Enter = confirm+advance, Ctrl+Insert = copy source ---
  document.addEventListener("keydown", function (e) {
    if (!e.target.matches("textarea[data-target]")) return;
    if (e.ctrlKey && e.key === "Enter") {
      e.preventDefault();
      confirmSeg(e.target, true);
    } else if (e.ctrlKey && (e.key === "Insert")) {
      e.preventDefault();
      copySrc(e.target);
    }
  });

  function confirmSeg(el, advance) {
    var tr = row(el);
    var ta = tr.querySelector("[data-target]");
    // Save pending edits first (only when changed), then confirm, THEN move focus.
    var saved = isDirty(ta) ? saveTarget(ta) : (ta._pendingSave || Promise.resolve({ ok: true }));
    saved.then(function (sv) {
      if (!sv.ok) return;
      api("POST", "/projects/" + pid() + "/segments/" + segId(el) + "/confirm").then(function (res) {
        if (res.ok) { setStatus(tr, res.data); if (advance) focusNext(tr); }
      });
    });
  }

  function copySrc(el) {
    var tr = row(el);
    api("POST", "/projects/" + pid() + "/segments/" + segId(el) + "/copy-source").then(function (res) {
      if (res.ok) { var t = tr.querySelector("[data-target]"); t.value = res.data.target; markSaved(t); setStatus(tr, res.data); }
    });
  }

  function focusNext(tr) {
    var next = tr.nextElementSibling;
    while (next && !next.querySelector("textarea[data-target]")) next = next.nextElementSibling;
    if (next) next.querySelector("textarea[data-target]").focus();
  }

  // --- button delegation ---
  document.addEventListener("click", function (e) {
    var el = e.target;
    if (el.matches("[data-confirm]")) { confirmSeg(el, true); }
    else if (el.matches("[data-copy]")) { copySrc(el); }
    else if (el.matches("[data-approve]")) {
      var tr = row(el);
      api("POST", "/projects/" + pid() + "/segments/" + segId(el) + "/approve")
        .then(function (r) { if (r.ok) setStatus(tr, r.data); });
    }
    else if (el.matches("[data-reject]")) {
      var tag = prompt("Issue tag for the rejection:", "mistranslation");
      if (tag === null) return;
      var tr = row(el);
      api("POST", "/projects/" + pid() + "/segments/" + segId(el) + "/reject", { issue_tag: tag })
        .then(function (r) { if (r.ok) setStatus(tr, r.data); });
    }
    else if (el.matches("[data-comment]")) {
      var body = prompt("Comment:");
      if (!body) return;
      // Wave 3 Train E (§7.5): the share consent bit. The label names BOTH audiences --
      // a vendor consenting to one audience while reaching two is a consent defect.
      var share = confirm("Send with the return — the buyer AND their end client will "
                          + "see this. OK = send it back. Cancel = keep it internal.");
      api("POST", "/projects/" + pid() + "/segments/" + segId(el) + "/comment",
          { body: body, share: share })
        .then(function () { location.reload(); });
    }
    else if (el.matches("[data-reply-query]")) {
      // Reply to a specific buyer thread; a reply always shares (that is the point of a
      // reply) and carries the buyer thread id so it appends to that thread on return.
      var ref = el.getAttribute("data-ref");
      var rbody = prompt("Reply to the buyer's query (this WILL be sent back — the "
                         + "buyer AND their end client will see it):");
      if (!rbody) return;
      api("POST", "/projects/" + pid() + "/segments/" + segId(el) + "/comment",
          { body: rbody, share: true, external_ref: ref })
        .then(function () { location.reload(); });
    }
    else if (el.matches("[data-advice]")) { adviseOne(el); }
    else if (el.matches("[data-advice-job]")) { adviseJob(); }
    else if (el.matches("[data-qa-run]")) { runQA(); }
    else if (el.matches("[data-ignore]")) {
      var reason = prompt("Reason for dismissing this warning:");
      if (!reason) return;
      var tr = row(el);
      api("POST", "/projects/" + pid() + "/qa/ignore", {
        segment_id: parseInt(segId(el), 10), check_key: el.getAttribute("data-check"),
        detail_sig: el.getAttribute("data-detail"), reason: reason
      }).then(function (r) { if (r.ok) location.reload(); else alert((r.data && r.data.detail) || "cannot dismiss"); });
    }
    else if (el.matches("[data-deliver]")) { deliver(el.getAttribute("data-deliver")); }
  });

  function adviseOne(el) {
    var tr = row(el);
    api("POST", "/projects/" + pid() + "/qa/advice", { segment_id: parseInt(segId(el), 10) })
      .then(function (r) {
        if (!r.ok || !r.data.suggestions.length) return;
        showAdvice(tr, r.data.suggestions[0]);
      });
  }

  function adviseJob() {
    api("POST", "/projects/" + pid() + "/qa/advice", {}).then(function (r) {
      if (!r.ok) return;
      r.data.suggestions.forEach(function (s) {
        var tr = document.querySelector('tr[data-seg="' + s.segment_id + '"]');
        if (tr) showAdvice(tr, s);
      });
    });
  }

  function showAdvice(tr, s) {
    var box = document.createElement("div");
    box.className = "advice";
    if (!s.available) { box.textContent = "Advisor: " + s.rationale; }
    else {
      // H1: rationale + suggestion are WIRE-DERIVED (mock mode interpolates package
      // glossary terms; live mode is an LLM). Render as TEXT, never innerHTML.
      var note = document.createElement("div");
      note.className = "muted small";
      note.textContent = s.rationale;
      box.appendChild(note);
      var accept = document.createElement("button");
      accept.textContent = "Accept suggestion";
      accept.onclick = function () {
        api("POST", "/projects/" + pid() + "/segments/" + s.segment_id + "/advice/accept",
            { suggested: s.suggested, rationale: s.rationale }).then(function (r) {
          if (r.ok) { var t = tr.querySelector("[data-target]"); t.value = r.data.target; markSaved(t); setStatus(tr, r.data); box.remove(); }
        });
      };
      var ignore = document.createElement("button");
      ignore.textContent = "Ignore";
      ignore.onclick = function () {
        api("POST", "/projects/" + pid() + "/segments/" + s.segment_id + "/advice/ignore",
            { suggested: s.suggested, rationale: s.rationale });
        box.remove();
      };
      box.appendChild(accept); box.appendChild(ignore);
    }
    tr.querySelector("td.tgt").appendChild(box);
  }

  function runQA() {
    // QA is advisory-only: show the open-finding count, never a PASS/blocked verdict.
    api("GET", "/projects/" + pid() + "/qa").then(function (r) {
      if (r.ok) {
        var g = document.getElementById("gate");
        if (g) {
          var open = r.data.gate.open;
          g.className = "gate " + (open === 0 ? "ok" : "bad");
          g.setAttribute("data-open", open);
          g.textContent = "QA (advisory): " + (open === 0 ? "no open findings" : open + " open finding(s)");
        }
      }
    });
  }

  function deliver(kind) {
    var url = kind === "partial" ? "/deliver-partial" : "/deliver";
    var out = document.getElementById("deliver-result");
    // Advisory, non-blocking confirm hint: findings never block delivery, but warn once if
    // any are open so a click is a deliberate "deliver anyway".
    var g = document.getElementById("gate");
    var open = g ? parseInt(g.getAttribute("data-open") || "0", 10) : 0;
    if (open > 0 && !confirm(open + " open QA finding(s). Deliver anyway?")) return;
    api("POST", "/projects/" + pid() + url, {}).then(function (r) {
      if (out) {
        if (r.ok) out.textContent = "Localization OS: HTTP " + r.data.status_code +
          (r.data.complete ? " (complete)" : " (partial)") + " - status " + r.data.project_status;
        else out.textContent = "Delivery refused: " + ((r.data && r.data.detail) || r.status);
      }
    });
  }
})();
