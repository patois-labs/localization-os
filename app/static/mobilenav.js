/* Mobile nav hamburger (bug #14).
 *
 * Below 900px the sidebar is off-canvas (style.css: .sidebar { transform:
 * translateX(-100%) }); this script toggles `.nav-open` on `.app` to slide it
 * in as an overlay. Degrades sanely without JS: the button just does nothing
 * and the page still renders fine (the sidebar stays off-canvas until the
 * viewport widens past the breakpoint) - no broken markup, nothing throws.
 *
 * Markup contract (rendered by base.html):
 *   <div class="app">
 *     <aside class="sidebar">...</aside>
 *     <div class="nav-backdrop" data-nav-backdrop></div>
 *     <div class="main">
 *       <header class="topbar2">
 *         <button data-nav-toggle aria-expanded="false">...</button>
 */
(function () {
  "use strict";

  var app = document.querySelector(".app");
  var toggle = document.querySelector("[data-nav-toggle]");
  var backdrop = document.querySelector("[data-nav-backdrop]");
  var main = document.querySelector(".main");
  var sidebar = document.querySelector(".sidebar");
  var closeButton = document.querySelector("[data-nav-close]");
  var narrow = window.matchMedia("(max-width: 900px)");
  if (!app || !toggle) { return; }

  function isOpen() { return app.classList.contains("nav-open"); }

  function open() {
    if (!narrow.matches) { return; }
    app.classList.add("nav-open");
    toggle.setAttribute("aria-expanded", "true");
    var first = sidebar && Array.from(sidebar.querySelectorAll(".side-nav a")).find(function (a) {
      return a.getClientRects().length && getComputedStyle(a).visibility !== "hidden";
    });
    if (!first) { first = closeButton; }
    if (first) { first.focus({ preventScroll: true }); }
    if (main) { main.setAttribute("inert", ""); }
  }

  function close(restoreFocus) {
    app.classList.remove("nav-open");
    toggle.setAttribute("aria-expanded", "false");
    if (main) { main.removeAttribute("inert"); }
    if (restoreFocus) { toggle.focus(); }
  }

  toggle.addEventListener("click", function () {
    if (isOpen()) { close(true); } else { open(); }
  });
  if (closeButton) {
    closeButton.addEventListener("click", function () { close(true); });
  }

  // Outside click (the backdrop covers everything but the sidebar itself).
  if (backdrop) {
    backdrop.addEventListener("click", function () { close(true); });
  }

  // Escape closes it, matching the profile-dropdown convention in base.html.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && isOpen()) { e.preventDefault(); close(true); }
    if (e.key === "Tab" && isOpen() && sidebar) {
      var links = Array.from(sidebar.querySelectorAll("a[href], button, [tabindex='0']")).filter(function (el) {
        return !el.disabled && el.getClientRects().length && getComputedStyle(el).visibility !== "hidden";
      });
      var first = links[0], last = links[links.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  narrow.addEventListener("change", function () {
    var inSidebar = sidebar && sidebar.contains(document.activeElement);
    close(narrow.matches && inSidebar);
  });

  // Navigating to a new page via a sidebar link should not leave the overlay
  // open on the next page load (a fresh page load already resets the class,
  // but a same-page anchor click inside the nav should still close it).
  if (sidebar) {
    // Close before document-level links open a dialog in the main area.
    sidebar.addEventListener("click", function (e) {
      if (isOpen() && e.target.closest("a")) { close(true); }
    }, true);
  }
})();
