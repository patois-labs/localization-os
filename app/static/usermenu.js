/* Profile (usermenu) dropdown, following the WAI-ARIA APG menu button pattern:
 * focus into the menu on open, outside-click / Escape / Tab-out to close,
 * aria-expanded sync, and keyboard navigation once open (Wave 3D; NVDA fix).
 *
 * Progressive enhancement: the <details>/<summary> element already opens and
 * closes with the mouse and with Enter/Space on the keyboard via native
 * browser behavior, with zero JS. Everything below only ADDS to that:
 *   - whenever the menu opens, by ANY path (mouse click, Enter/Space, a screen
 *     reader's synthetic click, a scripted element.click() or open = true),
 *     focus moves to the first menu item, so a screen reader announces the
 *     menu and its items at once (NVDA browse mode turns Enter on the summary
 *     into a click with no keydown, so an "opened by keyboard" test cannot be
 *     the gate). A pointer-driven open asks for no visible focus ring
 *     (focus({focusVisible: false}); browsers without the option already skip
 *     :focus-visible after a mouse click),
 *   - closes the menu on outside click, on Escape (focus returns to the
 *     summary) and when Tab moves focus out of it,
 *   - keeps aria-expanded on the <summary> in sync with the open state,
 *   - once open, ArrowDown/ArrowUp/Home/End move focus among the menu items
 *     (wrapping), and typing a letter jumps to the next item whose label
 *     starts with it.
 * With JS disabled, the menu still opens/closes natively; it just won't
 * auto-close on outside click and won't get the extra keyboard navigation.
 *
 * Markup contract (rendered by base.html):
 *   <details class="usermenu">
 *     <summary aria-haspopup="true" aria-expanded="false">...</summary>
 *     <div class="um-panel">
 *       <div role="menu">
 *         <a role="menuitem" href="..."><span aria-hidden="true">GLYPH</span> Label</a>
 *         <button role="menuitemradio" aria-checked="false" data-theme-set="light">Light</button>
 *         ...
 *       </div>
 *     </div>
 *   </details>
 */
(function () {
  "use strict";

  var menus = document.querySelectorAll("details.usermenu");
  if (!menus.length) { return; }

  function items(menu) {
    return Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"], [role="menuitemradio"]'));
  }

  function focusIndex(menu, idx, opts) {
    var list = items(menu);
    if (!list.length) { return; }
    var next = ((idx % list.length) + list.length) % list.length; // wrap both ways
    if (opts) { list[next].focus(opts); } else { list[next].focus(); }
  }

  function closeMenu(menu, refocusSummary) {
    var summary = menu.querySelector("summary");
    menu.removeAttribute("open");
    if (summary) {
      summary.setAttribute("aria-expanded", "false");
      if (refocusSummary) { summary.focus(); }
    }
  }

  // The human-readable label of a menu item: its text WITHOUT the aria-hidden
  // icon glyph (the glyph is decorative and hidden from assistive tech).
  function labelOf(item) {
    var text = "";
    for (var n = item.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 1 && n.getAttribute("aria-hidden") === "true") { continue; }
      text += n.textContent || "";
    }
    return text.replace(/\s+/g, " ").trim().toLowerCase();
  }

  // Jump to the next item whose visible label starts with the typed letter,
  // wrapping past the current position.
  function typeahead(menu, list, current, letter) {
    var start = current === -1 ? 0 : current + 1;
    for (var i = 0; i < list.length; i++) {
      var idx = (start + i) % list.length;
      if (labelOf(list[idx]).indexOf(letter) === 0) {
        list[idx].focus();
        return true;
      }
    }
    return false;
  }

  menus.forEach(function (menu) {
    var summary = menu.querySelector("summary");
    if (!summary) { return; }
    var openedByKeyboard = false;
    var openedByPointer = false;

    // Native <details> toggles on Enter/Space when the summary has focus. A
    // keyboard open keeps the browser's own focus-visible heuristic (ring on).
    summary.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
        openedByKeyboard = true;
      }
    });
    // A real pointer click (mouse, pen, touch): click is a PointerEvent with a
    // pointerType in Chrome and Firefox ("" for a keyboard or synthetic click);
    // Safari's MouseEvent has none, so fall back to detail (0 = not a pointer).
    // The click runs before the queued toggle event.
    summary.addEventListener("click", function (e) {
      var pt = e.pointerType;
      openedByPointer = typeof pt === "string" ? pt !== "" : e.detail > 0;
    });

    // Fires on every open/close, mouse or keyboard driven, native or scripted.
    menu.addEventListener("toggle", function () {
      var open = menu.hasAttribute("open");
      summary.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        focusIndex(menu, 0, openedByPointer && !openedByKeyboard ? { focusVisible: false } : null);
      }
      openedByKeyboard = false;
      openedByPointer = false;
    });

    // Tab (or anything else) moving focus OUT of the open menu closes it.
    menu.addEventListener("focusout", function (e) {
      var to = e.relatedTarget;
      if (menu.hasAttribute("open") && to && !menu.contains(to)) {
        closeMenu(menu, false);
      }
    });

    menu.addEventListener("keydown", function (e) {
      if (!menu.hasAttribute("open")) { return; }
      var list = items(menu);
      if (!list.length) { return; }
      var current = list.indexOf(document.activeElement);

      if (e.key === "Escape") {
        e.preventDefault();
        closeMenu(menu, true);
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        focusIndex(menu, current === -1 ? 0 : current + 1);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        focusIndex(menu, current === -1 ? list.length - 1 : current - 1);
        return;
      }
      if (e.key === "Home") {
        e.preventDefault();
        focusIndex(menu, 0);
        return;
      }
      if (e.key === "End") {
        e.preventDefault();
        focusIndex(menu, list.length - 1);
        return;
      }
      if (e.key.length === 1 && /[a-z0-9]/i.test(e.key)) {
        if (typeahead(menu, list, current, e.key.toLowerCase())) { e.preventDefault(); }
      }
    });
  });

  // Outside click closes any open menu (native <details> has no such rule).
  document.addEventListener("click", function (e) {
    menus.forEach(function (menu) {
      if (menu.hasAttribute("open") && !menu.contains(e.target)) {
        closeMenu(menu, false);
      }
    });
  });

  // Belt-and-braces global Escape: closes an open menu even if focus somehow
  // ended up outside it (e.g. a click focused something else on the page).
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") { return; }
    menus.forEach(function (menu) {
      if (menu.hasAttribute("open")) { closeMenu(menu, false); }
    });
  });
})();
