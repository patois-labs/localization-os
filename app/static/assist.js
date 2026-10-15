// "Ask about this page" panel (SPEC-ask-draft section 4).
//
// The panel posts the question to /assist/ask and renders the answer in place.
//
// THIS IS NOT A PROGRESSIVE ENHANCEMENT. /assist/ask answers with JSON, so without
// JS the panel's submit button navigates to a page of raw JSON -- which is why the
// "read the full guide" link to /help/<module> sits in the panel itself and is the
// no-JS path. (A no-JS HTML render of the answer would be the real fix; it is not
// worth a second answer template for a browser configuration nobody here runs.)
//
// The answer is rendered as MARKDOWN-LITE and TEXT-ONLY: blank-line-separated
// paragraphs, and lines starting with "-"/"*"/"1." as list items. Every string goes
// in through textContent, never innerHTML, so a model reply can never inject markup.
(function () {
  "use strict";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  function isBullet(line) {
    return /^\s*([-*•]|\d+[.)])\s+/.test(line);
  }

  function stripBullet(line) {
    return line.replace(/^\s*([-*•]|\d+[.)])\s+/, "");
  }

  // Markdown-lite: paragraphs and lists only (spec: "paragraphs/lists only").
  function renderAnswer(target, text) {
    target.textContent = "";
    var blocks = String(text || "").split(/\n\s*\n/);
    blocks.forEach(function (block) {
      var lines = block.split("\n").filter(function (l) { return l.trim() !== ""; });
      if (!lines.length) { return; }
      if (lines.every(isBullet)) {
        var ul = el("ul", "assist-list");
        lines.forEach(function (l) { ul.appendChild(el("li", null, stripBullet(l))); });
        target.appendChild(ul);
      } else {
        target.appendChild(el("p", null, lines.join(" ")));
      }
    });
  }

  function wire(panel) {
    var form = panel.querySelector("[data-assist-form]");
    var input = panel.querySelector("[data-assist-question]");
    var out = panel.querySelector("[data-assist-answer]");
    var foot = panel.querySelector("[data-assist-grounding]");
    if (!form || !input || !out) { return; }
    var url = panel.getAttribute("data-assist-url") || "/assist/ask";
    var token = panel.getAttribute("data-csrf") || "";
    var moduleName = panel.getAttribute("data-assist-module") || "";
    var busy = false;

    function ask(question) {
      if (busy || !question.trim()) { return; }
      busy = true;
      panel.classList.add("is-busy");
      // Busy indicators: the shared spinner/label on the Ask button + the top bar
      // (the panel keeps its own dimmed-answer .is-busy look as well).
      var submitBtn = form.querySelector("button[type=submit]");
      var busyDone = window.LocBusy ? window.LocBusy.start(submitBtn) : function () {};
      out.textContent = "Reading the guide...";
      if (foot) { foot.textContent = ""; }
      var body = "module=" + encodeURIComponent(moduleName) +
                 "&question=" + encodeURIComponent(question) +
                 "&csrf_token=" + encodeURIComponent(token);
      fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRF-Token": token
        },
        body: body,
        credentials: "same-origin"
      }).then(function (r) {
        return r.json().catch(function () { return { ok: false, error: "The assistant is unavailable (" + r.status + ")." }; });
      }).then(function (data) {
        if (!data || data.ok !== true) {
          out.textContent = (data && data.error) || "The assistant is unavailable.";
          return;
        }
        renderAnswer(out, data.answer);
        if (foot) {
          var note = data.grounding || "";
          if (data.engine_note) { note = note + " · " + data.engine_note; }
          foot.textContent = note;
        }
      }).catch(function () {
        out.textContent = "The assistant could not be reached.";
      }).then(function () {
        busy = false;
        panel.classList.remove("is-busy");
        busyDone();
      });
    }

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      ask(input.value);
    });

    panel.querySelectorAll("[data-assist-starter]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        input.value = chip.getAttribute("data-assist-starter") || chip.textContent;
        ask(input.value);
      });
    });
  }

  document.querySelectorAll("[data-assist-panel]").forEach(wire);
})();
