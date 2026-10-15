// Connector field-mapping pickers - progressive enhancement over the plain text
// inputs that spec.config_fields renders. A "Load from <service>" button asks the
// connector what bases/tables/projects/spaces exist (via the POST browse routes,
// the codebase convention for live-network calls) and drops a <select> next to
// the matching text input. Picking an option writes the value INTO that text
// input, so the form still submits exactly what it did before - the text inputs
// remain the source of truth, and a user can still type a custom value or blank
// one out. Errors render inline (small .error text), never an alert().
//
// The container it enhances declares, via data-* attributes on the wrapping form:
//   data-browse="settings"  cid=<id>   -> saved connection: browse via the cid routes
//   data-browse="add"       kind=<kind> -> add form: browse an UNSAVED throwaway
//                                          connector built from the typed creds
//   data-kind=<connector kind>          -> which cascades to wire (airtable/jira/…)
(function () {
  "use strict";

  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "X-CSRF-Token": window.CSRF_TOKEN },
                        body: body || new URLSearchParams() })
      .then(function (r) {
        // Parse defensively: the browse routes normally return JSON, but when the
        // _ai_block gate trips (session expired mid-page) they return an HTML 403
        // error page. r.json() on that throws - synthesize the {ok,error} contract
        // the callers expect so the UI shows a useful inline message, not a generic
        // "Could not reach the server."
        return r.text().then(function (txt) {
          var d;
          try { d = JSON.parse(txt); }
          catch (e) {
            d = { ok: false, error: r.status === 403
              ? "Not authorized (or your session expired)."
              : "Request failed (" + r.status + ")." };
          }
          return { status: r.status, data: d };
        });
      });
  }

  // The <input name="..."> for a given field name, scoped to one form.
  function inputByName(form, name) {
    return form.querySelector('input[name="' + name + '"]');
  }

  // A small inline error/status line right after an input's <label>.
  function statusLine(input) {
    var label = input.closest("label") || input.parentElement;
    var line = label.querySelector(".browse-status");
    if (!line) {
      line = document.createElement("span");
      line.className = "muted small browse-status";
      label.appendChild(line);
    }
    return line;
  }

  function setError(input, msg) {
    var line = statusLine(input);
    line.className = "small error browse-status";
    line.textContent = msg;
  }

  function setNote(input, msg) {
    var line = statusLine(input);
    line.className = "muted small browse-status";
    line.textContent = msg || "";
  }

  // The leading placeholder <option value="">- … -</option>. Built via the DOM
  // (textContent), never innerHTML - the placeholder is a constant here, but we
  // keep ZERO innerHTML sinks in this file so no future caller can smuggle markup.
  function placeholderOption(placeholder) {
    var opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "- " + placeholder + " -";
    return opt;
  }

  // Insert (once) a <select> immediately after `input`; return it. Choosing an
  // option copies its value into the text input and fires `onpick(value)`.
  function ensureSelect(input, placeholder, onpick) {
    var existing = input.parentElement.querySelector('select.browse-pick[data-for="' + input.name + '"]');
    if (existing) {
      // A second "Load from ..." re-wires the cascade with the NEW payload: the pick
      // handler must follow, or a pick would act on the first load's data (#545).
      existing.locOnpick = onpick;
      return existing;
    }
    var sel = document.createElement("select");
    sel.className = "browse-pick";
    var label = input.labels && input.labels[0];
    var caption = label && Array.prototype.find.call(label.childNodes, function (node) {
      return node.nodeType === 3 && node.textContent.trim();
    });
    sel.setAttribute("aria-label", "Browse " + (caption ? caption.textContent.trim() : placeholder));
    sel.setAttribute("data-for", input.name);
    sel.style.marginTop = "6px";
    sel.appendChild(placeholderOption(placeholder));
    sel.addEventListener("change", function () {
      var v = sel.value;
      if (v) { input.value = v; }
      if (sel.locOnpick) sel.locOnpick(v);
    });
    sel.locOnpick = onpick;
    input.insertAdjacentElement("afterend", sel);
    return sel;
  }

  // Repopulate `sel` from `options` ([{value,label}]). Options are built via the
  // DOM - opt.value / opt.textContent - NEVER innerHTML string concat: the labels
  // (base/table/field/project/space names) come from remote APIs, so a name like
  // `</option><img src=x onerror=…>` would otherwise execute in the admin session.
  // textContent treats every character as text, closing that DOM-XSS sink.
  function fillSelect(sel, options, placeholder) {
    while (sel.firstChild) sel.removeChild(sel.firstChild);
    sel.appendChild(placeholderOption(placeholder));
    options.forEach(function (o) {
      var opt = document.createElement("option");
      opt.value = o.value;
      opt.textContent = (o.label != null ? o.label : o.value);
      sel.appendChild(opt);
    });
  }

  // Body for the unsaved (add-form) browse: gather the typed secret + config
  // inputs from the DOM, mirroring setup_ai's localBody().
  function addFormBody(form) {
    var body = new URLSearchParams();
    form.querySelectorAll("input[name]").forEach(function (el) {
      if (el.type === "password" || el.type === "text") body.set(el.name, el.value);
    });
    return body;
  }

  // ---- Airtable cascade: base -> tables (client-side, same payload) -> fields ----
  function wireAirtable(form, containers) {
    var baseInput = inputByName(form, "base_id");
    var tableInput = inputByName(form, "table");
    var srcInput = inputByName(form, "source_field");
    if (!baseInput) return;

    var byBase = {};
    containers.forEach(function (b) { byBase[b.id] = b; });

    var tableSel = tableInput
      ? ensureSelect(tableInput, "pick a table", function (tableVal) { loadFields(); })
      : null;

    function populateTables(baseId) {
      if (!tableSel) return;
      var b = byBase[baseId];
      var tables = (b && b.tables) || [];
      fillSelect(tableSel, tables.map(function (t) {
        return { value: t.name, label: t.name };
      }), tables.length ? "pick a table" : "no tables (needs schema scope)");
    }

    function loadFields() {
      if (!srcInput) return;
      var base = baseInput.value.trim();
      var table = tableInput ? tableInput.value.trim() : "";
      if (!base || !table) return;
      var cid = form.getAttribute("cid");
      if (!cid) return;   // fields need a saved connection (no unsaved route for this)
      setNote(srcInput, "loading fields…");
      post("/connectors/connection/" + cid + "/browse/fields",
        new URLSearchParams({ base: base, table: table }))
        .then(function (res) {
          if (!res.data.ok) { setError(srcInput, res.data.error || "Could not load fields."); return; }
          var fields = res.data.fields || [];
          var sel = ensureSelect(srcInput, "pick a field", null);
          fillSelect(sel, fields.map(function (f) { return { value: f, label: f }; }),
            fields.length ? "pick a field" : "no fields found");
          setNote(srcInput, fields.length ? "" : "No fields found for that table.");
        })
        .catch(function () { setError(srcInput, "Could not reach the server."); });
    }

    var baseSel = ensureSelect(baseInput, "pick a base", function (baseId) {
      populateTables(baseId);
    });
    fillSelect(baseSel, containers.map(function (b) {
      return { value: b.id, label: b.name + " (" + b.id + ")" };
    }), containers.length ? "pick a base" : "no bases (needs schema.bases:read)");

    // If a base is already typed, prime the table list from the payload.
    if (baseInput.value.trim() && byBase[baseInput.value.trim()]) {
      baseSel.value = baseInput.value.trim();
      populateTables(baseInput.value.trim());
    }
  }

  // ---- Jira: project pick -> insert `project = KEY` into the jql input ----
  function wireJira(form, containers) {
    var jql = inputByName(form, "jql");
    if (!jql) return;
    var sel = ensureSelect(jql, "pick a project", function (key) {
      if (!key) return;
      var clause = "project = " + key;
      var cur = jql.value.trim();
      if (!cur) {
        jql.value = clause;
      } else if (/project\s*=\s*\S+/i.test(cur)) {
        // Replace an existing `project = ...` clause in place.
        jql.value = cur.replace(/project\s*=\s*\S+/i, clause);
      } else {
        // Otherwise prepend the clause, AND-joined with the existing JQL.
        jql.value = clause + " AND " + cur;
      }
    });
    fillSelect(sel, containers.map(function (p) {
      return { value: p.id, label: p.name };   // name already reads "Foo (KEY)"
    }), containers.length ? "pick a project" : "no projects visible");
  }

  // ---- Confluence: space pick -> fill space_key ----
  function wireConfluence(form, containers) {
    var space = inputByName(form, "space_key");
    if (!space) return;
    var sel = ensureSelect(space, "pick a space", null);
    fillSelect(sel, containers.map(function (sp) {
      return { value: sp.id, label: sp.name };   // name already reads "Name (KEY)"
    }), containers.length ? "pick a space" : "no spaces visible");
  }

  function wire(form, kind, containers) {
    if (kind === "airtable") wireAirtable(form, containers);
    else if (kind === "jira") wireJira(form, containers);
    else if (kind === "confluence") wireConfluence(form, containers);
  }

  // The "Load from <service>" button handler.
  function loadContainers(btn) {
    var form = btn.closest("form");
    if (!form) return;
    var kind = form.getAttribute("data-kind");
    var mode = form.getAttribute("data-browse");
    var out = form.querySelector(".browse-out");
    if (out) { out.className = "muted small browse-out"; out.textContent = "loading…"; }
    // Busy indicators: the shared spinner/label + top bar for the live browse call
    // (falls back to a plain disable if busy.js is absent).
    if (window.LocBusy && window.LocBusy.isBusy(btn)) return;
    var busyDone = window.LocBusy ? window.LocBusy.start(btn)
                                  : function () { btn.disabled = false; };
    if (!window.LocBusy) btn.disabled = true;

    var req;
    if (mode === "add") {
      req = post("/connectors/" + kind + "/browse", addFormBody(form));
    } else {
      var cid = form.getAttribute("cid");
      req = post("/connectors/connection/" + cid + "/browse/containers");
    }

    req.then(function (res) {
      busyDone();
      if (!res.data.ok) {
        if (out) { out.className = "small error browse-out"; out.textContent = res.data.error || "Could not load."; }
        return;
      }
      var containers = res.data.containers || [];
      wire(form, kind, containers);
      if (out) {
        if (!containers.length) {
          out.className = "small error browse-out";
          out.textContent = "Nothing found - check the token / credentials and required scopes.";
        } else {
          out.className = "muted small browse-out";
          out.textContent = "Loaded " + containers.length + " item(s). Pick from the dropdowns above.";
        }
      }
    }).catch(function () {
      busyDone();
      if (out) { out.className = "small error browse-out"; out.textContent = "Could not reach the server."; }
    });
  }

  document.querySelectorAll('[data-connector-browse]').forEach(function (el) {
    el.addEventListener('click', function () { loadContainers(el); });
  });
  window.connectorBrowse = { load: loadContainers };
})();
