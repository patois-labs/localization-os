/* Language multi-select picker (P2, item 1).
 *
 * Progressive enhancement over a plain checkbox list: any element with
 * [data-langpicker] gets a search box that filters its options, a live count, a
 * select-all / none pair, a row of removable "selected" chips, and a "show all
 * languages" toggle that reveals options flagged data-disabled="1" (the escape
 * hatch to codes outside the enabled subset). No build step, no framework - it
 * degrades to a scrollable checkbox list if JS is off (the form still submits the
 * checked boxes; every checkbox shares the picker's name).
 *
 * Markup contract (rendered by the templates):
 *   <div data-langpicker>
 *     <input data-lp-search>                 (search box, optional)
 *     <button data-lp-all>  <button data-lp-none>  <button data-lp-showall>
 *     <span data-lp-count></span>
 *     <div data-lp-chips></div>              (selected chips render here)
 *     <div data-lp-list>
 *       <label data-lp-opt data-code="fr-FR" data-label="French (France) fr-FR"
 *              data-disabled="0">
 *         <input type="checkbox" name="target_locales" value="fr-FR"> French (France)
 *       </label>
 *       ...
 *     </div>
 *   </div>
 */
(function () {
  "use strict";

  function initPicker(root) {
    // Idempotence guard: the script may be loaded twice (base.html global
    // dialog + a page-local tag) - binding handlers twice would make every
    // toggle fire twice and cancel itself out.
    if (root.dataset.lpReady) { return; }
    root.dataset.lpReady = "1";
    var search = root.querySelector("[data-lp-search]");
    var list = root.querySelector("[data-lp-list]");
    var chips = root.querySelector("[data-lp-chips]");
    var count = root.querySelector("[data-lp-count]");
    var btnAll = root.querySelector("[data-lp-all]");
    var btnNone = root.querySelector("[data-lp-none]");
    var btnShowAll = root.querySelector("[data-lp-showall]");
    var opts = Array.prototype.slice.call(root.querySelectorAll("[data-lp-opt]"));
    var showingAll = false;

    function boxOf(opt) { return opt.querySelector('input[type="checkbox"]'); }

    function selectedCodes() {
      return opts.filter(function (o) { return boxOf(o).checked; })
                 .map(function (o) { return o.getAttribute("data-code"); });
    }

    function renderChips() {
      if (!chips) { return; }
      chips.innerHTML = "";
      opts.filter(function (o) { return boxOf(o).checked; }).forEach(function (o) {
        var code = o.getAttribute("data-code");
        var chip = document.createElement("span");
        chip.className = "chip lp-chip";
        chip.textContent = code + " ";
        var x = document.createElement("button");
        x.type = "button";
        x.className = "lp-chip-x";
        x.setAttribute("aria-label", "Remove " + code);
        x.textContent = "×";
        x.addEventListener("click", function () {
          boxOf(o).checked = false;
          refresh();
        });
        chip.appendChild(x);
        chips.appendChild(chip);
      });
    }

    function renderCount() {
      if (!count) { return; }
      var n = selectedCodes().length;
      count.textContent = n + " selected";
    }

    function applyFilter() {
      var q = search ? (search.value || "").trim().toLowerCase() : "";
      opts.forEach(function (o) {
        var disabled = o.getAttribute("data-disabled") === "1";
        var hay = (o.getAttribute("data-label") || "").toLowerCase();
        var matches = !q || hay.indexOf(q) !== -1;
        // A disabled (not-enabled) option is hidden unless "show all" is on OR it
        // is already checked (so a grandfathered selection stays visible).
        var visibleByTier = showingAll || !disabled || boxOf(o).checked;
        o.style.display = (matches && visibleByTier) ? "" : "none";
      });
    }

    function refresh() {
      renderChips();
      renderCount();
      applyFilter();
    }

    if (search) { search.addEventListener("input", applyFilter); }
    opts.forEach(function (o) {
      boxOf(o).addEventListener("change", refresh);
    });
    if (btnAll) {
      btnAll.addEventListener("click", function () {
        // Only toggle currently-visible options (respects the search filter).
        opts.forEach(function (o) {
          if (o.style.display !== "none") { boxOf(o).checked = true; }
        });
        refresh();
      });
    }
    if (btnNone) {
      btnNone.addEventListener("click", function () {
        opts.forEach(function (o) { boxOf(o).checked = false; });
        refresh();
      });
    }
    if (btnShowAll) {
      btnShowAll.addEventListener("click", function () {
        showingAll = !showingAll;
        btnShowAll.textContent = showingAll
          ? "Show enabled only" : "Show all languages";
        btnShowAll.setAttribute("aria-pressed", showingAll ? "true" : "false");
        applyFilter();
      });
    }

    refresh();
  }

  function init() {
    Array.prototype.slice.call(document.querySelectorAll("[data-langpicker]"))
      .forEach(initPicker);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
