/* Reflow, keyboard access to scrollers, and measured sticky clearance. */
(function () {
  "use strict";
  var root = document.documentElement;
  var topbar = document.querySelector(".topbar2");
  var toolbar = document.querySelector(".editor-toolbar");
  var managed = new Map();
  var scrollers = new Set();
  function labelFor(el) {
    var cap = el.querySelector("caption");
    if (cap && cap.textContent.trim()) { return cap.textContent.trim(); }
    var box = el.closest("section, .card, .doc-section");
    var heading = box && box.querySelector("h1, h2, h3");
    return (heading && heading.textContent.trim()) ||
      (window.I18N && window.I18N.scroll_region) || "Scrollable content";
  }
  function inScroller(el) {
    for (var p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      if (/^(auto|scroll)$/.test(getComputedStyle(p).overflowX)) { return p; }
    }
    return null;
  }
  function wrapTables(scope) {
    var tables = Array.from(scope.querySelectorAll("table"));
    if (scope.matches && scope.matches("table")) { tables.unshift(scope); }
    tables.forEach(function (table) {
      if (!table.closest("main") || table.parentElement.closest("table")) { return; }
      var wrapper = inScroller(table);
      if (!wrapper) {
        wrapper = document.createElement("div");
        wrapper.className = "tablewrap";
        table.parentNode.insertBefore(wrapper, table);
        wrapper.appendChild(table);
      }
      scrollers.add(wrapper);
    });
  }
  function hasFocusable(el) {
    return Array.from(el.querySelectorAll("a[href], button, input, select, textarea, [tabindex], [contenteditable='true']")).some(function (child) {
      return !child.disabled && child.tabIndex >= 0 && !child.closest("[inert]") &&
        child.getClientRects().length && getComputedStyle(child).visibility !== "hidden";
    });
  }
  function syncScrollers() {
    document.querySelectorAll("main .tablewrap, main pre").forEach(function (el) { scrollers.add(el); });
    document.querySelectorAll("main *").forEach(function (el) {
      if (el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 1 &&
          /^(auto|scroll)$/.test(getComputedStyle(el).overflowX)) { scrollers.add(el); }
    });
    var names = new Set();
    document.querySelectorAll('[role="region"]').forEach(function (el) {
      if (managed.has(el) && !managed.get(el)["aria-label"] && !el.getAttribute("aria-labelledby")) { return; }
      var name = el.getAttribute("aria-label");
      if (name) { names.add(name); }
    });
    scrollers.forEach(function (el) {
      if (!el.isConnected) { scrollers.delete(el); managed.delete(el); return; }
      var scrolls = el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 1;
      if (scrolls && !hasFocusable(el)) {
        if (!managed.has(el)) {
          var old = {};
          ["tabindex", "role", "aria-label"].forEach(function (key) { old[key] = el.getAttribute(key); });
          managed.set(el, old);
        }
        el.setAttribute("tabindex", "0");
        el.setAttribute("role", "region");
        if (!managed.get(el)["aria-label"] && !el.getAttribute("aria-labelledby")) {
          var base = labelFor(el), name = base, index = 1;
          while (names.has(name)) { index += 1; name = base + " (" + index + ")"; }
          names.add(name);
          el.setAttribute("aria-label", name);
        }
      } else if (managed.has(el)) {
        var saved = managed.get(el);
        Object.keys(saved).forEach(function (key) {
          if (saved[key] === null) { el.removeAttribute(key); }
          else { el.setAttribute(key, saved[key]); }
        });
        managed.delete(el);
      }
    });
  }
  function stickyHeight(el) {
    return el && getComputedStyle(el).position === "sticky" ? el.getBoundingClientRect().height : 0;
  }
  function measure() {
    var barHeight = stickyHeight(topbar);
    root.style.setProperty("--topbar-h", barHeight + "px");
    root.style.scrollPaddingTop = (barHeight + stickyHeight(toolbar) + 12) + "px";
  }
  var toc = document.querySelector(".docs-toc-details");
  var wide = window.matchMedia("(min-width: 901px)");
  function syncToc() { if (toc) { toc.open = wide.matches; } }
  syncToc();
  wide.addEventListener("change", syncToc);
  if (toc) { toc.addEventListener("toggle", syncScrollers); }
  window.locA11yEnhance = function (scope) {
    wrapTables(scope || document);
    syncScrollers();
    measure();
  };
  window.locA11yEnhance();
  window.addEventListener("resize", function () { syncScrollers(); measure(); });
  if (window.ResizeObserver) {
    var observer = new ResizeObserver(function () { measure(); syncScrollers(); });
    [topbar, toolbar, document.querySelector("main")].forEach(function (el) { if (el) { observer.observe(el); } });
  }
  document.addEventListener("focusin", function (event) {
    var el = event.target;
    if (!el.closest("main") || el.closest(".editor-toolbar")) { return; }
    // <main> is tabindex=-1, so a click on plain text focuses it, and the browser
    // re-fires focus on it when the window regains focus. It is the whole page, never
    // a control hidden under the bar: scrolling it "into view" jumped to the top.
    if (el.matches("main")) { return; }
    requestAnimationFrame(function () {
      if (document.activeElement !== el) { return; }
      var bottom = 0;
      [topbar, toolbar].forEach(function (bar) {
        if (stickyHeight(bar)) {
          var barRect = bar.getBoundingClientRect();
          var offset = parseFloat(getComputedStyle(bar).top) || 0;
          if (barRect.top <= offset + 1) { bottom = Math.max(bottom, barRect.bottom); }
        }
      });
      var rect = el.getBoundingClientRect();
      // Keyboard focus is already scrolled into view by the browser, so an element wholly
      // above the viewport is a re-fired focus (window switch) and one taller than the
      // visible area cannot be cleared anyway: leave the reader where they are.
      if (rect.bottom <= 0 || rect.height >= window.innerHeight - bottom) { return; }
      if (bottom && rect.top < bottom + 12) { window.scrollBy(0, rect.top - bottom - 12); }
    });
  });
})();
