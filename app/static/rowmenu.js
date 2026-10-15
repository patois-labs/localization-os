/* Row actions "..." menu, following the WAI-ARIA APG menu button pattern.
 *
 * Markup contract (rendered by the row_menu macro in templates/_macros.html):
 *   <div class="row-menu" data-row-menu>
 *     <button type="button" class="row-menu-btn" aria-haspopup="menu" aria-expanded="false"
 *             aria-controls="ID" data-row-menu-btn aria-label="More actions for X">...</button>
 *     <div class="row-menu-list" id="ID" role="menu">
 *       <a role="menuitem" href="...">Open</a>
 *       <form method="post" ...><button type="submit" role="menuitem">Delete</button></form>
 *     </div>
 *   </div>
 *
 * This file is loaded in <head> and only sets html.row-menu-ready; style.css hides the lists
 * and shows the "..." buttons ONLY under that class, so with JS off (or this file missing)
 * the actions stay reachable as an inline list. With JS:
 *   Enter / Space / ArrowDown on the button opens the menu and focuses the first item
 *   (ArrowUp opens on the last); ArrowUp / ArrowDown / Home / End move among the items
 *   (wrapping); Escape closes and returns focus to the button; Tab closes; an outside
 *   click closes. The list is position:fixed and placed from the button rectangle, so a
 *   table's horizontal scroller never clips it.
 */
(function () {
  "use strict";

  var openMenu = null;
  document.documentElement.classList.add("row-menu-ready");

  function itemsOf(list) {
    return Array.prototype.slice.call(
      list.querySelectorAll('[role="menuitem"]:not([disabled]):not([aria-disabled="true"])')
    );
  }

  function place(btn, list) {
    var r = btn.getBoundingClientRect();
    list.style.visibility = "hidden";
    list.classList.add("rm-open");
    var w = list.offsetWidth;
    var h = list.offsetHeight;
    var left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
    var top = r.bottom + 4;
    if (top + h > window.innerHeight - 8 && r.top - h - 4 > 8) { top = r.top - h - 4; }
    list.style.left = left + "px";
    list.style.top = top + "px";
    list.style.visibility = "";
  }

  function close(menu, refocus) {
    if (!menu) { return; }
    var btn = menu.querySelector("[data-row-menu-btn]");
    var list = menu.querySelector(".row-menu-list");
    if (list) { list.classList.remove("rm-open"); }
    if (btn) {
      btn.setAttribute("aria-expanded", "false");
      if (refocus) { btn.focus(); }
    }
    if (openMenu === menu) { openMenu = null; }
  }

  function open(menu, which) {
    if (openMenu && openMenu !== menu) { close(openMenu, false); }
    var btn = menu.querySelector("[data-row-menu-btn]");
    var list = menu.querySelector(".row-menu-list");
    if (!btn || !list) { return; }
    place(btn, list);
    btn.setAttribute("aria-expanded", "true");
    openMenu = menu;
    var items = itemsOf(list);
    if (items.length) { (which === "last" ? items[items.length - 1] : items[0]).focus(); }
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest("[data-row-menu-btn]") : null;
    if (btn) {
      var menu = btn.closest("[data-row-menu]");
      if (menu === openMenu) { close(menu, true); } else { open(menu, "first"); }
      ev.preventDefault();
      return;
    }
    if (openMenu && !(ev.target.closest && ev.target.closest(".row-menu-list"))) {
      close(openMenu, false);
    }
  });

  document.addEventListener("keydown", function (ev) {
    var key = ev.key;
    var btn = ev.target.closest ? ev.target.closest("[data-row-menu-btn]") : null;
    if (btn && (key === "ArrowDown" || key === "ArrowUp")) {
      ev.preventDefault();
      open(btn.closest("[data-row-menu]"), key === "ArrowUp" ? "last" : "first");
      return;
    }
    if (!openMenu) { return; }
    var list = openMenu.querySelector(".row-menu-list");
    var items = itemsOf(list);
    var idx = items.indexOf(document.activeElement);
    if (key === "Escape") {
      ev.preventDefault();
      close(openMenu, true);
    } else if (key === "Tab") {
      close(openMenu, false);
    } else if (items.length && (key === "ArrowDown" || key === "ArrowUp" || key === "Home" || key === "End")) {
      ev.preventDefault();
      var next = key === "Home" ? 0
        : key === "End" ? items.length - 1
        : key === "ArrowDown" ? (idx + 1) % items.length
        : (idx - 1 + items.length) % items.length;
      items[next].focus();
    }
  });

  window.addEventListener("scroll", function (ev) {
    if (openMenu && !openMenu.contains(ev.target)) { close(openMenu, false); }
  }, true);
  window.addEventListener("resize", function () { if (openMenu) { close(openMenu, false); } });
})();
