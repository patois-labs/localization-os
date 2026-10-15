/* Appearance: Light, Dark or Match my computer (ADR 0063).
 *
 * The choice is stored PER ACCOUNT on the server (User.ui_theme). Every signed-in page renders
 * it into <html data-theme-pref="light|dark|system">, and the head bootstrap in base.html (and
 * the standalone auth pages) applies it before first paint. Choosing here applies at once and
 * POSTs /account/theme (fetch, X-CSRF-Token), so the choice follows the person to every browser.
 *
 * localStorage('loc_theme') is only the fallback for pages with no account in the request
 * (sign-in, invitation): it mirrors the last choice made or loaded in this browser. No stored
 * value means "Match my computer". A "Match my computer" page follows OS changes live.
 *
 * Controls (progressive enhancement, all optional on a page):
 *   [data-theme-set="light|dark|system"]  buttons in the account menu (menuitemradio)
 *   input[name="theme"]                   the radio group on Account settings (a real form
 *                                         posting to /account/theme without JS)
 */
(function () {
  "use strict";

  var root = document.documentElement;
  var query = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

  function valid(t) { return t === "light" || t === "dark" || t === "system"; }

  function remember(choice) {
    try {
      if (choice === "light" || choice === "dark") { localStorage.setItem("loc_theme", choice); }
      else { localStorage.removeItem("loc_theme"); }
    } catch (e) { /* storage blocked: the server copy still holds the choice */ }
  }

  function stored() {
    var pref = root.getAttribute("data-theme-pref");
    if (valid(pref)) { return pref; }
    try {
      var t = localStorage.getItem("loc_theme");
      return t === "light" || t === "dark" ? t : "system";
    } catch (e) {
      return "system";
    }
  }

  function effective(choice) {
    if (choice === "system") { return query && query.matches ? "light" : "dark"; }
    return choice;
  }

  function sync(choice) {
    var buttons = document.querySelectorAll("[data-theme-set]");
    Array.prototype.forEach.call(buttons, function (b) {
      b.setAttribute("aria-checked", b.getAttribute("data-theme-set") === choice ? "true" : "false");
    });
    var radios = document.querySelectorAll('input[name="theme"]');
    Array.prototype.forEach.call(radios, function (r) {
      r.checked = r.value === choice;
      var label = r.closest ? r.closest("label") : null;
      if (label) { label.classList.toggle("on", r.checked); }
    });
  }

  function apply(choice) {
    root.setAttribute("data-theme", effective(choice));
    sync(choice);
  }

  function save(choice) {
    var token = window.CSRF_TOKEN;
    if (!token || !window.fetch) { return; }
    try {
      window.fetch("/account/theme", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "Accept": "application/json",
          "X-CSRF-Token": token
        },
        body: "theme=" + encodeURIComponent(choice) + "&csrf_token=" + encodeURIComponent(token)
      }).catch(function () { /* offline: this page still shows the choice */ });
    } catch (e) { /* no fetch: the choice still applies to this page */ }
  }

  function choose(choice) {
    if (!valid(choice)) { return; }
    root.setAttribute("data-theme-pref", choice);
    remember(choice);
    apply(choice);
    save(choice);
  }

  document.addEventListener("click", function (ev) {
    var target = ev.target && ev.target.closest ? ev.target.closest("[data-theme-set]") : null;
    if (target) { choose(target.getAttribute("data-theme-set")); }
  });
  document.addEventListener("change", function (ev) {
    var t = ev.target;
    if (t && t.name === "theme" && t.type === "radio" && t.form && t.form.id === "appearance-form") {
      choose(t.value);
    }
  });
  if (query) {
    var onOs = function () { if (stored() === "system") { apply("system"); } };
    if (query.addEventListener) { query.addEventListener("change", onOs); }
    else if (query.addListener) { query.addListener(onOs); }
  }

  var initial = stored();
  if (valid(root.getAttribute("data-theme-pref"))) { remember(initial); }
  apply(initial);
  window.LocTheme = { choose: choose, current: stored };
})();
