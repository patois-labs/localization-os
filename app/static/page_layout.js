/* Editable page layouts (train L1, SPEC-project-layout-editor-2026-09).
 *
 * A page with movable cards renders them in its DEFAULT order inside one wrapper
 * (#page-layout, data-layout-page="<key>"), each card a <section data-card="<key>">,
 * followed by an inert JSON blob (#page-layout-data, rendered with |tojson) and this
 * plain, non-deferred script. The blob carries the signed-in person's saved rows for
 * this page TYPE (or the default), so the layout is applied here, synchronously, before
 * first paint, by MOVING the real card nodes into row containers: DOM order = visual
 * order, so Tab and a screen reader follow the layout (WCAG 1.3.2, 2.4.3). Nothing is
 * cloned and no markup string is ever parsed (no innerHTML; every label goes through
 * textContent).
 *
 * Fail-open: with JS off, a 404 on this file, a bad blob, or no saved layout, the page
 * keeps its server markup in the default order, fully usable. The Edit layout button
 * is rendered `hidden` and only shown by this script, on desktop widths (> 900 px).
 *
 * Edit mode (spec sections 1, 4, 5): every card becomes a tile (title + tools; its
 * forms are inert and hidden, so nothing can be submitted by accident); every row gets
 * a real radio group for its width (1 wide / 2 across / 3 across) and Move row buttons.
 * Tiles move by HTML5 drag and drop AND by keyboard buttons (Move up / down / left /
 * right, Own row) whose names contain the card title; focus stays on the pressed
 * button (aria-disabled, never disabled, so focus is never lost) and a polite
 * announcement says where the card went. Escape = Cancel. Done saves with fetch PUT
 * (busy indicator per DESIGN.md section 5; on failure the error is announced and edit
 * mode stays open); Reset to default = DELETE.
 *
 * The layout MODEL below is pure (no DOM) and is exported for the node test in
 * tests/verify_page_layouts.py. Card keys come from the server and from the DOM; every
 * map keyed by them is null-prototype (the sidebar_ux.js SB-101 lesson), so a key like
 * "constructor" or "__proto__" can never resolve to an inherited member.
 */
(function (root) {
  "use strict";

  var MAX = 3;

  /* ------------------------------------------------------------------ model -- */
  function isArr(x) { return Object.prototype.toString.call(x) === "[object Array]"; }

  function keyMap(list) {
    var m = Object.create(null);
    (isArr(list) ? list : []).forEach(function (k) { if (typeof k === "string") { m[k] = true; } });
    return m;
  }

  function clone(rows) { return rows.map(function (r) { return r.slice(); }); }

  function dropEmpty(rows) { return rows.filter(function (r) { return r.length > 0; }); }

  /* Keep only string keys in `allowed`, each once, split rows over the cap into
     consecutive rows, drop empty rows. Never throws on hostile input. */
  function normalize(rows, allowed, maxPer) {
    var cap = maxPer || MAX;
    var ok = keyMap(allowed);
    var seen = Object.create(null);
    var out = [];
    (isArr(rows) ? rows : []).forEach(function (row) {
      if (!isArr(row)) { return; }
      var kept = [];
      row.forEach(function (k) {
        if (typeof k === "string" && ok[k] && !seen[k]) { seen[k] = true; kept.push(k); }
      });
      for (var i = 0; i < kept.length; i += cap) { out.push(kept.slice(i, i + cap)); }
    });
    return out;
  }

  /* The rows to SHOW for the cards the server rendered: the layout restricted to them,
     plus any rendered card the layout does not mention, each in its own row at the end,
     in DOM order. */
  function forRendered(rows, rendered, maxPer) {
    var out = normalize(rows, rendered, maxPer);
    var have = Object.create(null);
    out.forEach(function (r) { r.forEach(function (k) { have[k] = true; }); });
    (isArr(rendered) ? rendered : []).forEach(function (k) {
      if (typeof k === "string" && !have[k]) { have[k] = true; out.push([k]); }
    });
    return out;
  }

  function locate(rows, key) {
    for (var r = 0; r < rows.length; r++) {
      var i = rows[r].indexOf(key);
      if (i !== -1) { return { r: r, i: i }; }
    }
    return null;
  }

  /* Put back the cards of the SAVED layout this render did not show (a budget card a
     translator cannot see, Upload on a key project), next to the card they followed, so
     saving on a smaller render never loses their place (the SB-03 lesson). */
  function mergeHidden(rows, saved, rendered, maxPer) {
    var cap = maxPer || MAX;
    var shown = keyMap(rendered);
    var out = clone(rows);
    var placed = Object.create(null);
    out.forEach(function (r) { r.forEach(function (k) { placed[k] = true; }); });
    var prev = normalize(saved, Object.keys(keyMapAll(saved)), cap);
    var anchor = null;  // the last SHOWN card before this point in the saved order
    prev.forEach(function (row) {
      var rowAnchor = null;
      row.forEach(function (k) {
        if (shown[k]) { anchor = k; rowAnchor = k; return; }
        if (placed[k]) { return; }
        placed[k] = true;
        var at = rowAnchor ? locate(out, rowAnchor) : null;
        if (at && out[at.r].length < cap) {
          out[at.r].splice(at.i + 1, 0, k);
          rowAnchor = k;
          return;
        }
        var a = anchor ? locate(out, anchor) : null;
        var pos = a ? a.r + 1 : 0;
        out.splice(pos, 0, [k]);
        anchor = k;
        rowAnchor = k;
      });
    });
    return dropEmpty(out);
  }

  function keyMapAll(rows) {
    var m = Object.create(null);
    (isArr(rows) ? rows : []).forEach(function (r) {
      if (isArr(r)) { r.forEach(function (k) { if (typeof k === "string") { m[k] = true; } }); }
    });
    return m;
  }

  /* Keyboard moves. Each returns NEW rows; an impossible move returns an equal copy
     (same() tells the caller to mark that button aria-disabled). */
  function moveUp(rows, key, maxPer) {
    var cap = maxPer || MAX;
    var out = clone(rows);
    var at = locate(out, key);
    if (!at) { return out; }
    var row = out[at.r];
    if (row.length > 1) {
      row.splice(at.i, 1);
      out.splice(at.r, 0, [key]);  // its own row, just above
      return dropEmpty(out);
    }
    if (at.r === 0) { return out; }
    var prev = out[at.r - 1];
    if (prev.length < cap) {
      prev.push(key);
      out.splice(at.r, 1);
    } else {
      out[at.r] = prev;
      out[at.r - 1] = row;
    }
    return dropEmpty(out);
  }

  function moveDown(rows, key, maxPer) {
    var cap = maxPer || MAX;
    var out = clone(rows);
    var at = locate(out, key);
    if (!at) { return out; }
    var row = out[at.r];
    if (row.length > 1) {
      row.splice(at.i, 1);
      out.splice(at.r + 1, 0, [key]);  // its own row, just below
      return dropEmpty(out);
    }
    if (at.r === out.length - 1) { return out; }
    var next = out[at.r + 1];
    if (next.length < cap) {
      next.unshift(key);
      out.splice(at.r, 1);
    } else {
      out[at.r] = next;
      out[at.r + 1] = row;
    }
    return dropEmpty(out);
  }

  function moveLeft(rows, key) {
    var out = clone(rows);
    var at = locate(out, key);
    if (at && at.i > 0) {
      var row = out[at.r];
      row[at.i] = row[at.i - 1];
      row[at.i - 1] = key;
    }
    return out;
  }

  function moveRight(rows, key) {
    var out = clone(rows);
    var at = locate(out, key);
    if (at && at.i < out[at.r].length - 1) {
      var row = out[at.r];
      row[at.i] = row[at.i + 1];
      row[at.i + 1] = key;
    }
    return out;
  }

  function ownRow(rows, key) {
    var out = clone(rows);
    var at = locate(out, key);
    if (at && out[at.r].length > 1) {
      out[at.r].splice(at.i, 1);
      out.splice(at.r + 1, 0, [key]);
    }
    return dropEmpty(out);
  }

  /* A row's width picker: w cards side by side. Fewer than it holds: the rest move to a
     new row below. More: cards are pulled up from the rows below, in order. */
  function setWidth(rows, r, w, maxPer) {
    var cap = maxPer || MAX;
    var out = clone(rows);
    if (!out[r]) { return out; }
    w = Math.max(1, Math.min(cap, parseInt(w, 10) || 1));
    var row = out[r];
    if (w < row.length) {
      out.splice(r + 1, 0, row.splice(w));
    } else {
      while (row.length < w && r + 1 < out.length) {
        row.push(out[r + 1].shift());
        if (!out[r + 1].length) { out.splice(r + 1, 1); }
      }
    }
    return dropEmpty(out);
  }

  function moveRow(rows, from, to) {
    var out = clone(rows);
    if (from < 0 || from >= out.length || to < 0 || to >= out.length) { return out; }
    var row = out.splice(from, 1)[0];
    out.splice(to, 0, row);
    return out;
  }

  /* Drag and drop: into row tr at index idx; a full row starts a new row below it. */
  function placeAt(rows, key, tr, idx, maxPer) {
    var cap = maxPer || MAX;
    var out = clone(rows);
    var at = locate(out, key);
    if (!at || !out[tr]) { return out; }
    out[at.r].splice(at.i, 1);  // empties are kept until the end so indexes stay valid
    if (at.r === tr && at.i < idx) { idx -= 1; }
    var target = out[tr];
    idx = Math.max(0, Math.min(target.length, idx));
    if (target.length < cap) { target.splice(idx, 0, key); }
    else { out.splice(tr + 1, 0, [key]); }
    return dropEmpty(out);
  }

  /* Drag and drop onto a "new row" strip: its own row at position `pos` (0..rows). */
  function placeNewRow(rows, key, pos) {
    var out = clone(rows);
    var at = locate(out, key);
    if (!at) { return out; }
    out[at.r].splice(at.i, 1);
    out.splice(Math.max(0, Math.min(out.length, pos)), 0, [key]);
    return dropEmpty(out);
  }

  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

  var Model = {
    MAX: MAX,
    normalize: normalize,
    forRendered: forRendered,
    mergeHidden: mergeHidden,
    locate: locate,
    moveUp: moveUp,
    moveDown: moveDown,
    moveLeft: moveLeft,
    moveRight: moveRight,
    ownRow: ownRow,
    setWidth: setWidth,
    moveRow: moveRow,
    placeAt: placeAt,
    placeNewRow: placeNewRow,
    same: same
  };

  if (typeof module === "object" && module && module.exports) { module.exports = Model; }
  if (typeof document === "undefined") { return; }
  root.PageLayoutModel = Model;

  /* -------------------------------------------------------------------- DOM -- */
  var wrap = document.getElementById("page-layout");
  var dataEl = document.getElementById("page-layout-data");
  if (!wrap || !dataEl) { return; }
  var blob;
  try { blob = JSON.parse(dataEl.textContent || "null"); } catch (e) { return; }
  if (!blob || !isArr(blob.rows)) { return; }
  var cap = Math.max(1, Math.min(MAX, parseInt(blob.max_per_row, 10) || MAX));
  var cards = Object.create(null);  // key -> <section>, only the ones rendered
  var rendered = [];
  Array.prototype.forEach.call(wrap.querySelectorAll("section[data-card]"), function (sec) {
    var k = sec.getAttribute("data-card");
    if (k && !cards[k]) { cards[k] = sec; rendered.push(k); }
  });
  if (!rendered.length) { return; }

  function tr(key, fallback, vars) {
    var s = fallback;
    try {
      var m = root.I18N;
      if (m && typeof m[key] === "string" && m[key]) { s = m[key]; }
    } catch (e) {}
    if (vars) {
      Object.keys(vars).forEach(function (v) { s = s.split("{" + v + "}").join(String(vars[v])); });
    }
    return s;
  }

  /* A card's name is its own heading (the blob carries no titles, so a card this page
     did not render never leaks its name into the markup). A card without an h2 (a
     <details> card whose name is its summary, a list card) names itself with a
     data-card-title attribute on its section (train L2). */
  function titleOf(k) {
    var h = cards[k] && cards[k].querySelector("h2");
    if (h) {
      /* A decorative icon in the heading (aria-hidden) is not part of the name, so the
         Move buttons' names start with words (L2 gate LOW 3, fixed in T11). */
      var copy = h.cloneNode(true);
      copy.querySelectorAll('[aria-hidden="true"]').forEach(function (n) { n.remove(); });
      return copy.textContent.replace(/\s+/g, " ").trim();
    }
    var named = cards[k] && cards[k].getAttribute("data-card-title");
    return named ? named.replace(/\s+/g, " ").trim() : k;
  }

  /* The rows as the server markup shows them: cards in DOM order, cards that share a
     wrapper element (the default TM + Glossary .grid2) in one row. */
  function domRows() {
    var out = [];
    var lastParent = null;
    rendered.forEach(function (k) {
      var p = cards[k].parentNode;
      if (p !== wrap && p === lastParent && out.length && out[out.length - 1].length < cap) {
        out[out.length - 1].push(k);
      } else {
        out.push([k]);
      }
      lastParent = p;
    });
    return out;
  }

  /* Unsaved, the blob's rows ARE the page's registered default rows (the server markup
     already shows them, so nothing moves). Reading them from the blob rather than from
     the DOM (train L2) keeps a default .grid2 of four cards (two rows of two on Reports)
     as two rows of two in the edit mode, where grouping by wrapper would make one row of
     three plus one. */
  var rows = forRendered(blob.rows, rendered, cap);
  var structured = false;

  /* Move the real card nodes into one .layout-row per row (appendChild only). */
  function applyRows() {
    var frag = document.createDocumentFragment();
    rows.forEach(function (row, r) {
      var div = document.createElement("div");
      div.className = "layout-row";
      div.setAttribute("data-cols", String(row.length));
      div.setAttribute("data-row", String(r));
      row.forEach(function (k) { div.appendChild(cards[k]); });
      frag.appendChild(div);
    });
    // Whatever is left in the wrapper (old rows, the default .grid2, whitespace) holds
    // no card any more; the edit bar lives outside the wrapper.
    while (wrap.firstChild) { wrap.removeChild(wrap.firstChild); }
    wrap.appendChild(frag);
    structured = true;
  }

  if (blob.saved && !same(rows, domRows())) { applyRows(); }

  /* ------------------------------------------------------------ edit mode -- */
  var editBtn = document.querySelector("[data-layout-edit]");
  var politeEl = document.getElementById("layoutAnnounce");
  var alertEl = document.getElementById("layoutAlert");
  var announcer = (typeof root.makeAnnouncer === "function")
    ? root.makeAnnouncer(politeEl, alertEl, { minGapMs: 0 })
    : { say: function () {}, alert: function () {}, clearAlert: function () {} };

  function desktop() {
    try { return root.matchMedia("(min-width: 901px)").matches; } catch (e) { return true; }
  }

  var editing = false;
  var snapshot = null;
  var bar = null;
  var barError = null;
  var extras = [];      // nodes this mode inserted (tools, row bars, strips)
  var inerted = [];     // card children this mode made inert
  var tools = Object.create(null);  // key -> { up, down, left, right, own }
  var drag = null;      // { type: 'card'|'row', key|row }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text) { e.textContent = text; }
    return e;
  }

  /* A tile tool button. The accessible name is "<verb>: <card title>" ("Move up:
     Glossary"): arrow buttons show only an aria-hidden glyph and carry the name in
     sr-only text; Own row shows its verb and adds the card title in sr-only text, so
     the name starts with the visible words (2.5.3 label in name). */
  function toolButton(action, glyph, verb, cardTitle) {
    var b = el("button", "layout-tool");
    b.type = "button";
    b.setAttribute("data-layout-action", action);
    b.title = verb;
    if (glyph) {
      var g = el("span", "layout-glyph", glyph);
      g.setAttribute("aria-hidden", "true");
      b.appendChild(g);
      b.appendChild(el("span", "sr-only", verb + ": " + cardTitle));
    } else {
      b.appendChild(el("span", "layout-tool-text", verb));
      b.appendChild(el("span", "sr-only", ": " + cardTitle));
    }
    return b;
  }

  function positionText(k) {
    var at = locate(rows, k);
    if (!at) { return ""; }
    return tr("layout_position", "{card}, row {row}, position {pos} of {count}.", {
      card: titleOf(k), row: at.r + 1, pos: at.i + 1, count: rows[at.r].length
    });
  }

  var ACTIONS = {
    up: function (k) { return moveUp(rows, k, cap); },
    down: function (k) { return moveDown(rows, k, cap); },
    left: function (k) { return moveLeft(rows, k); },
    right: function (k) { return moveRight(rows, k); },
    own: function (k) { return ownRow(rows, k); }
  };

  /* Make everything in `node` inert and hidden EXCEPT `keep` (the card's heading):
     a child that holds the heading (the .row-between of a card whose h2 sits next to a
     status badge) is walked into, so the real heading stays visible and in the
     accessibility tree while its neighbours are hidden. */
  function inertAround(node, keep) {
    Array.prototype.forEach.call(node.children, function (child) {
      if (child === keep) { return; }
      if (keep && child.contains(keep)) { inertAround(child, keep); return; }
      if (!child.hasAttribute("inert")) {
        child.setAttribute("inert", "");
        child.classList.add("layout-inert");
        inerted.push(child);
      }
    });
  }

  function buildTiles() {
    rendered.forEach(function (k) {
      var sec = cards[k];
      var t = titleOf(k);
      sec.classList.add("layout-tile");
      sec.setAttribute("draggable", "true");
      var h2 = sec.querySelector("h2");
      inertAround(sec, h2);
      /* The tools go right after the heading, or after the top-level child that holds
         it. A card with no h2 shows its data-card-title as the tile's visible name. */
      var anchor = h2;
      while (anchor && anchor.parentNode !== sec) { anchor = anchor.parentNode; }
      if (!h2) {
        anchor = el("p", "layout-tile-title", t);
        sec.insertBefore(anchor, sec.firstChild);
        extras.push(anchor);
      }
      var box = el("div", "layout-tools");
      box.setAttribute("role", "group");
      box.setAttribute("aria-label", tr("layout_tools", "Move {card}", { card: t }));
      var set = {
        up: toolButton("up", "↑", tr("layout_move_up", "Move up"), t),
        down: toolButton("down", "↓", tr("layout_move_down", "Move down"), t),
        left: toolButton("left", "←", tr("layout_move_left", "Move left"), t),
        right: toolButton("right", "→", tr("layout_move_right", "Move right"), t),
        own: toolButton("own", "", tr("layout_own_row", "Own row"), t)
      };
      ["up", "down", "left", "right", "own"].forEach(function (a) {
        set[a].setAttribute("data-layout-card", k);
        box.appendChild(set[a]);
      });
      tools[k] = set;
      if (anchor) { sec.insertBefore(box, anchor.nextSibling); }
      else { sec.insertBefore(box, sec.firstChild); }
      extras.push(box);
    });
  }

  function refreshTools() {
    rendered.forEach(function (k) {
      var set = tools[k];
      if (!set) { return; }
      Object.keys(ACTIONS).forEach(function (a) {
        var noop = same(ACTIONS[a](k), rows);
        if (noop) { set[a].setAttribute("aria-disabled", "true"); }
        else { set[a].removeAttribute("aria-disabled"); }
      });
    });
  }

  function clearRowChrome() {
    extras = extras.filter(function (n) {
      if (n.classList && (n.classList.contains("layout-rowbar") || n.classList.contains("layout-newrow"))) {
        if (n.parentNode) { n.parentNode.removeChild(n); }
        return false;
      }
      return true;
    });
  }

  function strip(pos) {
    var s = el("div", "layout-newrow", tr("layout_drop_new_row", "Drop here for a new row"));
    s.setAttribute("data-newrow", String(pos));
    s.setAttribute("aria-hidden", "true");
    extras.push(s);
    return s;
  }

  function rowBar(r) {
    var n = r + 1;
    var fs = el("fieldset", "layout-rowbar");
    fs.setAttribute("data-row", String(r));
    var lg = el("legend", "layout-rowlegend", tr("layout_row_width", "Row {n} width", { n: n }));
    fs.appendChild(lg);
    var handle = el("span", "layout-rowhandle", "☰");
    handle.setAttribute("aria-hidden", "true");
    handle.setAttribute("draggable", "true");
    handle.title = tr("layout_drag_row", "Drag to move this row");
    fs.appendChild(handle);
    var after = 0;
    for (var j = r + 1; j < rows.length; j++) { after += rows[j].length; }
    var have = rows[r].length;
    [[1, tr("layout_w1", "1 wide")], [2, tr("layout_w2", "2 across")], [3, tr("layout_w3", "3 across")]].forEach(function (opt) {
      if (opt[0] > cap) { return; }
      var lab = document.createElement("label");
      lab.className = "layout-wopt";
      var input = document.createElement("input");
      input.type = "radio";
      input.name = "layout-row-" + r;
      input.value = String(opt[0]);
      input.checked = opt[0] === have;
      input.disabled = opt[0] > have + after;
      // The wrapping label is the click target; the name is the same visible words.
      input.setAttribute("aria-label", opt[1]);
      lab.appendChild(input);
      lab.appendChild(document.createTextNode(" " + opt[1]));
      fs.appendChild(lab);
    });
    var up = el("button", "layout-tool layout-rowmove");
    up.type = "button";
    up.setAttribute("data-row-action", "up");
    up.appendChild(el("span", "layout-glyph", "⇧")).setAttribute("aria-hidden", "true");
    up.appendChild(el("span", "sr-only", tr("layout_row_up", "Move row {n} up", { n: n })));
    up.title = tr("layout_row_up", "Move row {n} up", { n: n });
    if (r === 0) { up.setAttribute("aria-disabled", "true"); }
    var down = el("button", "layout-tool layout-rowmove");
    down.type = "button";
    down.setAttribute("data-row-action", "down");
    down.appendChild(el("span", "layout-glyph", "⇩")).setAttribute("aria-hidden", "true");
    down.appendChild(el("span", "sr-only", tr("layout_row_down", "Move row {n} down", { n: n })));
    down.title = tr("layout_row_down", "Move row {n} down", { n: n });
    if (r === rows.length - 1) { down.setAttribute("aria-disabled", "true"); }
    fs.appendChild(up);
    fs.appendChild(down);
    extras.push(fs);
    return fs;
  }

  /* Re-apply the model to the page. In edit mode the row chrome (width pickers, Move
     row buttons, new-row strips) is rebuilt; the card tiles and their tools persist, so
     a focused tool button stays the same element. */
  function render(focusEl) {
    applyRows();
    if (editing) {
      clearRowChrome();
      var rowEls = Array.prototype.slice.call(wrap.querySelectorAll(":scope > .layout-row"));
      wrap.insertBefore(strip(0), rowEls[0] || null);
      rowEls.forEach(function (rowEl, r) {
        rowEl.insertBefore(rowBar(r), rowEl.firstChild);
        wrap.insertBefore(strip(r + 1), rowEl.nextSibling);
      });
      refreshTools();
    }
    if (focusEl && document.body.contains(focusEl)) { focusEl.focus(); }
  }

  function showError(msg) {
    if (barError) { barError.textContent = msg; barError.hidden = !msg; }
    if (msg) { announcer.alert(msg); } else { announcer.clearAlert(); }
  }

  function buildBar() {
    bar = el("div", "layout-editbar");
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", tr("layout_bar_label", "Page layout"));
    bar.appendChild(el("p", "layout-help", tr("layout_help",
      "Drag cards, or use each card's Move buttons. Each row holds 1, 2 or 3 cards side by side. Escape cancels.")));
    var btns = el("div", "layout-editbtns");
    var done = el("button", "layout-done", tr("layout_done", "Done"));
    done.type = "button";
    done.setAttribute("data-layout-bar", "done");
    var cancel = el("button", "ghost layout-cancel", tr("layout_cancel", "Cancel"));
    cancel.type = "button";
    cancel.setAttribute("data-layout-bar", "cancel");
    var reset = el("button", "ghost layout-reset", tr("layout_reset", "Reset to default"));
    reset.type = "button";
    reset.setAttribute("data-layout-bar", "reset");
    btns.appendChild(done);
    btns.appendChild(cancel);
    btns.appendChild(reset);
    bar.appendChild(btns);
    barError = el("p", "error layout-error");
    barError.hidden = true;
    bar.appendChild(barError);
    wrap.parentNode.insertBefore(bar, wrap);
    bar.addEventListener("click", function (e) {
      var b = e.target.closest("[data-layout-bar]");
      if (!b || b.getAttribute("aria-disabled") === "true") { return; }
      var what = b.getAttribute("data-layout-bar");
      if (what === "done") { save(b); }
      else if (what === "cancel") { cancelEdit(); }
      else if (what === "reset") { resetLayout(b); }
    });
    return done;
  }

  function enterEdit() {
    if (editing || !desktop()) { return; }
    editing = true;
    snapshot = clone(rows);
    wrap.classList.add("layout-editing");
    buildTiles();
    var done = buildBar();
    render(null);
    if (editBtn) { editBtn.setAttribute("aria-expanded", "true"); editBtn.hidden = true; }
    done.focus();
    announcer.say(tr("layout_entered",
      "Editing the page layout. Use the Move buttons on each card, or drag the cards. Escape cancels."));
  }

  function exitEdit() {
    editing = false;
    drag = null;
    clearRowChrome();
    extras.forEach(function (n) { if (n.parentNode) { n.parentNode.removeChild(n); } });
    extras = [];
    inerted.forEach(function (n) { n.removeAttribute("inert"); n.classList.remove("layout-inert"); });
    inerted = [];
    tools = Object.create(null);
    rendered.forEach(function (k) {
      cards[k].classList.remove("layout-tile", "layout-dragging", "layout-drop-before", "layout-drop-after");
      cards[k].removeAttribute("draggable");
    });
    if (bar && bar.parentNode) { bar.parentNode.removeChild(bar); }
    bar = null;
    barError = null;
    wrap.classList.remove("layout-editing");
    if (structured) { applyRows(); }
    if (editBtn) {
      editBtn.hidden = !desktop();
      editBtn.setAttribute("aria-expanded", "false");
      if (!editBtn.hidden) { editBtn.focus(); }
    }
  }

  /* While a Done (PUT) or Reset (DELETE) is in flight, the layout is frozen: Escape,
     Cancel, Reset, Done, moves, width changes and drops are all ignored, so the screen,
     the announcement and the server can never disagree about what was saved. */
  var pending = false;

  function cancelEdit() {
    if (pending) { return; }
    if (snapshot) { rows = snapshot; }
    exitEdit();
    announcer.say(tr("layout_cancelled", "Layout changes cancelled."));
  }

  function request(method, body) {
    var headers = { "X-CSRF-Token": root.CSRF_TOKEN || "", "Accept": "application/json" };  // # pregate: allow-secret CSRF header name, value from the page
    var init = { method: method, headers: headers, credentials: "same-origin" };
    if (body) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
    return fetch(blob.url, init).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok || !data || data.ok === false) {
          throw new Error((data && data.error) || ("HTTP " + resp.status));
        }
        return data;
      });
    });
  }

  function busy(btn) {
    try {
      if (root.LocBusy) {
        return root.LocBusy.start(btn, { label: tr("layout_saving", "Saving..."), announce: false });
      }
    } catch (e) {}
    return function () {};
  }

  function save(btn) {
    if (pending) { return; }
    pending = true;
    var toSave = mergeHidden(rows, blob.rows, rendered, cap);
    var done = busy(btn);
    showError("");
    request("PUT", { v: 1, rows: toSave }).then(function (data) {
      pending = false;
      done();
      blob.rows = isArr(data.rows) ? data.rows : toSave;
      blob.saved = true;
      // Show exactly what the server stored.
      rows = forRendered(blob.rows, rendered, cap);
      exitEdit();
      announcer.say(tr("layout_saved", "Layout saved."), { keep: true });
    }).catch(function (err) {
      pending = false;
      done();
      showError(tr("layout_save_failed", "The layout was not saved: {error}", { error: err.message }));
    });
  }

  function resetLayout(btn) {
    if (pending) { return; }
    pending = true;
    var done = busy(btn);
    showError("");
    request("DELETE", null).then(function (data) {
      pending = false;
      done();
      var def = isArr(data.default_rows) ? data.default_rows : blob.default_rows;
      blob.rows = def;
      blob.saved = false;
      rows = forRendered(def, rendered, cap);
      exitEdit();
      announcer.say(tr("layout_reset_done", "Layout reset to the default."), { keep: true });
    }).catch(function (err) {
      pending = false;
      done();
      showError(tr("layout_save_failed", "The layout was not saved: {error}", { error: err.message }));
    });
  }

  function doAction(btn) {
    var k = btn.getAttribute("data-layout-card");
    var a = btn.getAttribute("data-layout-action");
    if (!k || !ACTIONS[a] || btn.getAttribute("aria-disabled") === "true") { return; }
    rows = ACTIONS[a](k);
    render(btn);
    announcer.say(positionText(k));
  }

  wrap.addEventListener("click", function (e) {
    if (!editing || pending) { return; }
    var tool = e.target.closest("[data-layout-action]");
    if (tool) { doAction(tool); return; }
    var rb = e.target.closest("[data-row-action]");
    if (rb && rb.getAttribute("aria-disabled") !== "true") {
      var r = parseInt(rb.closest(".layout-rowbar").getAttribute("data-row"), 10);
      var dir = rb.getAttribute("data-row-action");
      var to = dir === "up" ? r - 1 : r + 1;
      rows = moveRow(rows, r, to);
      render(null);
      var bars = wrap.querySelectorAll(".layout-rowbar");
      var again = bars[to] && bars[to].querySelector('[data-row-action="' + dir + '"]');
      if (again) { again.focus(); }
      announcer.say(tr("layout_row_moved", "Row moved to position {n}.", { n: to + 1 }));
    }
  });

  wrap.addEventListener("change", function (e) {
    var input = e.target;
    if (!editing || !input || input.type !== "radio" || !input.closest(".layout-rowbar")) { return; }
    if (pending) { render(null); return; }  // frozen while saving: put the radios back
    var r = parseInt(input.closest(".layout-rowbar").getAttribute("data-row"), 10);
    var w = parseInt(input.value, 10);
    rows = setWidth(rows, r, w, cap);
    render(null);
    var again = wrap.querySelector('.layout-rowbar[data-row="' + r + '"] input[value="' + w + '"]');
    if (again) { again.focus(); }
    announcer.say(tr("layout_row_width_set", "Row {n} now holds {count} side by side.", {
      n: r + 1, count: rows[r] ? rows[r].length : w
    }));
  });

  /* Drag and drop (desktop pointer). Changes land on drop only; a cancelled drag
     changes nothing. */
  function clearDropMarks() {
    Array.prototype.forEach.call(wrap.querySelectorAll(".layout-drop-before, .layout-drop-after, .layout-drop-strip, .layout-drop-row"), function (n) {
      n.classList.remove("layout-drop-before", "layout-drop-after", "layout-drop-strip", "layout-drop-row");
    });
  }

  function dropTarget(e) {
    var s = e.target.closest(".layout-newrow");
    if (s && drag && drag.type === "card") { return { kind: "strip", el: s, pos: parseInt(s.getAttribute("data-newrow"), 10) }; }
    var rowEl = e.target.closest(".layout-row");
    if (!rowEl || rowEl.parentNode !== wrap) { return null; }
    var r = Array.prototype.indexOf.call(wrap.querySelectorAll(":scope > .layout-row"), rowEl);
    if (drag && drag.type === "row") { return { kind: "row", el: rowEl, r: r }; }
    var tile = e.target.closest("section[data-card]");
    if (!tile) { return { kind: "row-end", el: rowEl, r: r, idx: rows[r] ? rows[r].length : 0 }; }
    var k = tile.getAttribute("data-card");
    var at = locate(rows, k);
    if (!at) { return null; }
    var box = tile.getBoundingClientRect();
    var after = e.clientX > box.left + box.width / 2;
    return { kind: "tile", el: tile, r: at.r, idx: at.i + (after ? 1 : 0), after: after };
  }

  wrap.addEventListener("dragstart", function (e) {
    if (!editing || pending) { return; }
    var handle = e.target.closest && e.target.closest(".layout-rowhandle");
    if (handle) {
      drag = { type: "row", row: parseInt(handle.closest(".layout-rowbar").getAttribute("data-row"), 10) };
    } else {
      var tile = e.target.closest && e.target.closest("section[data-card]");
      if (!tile) { return; }
      drag = { type: "card", key: tile.getAttribute("data-card") };
      tile.classList.add("layout-dragging");
    }
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", ""); } catch (er) {}
    }
  });

  wrap.addEventListener("dragover", function (e) {
    if (!editing || !drag) { return; }
    var t = dropTarget(e);
    clearDropMarks();
    if (!t) { return; }
    e.preventDefault();
    if (t.kind === "strip") { t.el.classList.add("layout-drop-strip"); }
    else if (t.kind === "tile") { t.el.classList.add(t.after ? "layout-drop-after" : "layout-drop-before"); }
    else { t.el.classList.add("layout-drop-row"); }
  });

  wrap.addEventListener("drop", function (e) {
    if (!editing || !drag || pending) { return; }
    var t = dropTarget(e);
    clearDropMarks();
    if (!t) { return; }
    e.preventDefault();
    var before = rows;
    if (drag.type === "row") { rows = moveRow(rows, drag.row, t.r); }
    else if (t.kind === "strip") { rows = placeNewRow(rows, drag.key, t.pos); }
    else { rows = placeAt(rows, drag.key, t.r, t.idx, cap); }
    var moved = drag;
    drag = null;
    if (!same(before, rows)) {
      render(null);
      if (moved.type === "card") { announcer.say(positionText(moved.key)); }
      else { announcer.say(tr("layout_row_moved", "Row moved to position {n}.", { n: t.r + 1 })); }
    }
  });

  wrap.addEventListener("dragend", function () {
    drag = null;
    clearDropMarks();
    rendered.forEach(function (k) { cards[k].classList.remove("layout-dragging"); });
  });

  document.addEventListener("keydown", function (e) {
    // An Escape that belongs to an open dialog (the session-timeout warning, which
    // refuses Escape on purpose) must never cancel edit mode underneath it: the unsaved
    // arrangement would be lost and focus would have nowhere to return.
    if (document.querySelector("dialog[open]") ||
        (e.target && e.target.closest && e.target.closest("dialog"))) { return; }
    if (editing && !pending && e.key === "Escape" && !e.defaultPrevented) {
      e.preventDefault();
      cancelEdit();
    }
  });

  root.addEventListener("resize", function () {
    if (editing && !desktop()) { cancelEdit(); }
    if (!editing && editBtn) { editBtn.hidden = !desktop(); }
  });

  if (editBtn) {
    editBtn.hidden = !desktop();
    editBtn.setAttribute("aria-expanded", "false");
    editBtn.addEventListener("click", function () { enterEdit(); });
  }
})(typeof window !== "undefined" ? window : this);
