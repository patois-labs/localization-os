/* Customizable left sidebar (feat/sidebar-ux): collapse/expand, drag-resize, and
 * Slack-style reorder of nav groups + items. Vanilla only (CSP/offline), no libs.
 *
 * All state is per-browser localStorage:
 *   lsSidebarCollapsed  '1' | absent
 *   lsSidebarWidth      integer px (180..420)
 *   lsSidebarOrder      JSON { groups:[groupKey...], items:{ groupKey:[itemKey...] } }
 *
 * The no-flash collapse class + width var are applied in a <head> bootstrap in
 * base.html BEFORE paint; this file owns the interactive behaviour + the reorder
 * pass (which needs the parsed DOM, so it can't run in <head>).
 *
 * Fail-open (SB-05): the collapsed presentation in style.css is gated on the
 * `sb-js` class THIS script sets. If this file 404s/throws, the end-of-<body>
 * guard in base.html strips sb-js, so the sidebar always renders fully usable
 * expanded - never a control-less rail.
 *
 * Desktop-only (SB-04): the collapse/edit controls are dead or harmful on touch
 * (HTML5 drag-and-drop is not touch, a collapsed rail has no meaning under the
 * off-canvas overlay), so every interaction here is gated to >900px viewports;
 * the mobile off-canvas nav (mobilenav.js) is untouched. LTR is assumed (SB-11,
 * not built): the resize handle is on the right edge and the chevrons don't mirror.
 *
 * Reorder only ever moves nodes the server actually rendered - gated/hidden items
 * simply aren't in the DOM and are never resurrected.
 *
 * No drag-only control (a11y T8, WCAG 2.5.7 / 2.1.1): in edit mode every group and
 * item gets up/down buttons (moveItem / moveSection, a polite status announcement
 * "<label>, position n of m"), the edit bar gets a width stepper (stepWidth, 20 px
 * steps through applyWidth's clamp) and the resize handle becomes a keyboard window
 * splitter (role=separator, arrows / Home / End / Enter). The handle drags with
 * pointer events, so touch and pen work too. Same localStorage keys, no new state.
 */
(function () {
  "use strict";

  var DEFAULT_WIDTH = 252;
  var MIN_WIDTH = 180;
  var MAX_WIDTH = 420;

  var root = document.documentElement;
  var sidebar = document.querySelector(".sidebar");
  var sideNav = document.querySelector(".side-nav");
  if (!sidebar || !sideNav) { return; }

  // SB-05: mark that the interactive layer is present. The collapsed CSS is gated
  // on this class; the <head> bootstrap pre-sets it for a collapsed user (no
  // flash), and the base.html end-of-<body> guard strips it if we never got here.
  root.classList.add("sb-js");

  function ls(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function lsSet(key, val) { try { localStorage.setItem(key, val); } catch (e) {} }
  function lsDel(key) { try { localStorage.removeItem(key); } catch (e) {} }

  // SB-104: the injected control labels are load-bearing accessible names (SB-06),
  // so read them from the localized window.I18N (js.* slice, set in <head>) with an
  // English fallback for the no-I18N / missing-key case. Trusted catalog strings.
  function tr(key, fallback) {
    try {
      var m = window.I18N;
      if (m && typeof m[key] === "string" && m[key]) { return m[key]; }
    } catch (e) {}
    return fallback;
  }

  // SB-04: the feature is desktop-only. Fail toward "allowed" if matchMedia is
  // unavailable so a non-matchMedia browser keeps the desktop behaviour.
  function desktopViewport() {
    try { return window.matchMedia("(min-width: 901px)").matches; } catch (e) { return true; }
  }

  function itemKey(a) {
    return a.getAttribute("data-nav-key") || a.getAttribute("href") || a.textContent.trim();
  }

  /* --- Split "glyph label" text into spans so collapse can show glyph-only, set
     an aria-label from the label text (SB-06: the glyph alone is a useless
     accessible name in the collapsed rail, where the visible label is display:none
     - title does NOT contribute an accessible name here), and set a title for the
     hover tooltip. Idempotent. --- */
  function splitGlyph(el, forTooltip) {
    if (el.dataset.glyphSplit === "1") { return; }
    // base.html renders the glyph already wrapped (<span class="nav-glyph"
    // aria-hidden="true">), so a screen reader never reads it even before (or
    // without) this script; then the label is the first text after it.
    var pre = null;
    for (var c = el.firstElementChild; c; c = c.nextElementSibling) {
      if (c.classList.contains("nav-glyph")) { pre = c; break; }
    }
    var node = pre ? pre.nextSibling : el.firstChild;
    while (node && node.nodeType !== Node.TEXT_NODE) { node = node.nextSibling; }
    if (!node || !node.nodeValue.trim()) { el.dataset.glyphSplit = "1"; return; }
    var raw = node.nodeValue.replace(/\s+/g, " ").trim();
    var glyph, label, gEl;
    if (pre) {
      gEl = pre;
      gEl.setAttribute("aria-hidden", "true");
      glyph = gEl.textContent;
      label = raw;
    } else {
      var sp = raw.indexOf(" ");
      glyph = sp === -1 ? raw : raw.slice(0, sp);
      label = sp === -1 ? "" : raw.slice(sp + 1);
      gEl = document.createElement("span");
      gEl.className = "nav-glyph";
      gEl.setAttribute("aria-hidden", "true");
      gEl.textContent = glyph;
    }
    var lEl = document.createElement("span");
    lEl.className = "nav-label";
    lEl.textContent = label;
    var accessibleLabel = el.textContent.replace(glyph, "").replace(/\s+/g, " ").trim();
    var trailing = node.nextSibling;
    node.parentNode.replaceChild(lEl, node);
    if (!pre) { lEl.parentNode.insertBefore(gEl, lEl); }
    // T8 (2.5.3): keep the word gap before a trailing badge as its own text node
    // ("Images Beta", not "ImagesBeta"), so the visible text reads as the words of the
    // aria-label. A whitespace-only text node in the flex link renders nothing.
    if (trailing && label) { el.insertBefore(document.createTextNode(" "), trailing); }
    if (label) {
      if (!el.getAttribute("aria-label")) { el.setAttribute("aria-label", accessibleLabel); }
      if (forTooltip && !el.getAttribute("title")) { el.setAttribute("title", label); }
    }
    el.dataset.glyphSplit = "1";
  }

  /* --- Wrap the flat (heading, a, a, heading, a...) nav into <div.nav-section>
     blocks so groups + their items are real containers we can reorder. --- */
  function buildSections() {
    var kids = Array.prototype.slice.call(sideNav.children);
    var sections = [];
    var cur = null;
    kids.forEach(function (el) {
      if (el.classList && el.classList.contains("nav-group")) {
        cur = { head: el, items: [] };
        sections.push(cur);
      } else if (el.tagName === "A" && cur) {
        cur.items.push(el);
      }
    });
    sections.forEach(function (s) {
      var wrap = document.createElement("div");
      wrap.className = "nav-section";
      s.head.id = "nav-group-" + s.head.getAttribute("data-nav-key");
      wrap.setAttribute("role", "group");
      wrap.setAttribute("aria-labelledby", s.head.id);
      wrap.setAttribute("data-nav-key", s.head.getAttribute("data-nav-key") || "");
      sideNav.insertBefore(wrap, s.head);
      wrap.appendChild(s.head);
      s.items.forEach(function (a) {
        splitGlyph(a, true);
        a.setAttribute("data-nav-key", itemKey(a));
        wrap.appendChild(a);
      });
    });
    return sections;
  }

  /* App shell: fresh layouts adopt the new groups; customized legacy layouts
     retain their original containers and membership. Never write during load. */
  var originalSections;
  var originalHeads;
  var freshGroups = {
    workspace: ["/", "/my-work", "/queries"],
    projects: ["/projects", "/customers", "/images", "/jobs", "/exports"],
    "language-assets": ["/tm", "/glossaries", "/style-guides", "/content-types", "/languages", "/memories", "/memory-suggestions"],
    automation: ["/workflows", "/advanced", "/connectors", "/routing", "/vendors"],
    intelligence: ["/evaluations", "/reports", "/prompt-tap"],
    administration: ["/setup-ai", "/users", "/security", "/audit", "/settings", "/settings/enterprise"],
    system: ["/account", "/help"]
  };

  function groupDisclosure(sec) {
    var head = sec.querySelector(".nav-group");
    if (head.querySelector("button")) { return; }
    var button = document.createElement("button");
    button.type = "button";
    button.className = "nav-disclosure";
    button.textContent = head.textContent;
    button.setAttribute("aria-expanded", "true");
    head.textContent = "";
    head.appendChild(button);
    button.addEventListener("click", function () {
      if (editing) { return; }
      var closed = sec.classList.toggle("nav-section-closed");
      button.setAttribute("aria-expanded", String(!closed));
    });
  }

  function layoutSections(order) {
    if (!originalSections) {
      originalSections = Array.from(sideNav.querySelectorAll(".nav-section")).map(function (sec) {
        return { sec: sec, items: Array.from(sec.querySelectorAll(":scope > a")) };
      });
      originalHeads = Object.create(null);
      originalSections.forEach(function (entry) {
        originalHeads[entry.sec.dataset.navKey] = entry.sec.querySelector(".nav-group").cloneNode(true);
      });
    }
    var keys = order ? (Array.isArray(order.groups) ? order.groups : []).concat(Object.keys(order.items || {})) : [];
    var legacy = order && keys.indexOf("projects") === -1 && keys.indexOf("language-assets") === -1;
    // Reuse the original links only. Never create a destination from storage.
    originalSections.forEach(function (entry) {
      // A cross-tab reset can detach a legacy section while editing. Discard its
      // old move controls before reuse; syncOrderFromLs rebuilds the active set.
      Array.from(entry.sec.querySelectorAll(".sb-move")).forEach(function (control) { control.remove(); });
      entry.items.forEach(function (a) { entry.sec.appendChild(a); });
    });
    sideNav.replaceChildren();
    if (legacy) {
      originalSections.forEach(function (entry) { sideNav.appendChild(entry.sec); });
    } else {
      var links = Object.create(null);
      originalSections.forEach(function (entry) {
        entry.items.forEach(function (a) { links[itemKey(a)] = a; });
      });
      Object.keys(freshGroups).forEach(function (key) {
        if (!originalHeads[key] && key !== "projects" && key !== "language-assets") { return; }
        var permitted = freshGroups[key].map(function (href) { return links[href]; }).filter(Boolean);
        if (!permitted.length) { return; }
        var sec = document.createElement("div");
        sec.className = "nav-section";
        sec.dataset.navKey = key;
        var head = originalHeads[key] && originalHeads[key].cloneNode(true);
        if (!head) {
          head = document.createElement("div");
          head.className = "nav-group";
          head.dataset.navKey = key;
          head.textContent = key === "projects" ? sideNav.dataset.projectsLabel :
            key === "language-assets" ? sideNav.dataset.assetsLabel : sideNav.dataset.administrationLabel;
        }
        head.id = "nav-group-" + key;
        sec.setAttribute("role", "group");
        sec.setAttribute("aria-labelledby", head.id);
        sec.appendChild(head);
        permitted.forEach(function (a) { sec.appendChild(a); delete links[itemKey(a)]; });
        sideNav.appendChild(sec);
      });
      // Preserve unmapped links, including role-specific items whose target group
      // was not rendered. Their original section is the authoritative fallback.
      originalSections.forEach(function (entry) {
        var remaining = entry.items.filter(function (a) { return a.parentNode === entry.sec; });
        if (!remaining.length) { return; }
        var key = entry.sec.dataset.navKey;
        var sec = sideNav.querySelector('.nav-section[data-nav-key="' + cssEsc(key) + '"]');
        if (!sec) {
          sec = entry.sec;
          var keys = Object.keys(freshGroups);
          var next = keys.slice(keys.indexOf(key) + 1).map(function (nextKey) {
            return sideNav.querySelector('.nav-section[data-nav-key="' + cssEsc(nextKey) + '"]');
          }).filter(Boolean)[0];
          sideNav.insertBefore(sec, next || null);
        }
        remaining.forEach(function (a) { sec.appendChild(a); });
      });
    }
    Array.from(sideNav.querySelectorAll(".nav-section")).forEach(function (sec) {
      if (!sec.querySelector("a")) { sec.remove(); return; }
      groupDisclosure(sec);
    });
  }

  function ancestorTrail() {
    var crumbs = document.querySelector("[data-shell-ancestors]");
    var h1 = document.querySelector("main h1");
    if (!crumbs || !h1) { return; }
    var active = sideNav.querySelector("a.on");
    var section = active && active.closest(".nav-section");
    var heading = section && section.querySelector(".nav-group");
    var label = active && active.querySelector(".nav-label");
    if (!heading || !label) { crumbs.textContent = ""; return; }
    var parentLink = active.getAttribute("href") !== location.pathname &&
      label.textContent.trim() !== h1.textContent.trim();
    // The group is an ancestor, even where its label matches the page name.
    // In that case use Workspace alone to avoid a duplicate page title.
    crumbs.textContent = crumbs.dataset.shellAncestors;
    if (heading && heading.textContent.trim() !== h1.textContent.trim() &&
        heading.textContent.trim() !== crumbs.textContent.trim() &&
        !(parentLink && heading.textContent.trim() === label.textContent.trim())) {
      crumbs.appendChild(document.createTextNode(" / " + heading.textContent.trim()));
    }
    if (parentLink) {
      crumbs.appendChild(document.createTextNode(" / "));
      var link = document.createElement("a");
      link.href = active.getAttribute("href");
      link.textContent = label.textContent;
      crumbs.appendChild(link);
    }
  }

  function cssEsc(s) {
    if (window.CSS && CSS.escape) { return CSS.escape(s); }
    return String(s).replace(/["\\\]]/g, "\\$&");
  }

  function readOrder() {
    var raw = ls("lsSidebarOrder");
    if (!raw) { return null; }
    try { var o = JSON.parse(raw); return (o && typeof o === "object") ? o : null; }
    catch (e) { return null; }
  }

  /* --- Merge a stored sequence with the current DOM sequence (SB-03): every key
     currently in the DOM stays in its current order; a key that is ONLY in the
     stored sequence (hidden on this render - flag off / lower role) is re-inserted
     right after the stored key it used to trail, so a save made on a smaller
     render never silently drops the hidden keys. --- */
  function mergeSeq(storedList, domList) {
    // SB-101: these are indexed by stored/DOM keys, which are attacker-influenced
    // (localStorage) and can be any string - including Object.prototype member
    // names ('constructor','hasOwnProperty','toString','valueOf'). A bare {} would
    // resolve those to inherited functions: inDom['toString'] is truthy (silent
    // swallow -> key treated as in-DOM), and buckets['hasOwnProperty'] returns a
    // function whose `.push` throws inside the dragend listener (silent revert on
    // every load). Null-prototype maps have no inherited members, so every lookup
    // is honest. (Prototype itself is never touched - this is a correctness, not a
    // security, fix.)
    var inDom = Object.create(null);
    domList.forEach(function (k) { inDom[k] = true; });
    var frontBucket = [];
    var buckets = Object.create(null);  // anchorKey -> [stored-only keys, in stored order]
    var anchor = null;
    storedList.forEach(function (k) {
      if (inDom[k]) { anchor = k; }
      else if (anchor === null) { frontBucket.push(k); }
      else { (buckets[anchor] = buckets[anchor] || []).push(k); }
    });
    var out = [];
    var seen = Object.create(null);
    function push(k) { if (!seen[k]) { out.push(k); seen[k] = true; } }
    frontBucket.forEach(push);
    domList.forEach(function (k) {
      push(k);
      if (buckets[k]) { buckets[k].forEach(push); }
    });
    return out;
  }

  /* --- Apply saved order. Builds the FULL target sequence explicitly - saved
     keys in stored order first, then any node NOT in the saved list appended
     AFTER it, in DOM-relative order (SB-02/SB-10: the old appendChild-per-saved-key
     left UNLISTED nodes at the TOP, so e.g. a newly-visible Administration group
     jumped above Workspace after a role promotion). Only ever reorders existing
     nodes; appendChild-only, items stay within their own section. --- */
  function applyOrder() {
    var order = readOrder();
    layoutSections(order);
    try { ancestorTrail(); } catch (e) { /* Breadcrumbs must never block saved ordering. */ }
    if (!order) { return; }

    // Sections.
    if (Array.isArray(order.groups)) {
      var seenG = Object.create(null);  // SB-101: keyed by stored keys; no proto members
      var seqG = [];
      order.groups.forEach(function (gk) {
        var sec = sideNav.querySelector('.nav-section[data-nav-key="' + cssEsc(gk) + '"]');
        if (sec && !seenG[gk]) { seqG.push(sec); seenG[gk] = true; }
      });
      Array.prototype.forEach.call(sideNav.querySelectorAll(".nav-section"), function (sec) {
        var gk = sec.getAttribute("data-nav-key");
        if (!seenG[gk]) { seqG.push(sec); seenG[gk] = true; }
      });
      seqG.forEach(function (sec) { sideNav.appendChild(sec); });
    }
    // Items within each section.
    if (order.items && typeof order.items === "object") {
      Array.prototype.forEach.call(sideNav.querySelectorAll(".nav-section"), function (sec) {
        var gk = sec.getAttribute("data-nav-key");
        var keys = order.items[gk];
        if (!Array.isArray(keys)) { return; }
        var seenI = Object.create(null);  // SB-101: keyed by stored keys; no proto members
        var seqI = [];
        keys.forEach(function (ik) {
          var a = sec.querySelector('a[data-nav-key="' + cssEsc(ik) + '"]');
          if (a && !seenI[ik]) { seqI.push(a); seenI[ik] = true; }
        });
        Array.prototype.forEach.call(sec.querySelectorAll(":scope > a"), function (a) {
          var ik = a.getAttribute("data-nav-key");
          if (!seenI[ik]) { seqI.push(a); seenI[ik] = true; }
        });
        seqI.forEach(function (a) { sec.appendChild(a); });
      });
    }
  }

  /* --- Persist the current DOM order, MERGED with what's already stored (SB-03),
     so a drag on a render that shows fewer items (flag off / lower role) never
     drops the hidden keys, and concurrent tabs don't clobber each other. --- */
  function saveOrder() {
    var prev = readOrder();
    // SB-101: `items` and `domItems` are keyed by stored/DOM keys - null-prototype
    // so a group key like 'constructor'/'hasOwnProperty' can't collide with an
    // inherited member. JSON.stringify serialises own enumerable props regardless
    // of prototype, so the persisted shape is unchanged.
    var out = { groups: [], items: Object.create(null) };
    var domGroups = [];
    var domItems = Object.create(null);
    Array.prototype.forEach.call(sideNav.querySelectorAll(".nav-section"), function (sec) {
      var gk = sec.getAttribute("data-nav-key");
      domGroups.push(gk);
      domItems[gk] = Array.prototype.map.call(sec.querySelectorAll(":scope > a"), function (a) {
        return a.getAttribute("data-nav-key");
      });
    });
    out.groups = (prev && Array.isArray(prev.groups)) ? mergeSeq(prev.groups, domGroups) : domGroups;
    out.groups.forEach(function (gk) {
      var domList = domItems[gk];
      var prevList = prev && prev.items && Array.isArray(prev.items[gk]) ? prev.items[gk] : null;
      if (domList && prevList) { out.items[gk] = mergeSeq(prevList, domList); }
      else if (domList) { out.items[gk] = domList; }
      else if (prevList) { out.items[gk] = prevList; }  // group hidden this render - keep stored
    });
    lsSet("lsSidebarOrder", JSON.stringify(out));
  }

  /* ---------------- Controls (collapse + edit toggle) ---------------- */
  // An icon-only button's glyph sits in an aria-hidden span: the aria-label is the
  // name, and NVDA's mouse tracking must not read the symbol ("single left-pointing
  // angle quotation mark") under the pointer.
  function setGlyph(b, glyph) {
    var g = document.createElement("span");
    g.setAttribute("aria-hidden", "true");
    g.textContent = glyph;  // textContent (not innerHTML): never an HTML sink
    b.replaceChildren(g);
  }

  function makeBtn(cls, label, text, icon) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.setAttribute("aria-label", label);
    b.setAttribute("title", label);
    if (icon) { setGlyph(b, text); }
    else { b.textContent = text; }  // textContent (not innerHTML): never an HTML sink
    return b;
  }

  var controls = document.createElement("div");
  controls.className = "sb-controls";
  var collapseBtn = makeBtn("sb-collapse", tr("sb_collapse", "Collapse sidebar"), "‹", true);
  var editBtn = makeBtn("sb-edit", tr("sb_edit", "Edit sidebar layout"), "✎", true);
  editBtn.setAttribute("aria-pressed", "false");
  controls.appendChild(collapseBtn);
  controls.appendChild(editBtn);
  // Placement: directly BELOW the "New project" button - the
  // controls are secondary chrome, not the sidebar's lede. Fail-open: if .newbtn
  // is ever absent (a white-label build without it), fall back to the old top slot.
  var newBtn = sidebar.querySelector(".newbtn");
  if (newBtn) { newBtn.parentNode.insertBefore(controls, newBtn.nextSibling); }
  else { sidebar.insertBefore(controls, sidebar.firstChild); }

  // Edit-mode toolbar (Done / Reset layout), shown only while editing.
  var editBar = document.createElement("div");
  editBar.className = "sb-edit-bar";
  var doneBtn = makeBtn("sb-done", tr("sb_done_aria", "Done editing layout"), tr("sb_done", "Done"));
  var resetOrderBtn = makeBtn("sb-reset", tr("sb_reset_aria", "Reset sidebar layout"), tr("sb_reset", "Reset layout"));
  // T8 gate LOW 1 (2.5.3 label in name): the accessible name is the visible words; the
  // longer "Reset sidebar layout" stays as the tooltip only.
  resetOrderBtn.setAttribute("aria-label", tr("sb_reset", "Reset layout"));
  editBar.appendChild(doneBtn);
  editBar.appendChild(resetOrderBtn);

  // T8: the width stepper, the single-pointer and keyboard alternative to dragging the
  // resize handle. Its own line in the edit bar: "Width 252 px" then the buttons.
  function textBtn(cls, text) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = text;  // visible text IS the accessible name (label in name)
    return b;
  }
  var widthRow = document.createElement("div");
  widthRow.className = "sb-width-row";
  widthRow.setAttribute("role", "group");
  var widthLabel = document.createElement("span");
  widthLabel.className = "sb-width-label";
  widthLabel.id = "sbWidthLabel";
  widthLabel.textContent = tr("sb_width", "Width");
  widthRow.setAttribute("aria-labelledby", "sbWidthLabel");
  var widthNow = document.createElement("span");
  widthNow.className = "sb-width-now";
  var narrowerBtn = textBtn("sb-narrower", tr("sb_narrower", "Narrower"));
  var widerBtn = textBtn("sb-wider", tr("sb_wider", "Wider"));
  var resetWidthBtn = textBtn("sb-width-reset", tr("sb_width_reset", "Reset width"));
  widthRow.appendChild(widthLabel);
  widthRow.appendChild(widthNow);
  widthRow.appendChild(narrowerBtn);
  widthRow.appendChild(widerBtn);
  widthRow.appendChild(resetWidthBtn);
  editBar.appendChild(widthRow);
  sideNav.parentNode.insertBefore(editBar, sideNav.nextSibling);

  // T8: ONE polite status region for every sidebar announcement (a move's new
  // position, the new width). Present from load, so the first message is spoken.
  var sbStatus = document.createElement("p");
  sbStatus.className = "sr-only sb-status";
  sbStatus.setAttribute("role", "status");
  sbStatus.setAttribute("aria-live", "polite");
  sbStatus.setAttribute("aria-atomic", "true");
  sidebar.appendChild(sbStatus);
  var announceTimer = null;
  function announce(msg) {
    // Clear first, then set after a beat, so repeating the same text is spoken again.
    sbStatus.textContent = "";
    if (announceTimer) { clearTimeout(announceTimer); }
    announceTimer = setTimeout(function () { sbStatus.textContent = msg; }, 50);
  }
  // "{label}" style placeholders in the catalog strings. Plain text only (textContent).
  function fmt(s, vars) {
    return String(s).replace(/\{(\w+)\}/g, function (m, k) {
      return Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m;
    });
  }

  // Resize handle (right edge).
  var handle = document.createElement("div");
  handle.className = "sb-resize";
  handle.setAttribute("title", tr("sb_resize", "Drag to resize · double-click to reset"));
  handle.setAttribute("aria-hidden", "true");
  sidebar.appendChild(handle);

  function isCollapsed() { return root.classList.contains("sb-collapsed"); }

  // Visual + a11y state only - NO persistence, so the cross-tab `storage` handler
  // can mirror another tab's change without re-writing localStorage (SB-03).
  function renderCollapsed(on) {
    if (on) {
      root.classList.add("sb-collapsed");
      setGlyph(collapseBtn, "›"); // ›
      collapseBtn.setAttribute("aria-label", tr("sb_expand", "Expand sidebar"));
      collapseBtn.setAttribute("title", tr("sb_expand", "Expand sidebar"));
      if (editing) { exitEdit(); }
    } else {
      root.classList.remove("sb-collapsed");
      setGlyph(collapseBtn, "‹"); // ‹
      collapseBtn.setAttribute("aria-label", tr("sb_collapse", "Collapse sidebar"));
      collapseBtn.setAttribute("title", tr("sb_collapse", "Collapse sidebar"));
    }
  }

  function setCollapsed(on) {
    // SB-103: if the base.html fail-open guard stripped sb-js (this script threw
    // after wiring the controls but before signalling ready), collapsing has no
    // CSS effect - so the button must be an inert no-op, NOT a dead toggle that
    // still writes the lsSidebarCollapsed=1 landmine for the next good load.
    if (!root.classList.contains("sb-js")) { return; }
    renderCollapsed(on);
    if (on) { lsSet("lsSidebarCollapsed", "1"); }
    else { lsDel("lsSidebarCollapsed"); }
  }

  collapseBtn.addEventListener("click", function () {
    if (!desktopViewport()) { return; }  // SB-04: dead toggle on touch; don't write state
    setCollapsed(!isCollapsed());
  });
  // Sync the button label with the state the head bootstrap may have already
  // applied (no re-persist).
  if (isCollapsed()) { renderCollapsed(true); }

  // Also split the brand + new-project button so they collapse to glyph-only.
  var brand = sidebar.querySelector(".brand");
  if (brand) {
    var t = brand.querySelector(".mark");
    // Wrap the trailing org-name text node in a .nav-label.
    var bn = brand.lastChild;
    while (bn && bn.nodeType !== Node.TEXT_NODE) { bn = bn.previousSibling; }
    if (bn && bn.nodeValue.trim()) {
      var orgName = bn.nodeValue.replace(/\s+/g, " ").trim();
      var bl = document.createElement("span");
      bl.className = "nav-label";
      bl.textContent = bn.nodeValue.replace(/\s+/g, " ");
      bn.parentNode.replaceChild(bl, bn);
      // SB-06: the collapsed rail hides .nav-label, leaving only the ◐ mark - give
      // the brand link a real accessible name from the org text.
      if (orgName && !brand.getAttribute("aria-label")) { brand.setAttribute("aria-label", orgName); }
    }
  }
  var newbtn = sidebar.querySelector(".newbtn");
  if (newbtn) { splitGlyph(newbtn, false); }

  /* ---------------- Resize ---------------- */
  var dragging = false;

  function applyWidth(px) {
    px = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.round(px)));
    root.style.setProperty("--sidebar-w", px + "px");
    return px;
  }

  // The width in force: the stored custom property, else the CSS default. Never the
  // live box width, which lags behind during the .16s width transition.
  function currentWidth() {
    var w = parseInt(root.style.getPropertyValue("--sidebar-w"), 10);
    return (w >= MIN_WIDTH && w <= MAX_WIDTH) ? w : DEFAULT_WIDTH;
  }

  // T8: pure step math for the stepper buttons and the separator keys (20 px steps,
  // Home / End jump to the limits, clamped like applyWidth). null = not a width action.
  var WIDTH_STEP = 20;
  function widthFor(cur, action) {
    var px;
    if (action === "narrower" || action === "ArrowLeft") { px = cur - WIDTH_STEP; }
    else if (action === "wider" || action === "ArrowRight") { px = cur + WIDTH_STEP; }
    else if (action === "Home") { px = MIN_WIDTH; }
    else if (action === "End") { px = MAX_WIDTH; }
    else { return null; }
    return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.round(px)));
  }

  // Readout + separator value follow every width change (drag, stepper, keys, other tab).
  function syncWidthUi() {
    var w = currentWidth();
    widthNow.textContent = fmt(tr("sb_width_px", "{n} px"), { n: w });
    if (handle.getAttribute("role") === "separator") { handle.setAttribute("aria-valuenow", String(w)); }
  }

  function announceWidth() {
    announce(fmt(tr("sb_width_now", "Sidebar width {n} px"), { n: currentWidth() }));
  }

  function resetWidth() {
    lsDel("lsSidebarWidth");
    root.style.removeProperty("--sidebar-w");
    syncWidthUi();
  }

  // One width step (the stepper buttons and the separator keys). Same clamp and the
  // same lsSidebarWidth key as the drag: no new persistence.
  function stepWidth(action) {
    if (isCollapsed() || !desktopViewport()) { return; }
    if (action === "reset") { resetWidth(); announceWidth(); return; }
    var px = widthFor(currentWidth(), action);
    if (px === null) { return; }
    lsSet("lsSidebarWidth", String(applyWidth(px)));
    syncWidthUi();
    announceWidth();
  }

  narrowerBtn.addEventListener("click", function () { stepWidth("narrower"); });
  widerBtn.addEventListener("click", function () { stepWidth("wider"); });
  resetWidthBtn.addEventListener("click", function () { stepWidth("reset"); });

  // Pointer events (not mouse events): the drag also works with touch and pen.
  handle.addEventListener("pointerdown", function (e) {
    if (isCollapsed() || !desktopViewport()) { return; }
    if (typeof e.button === "number" && e.button !== 0) { return; }
    e.preventDefault();
    dragging = true;
    document.body.classList.add("sb-resizing");
    try { handle.setPointerCapture(e.pointerId); } catch (er) {}
    var move = function (ev) {
      if (!dragging) { return; }
      var w = applyWidth(ev.clientX - sidebar.getBoundingClientRect().left);
      lsSet("lsSidebarWidth", String(w));
      syncWidthUi();
    };
    var up = function () {
      dragging = false;
      document.body.classList.remove("sb-resizing");
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", up);
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", up);
  });

  handle.addEventListener("dblclick", function () { resetWidth(); });

  // T8: the keyboard window splitter. Only a tab stop (and only exposed) in edit mode.
  handle.addEventListener("keydown", function (e) {
    if (!editing) { return; }
    if (e.key === "Enter") { e.preventDefault(); stepWidth("reset"); return; }
    if (widthFor(currentWidth(), e.key) === null) { return; }
    e.preventDefault();
    stepWidth(e.key);
  });

  function setHandleKeyboard(on) {
    if (on) {
      handle.removeAttribute("aria-hidden");
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-valuemin", String(MIN_WIDTH));
      handle.setAttribute("aria-valuemax", String(MAX_WIDTH));
      handle.setAttribute("aria-label", tr("sb_resize_aria", "Sidebar width"));
      handle.setAttribute("tabindex", "0");
      syncWidthUi();
    } else {
      ["role", "aria-orientation", "aria-valuemin", "aria-valuemax", "aria-valuenow",
        "aria-label", "tabindex"].forEach(function (n) { handle.removeAttribute(n); });
      handle.setAttribute("aria-hidden", "true");
    }
  }

  /* ---------------- Reorder (edit mode) ---------------- */
  var sections = buildSections();
  applyOrder();

  // Rail tooltips work for keyboard focus as well as pointer hover. The link's
  // aria-label remains its name, including when the visible label is hidden.
  var railTip = document.createElement("div");
  railTip.className = "shell-nav-tooltip";
  railTip.id = "shell-nav-tooltip";
  railTip.setAttribute("role", "tooltip");
  if (sidebar.lang) { railTip.lang = sidebar.lang; }
  railTip.hidden = true;
  document.body.appendChild(railTip);
  var tipTarget = null;
  function hideRailTip() {
    if (tipTarget) {
      tipTarget.removeAttribute("aria-describedby");
      // Give the native title back: it was set aside so the browser tooltip does not
      // stack on top of the rail tooltip.
      var saved = tipTarget.getAttribute("data-rail-title");
      if (saved !== null) {
        tipTarget.setAttribute("title", saved);
        tipTarget.removeAttribute("data-rail-title");
      }
    }
    tipTarget = null;
    railTip.hidden = true;
  }
  function showRailTip(e, reposition) {
    var a = e.target.closest("a[title], a[data-rail-title]");
    if (!a || !isCollapsed() || !desktopViewport()) { return; }
    // mouseover bubbles from every child of the link: skip the forced layout below
    // when the tooltip already shows for this link.
    if (a === tipTarget && !reposition) { return; }
    if (a !== tipTarget) {
      hideRailTip();
      tipTarget = a;
      railTip.textContent = a.getAttribute("aria-label") || a.getAttribute("title");
      a.setAttribute("data-rail-title", a.getAttribute("title") || "");
      a.removeAttribute("title");
    }
    railTip.hidden = false;
    var box = a.getBoundingClientRect();
    railTip.style.left = (box.right + 10) + "px";
    railTip.style.top = Math.max(4, Math.min(box.top, innerHeight - railTip.offsetHeight - 4)) + "px";
    a.setAttribute("aria-describedby", railTip.id);
  }
  sidebar.addEventListener("focusin", showRailTip);
  sidebar.addEventListener("mouseover", showRailTip);
  sidebar.addEventListener("focusout", hideRailTip);
  sidebar.addEventListener("mouseout", function (e) {
    if (tipTarget && !tipTarget.contains(e.relatedTarget)) { hideRailTip(); }
  });
  sideNav.addEventListener("scroll", function () {
    // Focusing a link scrolls the rail. Reposition its tooltip after that scroll
    // instead of immediately dismissing the keyboard user's newly opened hint.
    // Only a tooltip that is showing is repositioned: a scroll event that lands after
    // Escape (or any other dismissal) must not bring it back.
    if (!tipTarget) { return; }
    var focused = document.activeElement;
    var box = focused && focused.getBoundingClientRect();
    var viewport = sideNav.getBoundingClientRect();
    if (focused && sideNav.contains(focused) && box.top >= viewport.top && box.bottom <= viewport.bottom) {
      showRailTip({ target: focused }, true);
    } else { hideRailTip(); }
  });
  window.addEventListener("resize", hideRailTip);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { hideRailTip(); } });

  var editing = false;
  var curDrag = null;      // { type:'section'|'item', el, container }
  var dragSnapshot = null; // DOM order captured at dragstart (SB-07: cancel = restore)

  function setDraggables(on) {
    var secs = sideNav.querySelectorAll(".nav-section");
    Array.prototype.forEach.call(secs, function (sec) {
      sec.setAttribute("draggable", on ? "true" : "false");
      if (!on) { sec.removeAttribute("draggable"); }
      Array.prototype.forEach.call(sec.querySelectorAll(":scope > a"), function (a) {
        if (on) { a.setAttribute("draggable", "true"); }
        else { a.removeAttribute("draggable"); }
      });
    });
  }

  function enterEdit() {
    if (isCollapsed() || !desktopViewport()) { return; }  // SB-04: no drag on touch/rail
    editing = true;
    sidebar.classList.add("editing");
    editBtn.setAttribute("aria-pressed", "true");
    setDraggables(true);
    setHandleKeyboard(true);
    buildMoveControls();
    syncWidthUi();
  }
  function exitEdit() {
    editing = false;
    sidebar.classList.remove("editing");
    editBtn.setAttribute("aria-pressed", "false");
    setDraggables(false);
    clearMoveControls();
    setHandleKeyboard(false);
    // SB-105: leaving edit mode (Done / collapse / viewport shrink) must cancel any
    // in-flight drag - clear the state AND roll back the live dragover mutations -
    // so the trailing dragend can't commit a reorder begun under edit mode.
    if (curDrag) {
      curDrag.el.classList.remove("sb-dragging");
      if (dragSnapshot) { restoreDom(dragSnapshot); }
    }
    curDrag = null;
    dragSnapshot = null;
  }

  editBtn.addEventListener("click", function () { editing ? exitEdit() : enterEdit(); });
  // Done hides the edit bar it sits in, so hand focus back to the edit toggle.
  doneBtn.addEventListener("click", function () { exitEdit(); editBtn.focus(); });
  resetOrderBtn.addEventListener("click", function () {
    lsDel("lsSidebarOrder");
    location.reload();
  });

  /* ---------------- Move buttons (edit mode, T8) ----------------
     The keyboard / single-pointer path to the same reorder the drag does. Each item
     gets a <span.sb-move> as its NEXT SIBLING (never inside the <a>: interactive
     content in a link is invalid), each group one right after its heading (not inside
     it: the heading names the group through aria-labelledby, and button names would
     leak into that name). applyOrder / saveOrder / snapshotDom / getDragAfter all
     select `:scope > a`, so the spans are invisible to the existing logic; anything
     that moves anchors (a drag, another tab) rebuilds them. */

  // Pure index math (node-tested): the target index of a one-step move, or -1 when the
  // element is already at that edge.
  function moveIndex(i, dir, len) {
    var j = i + dir;
    return (i < 0 || j < 0 || j >= len) ? -1 : j;
  }

  function labelOf(el) {
    return String(el.getAttribute("aria-label") || el.textContent || "").replace(/\s+/g, " ").trim();
  }

  function clearMoveControls() {
    Array.prototype.forEach.call(sideNav.querySelectorAll(".sb-move"), function (s) {
      s.parentNode.removeChild(s);
    });
  }

  function moveSpan(cls, upLabel, downLabel, first, last, onMove) {
    var span = document.createElement("span");
    span.className = cls;
    var up = makeBtn("sb-move-up", upLabel, "↑", true);
    var down = makeBtn("sb-move-down", downLabel, "↓", true);
    up.disabled = first;
    down.disabled = last;
    up.addEventListener("click", function () { onMove(-1); });
    down.addEventListener("click", function () { onMove(1); });
    span.appendChild(up);
    span.appendChild(down);
    return span;
  }

  function buildMoveControls() {
    clearMoveControls();
    if (!editing) { return; }
    var secs = Array.prototype.slice.call(sideNav.querySelectorAll(".nav-section"));
    secs.forEach(function (sec, si) {
      var head = sec.querySelector(":scope > .nav-group");
      if (head) {
        var gl = labelOf(head);
        var gs = moveSpan(
          "sb-move sb-move-group",
          fmt(tr("sb_group_up", "Move group up: {label}"), { label: gl }),
          fmt(tr("sb_group_down", "Move group down: {label}"), { label: gl }),
          si === 0, si === secs.length - 1,
          function (dir) { moveSection(sec, dir); }
        );
        sec.insertBefore(gs, head.nextSibling);
      }
      var items = Array.prototype.slice.call(sec.querySelectorAll(":scope > a"));
      items.forEach(function (a, ii) {
        var il = labelOf(a);
        var s = moveSpan(
          "sb-move",
          fmt(tr("sb_move_up", "Move up: {label}"), { label: il }),
          fmt(tr("sb_move_down", "Move down: {label}"), { label: il }),
          ii === 0, ii === items.length - 1,
          function (dir) { moveItem(a, dir); }
        );
        sec.insertBefore(s, a.nextSibling);
      });
    });
  }

  // Focus the same button of the moved row, or its twin when that one is now disabled
  // (the row reached an edge), so the keyboard user keeps their place.
  function focusMoveButton(span, dir) {
    if (!span) { return; }
    var up = span.querySelector(".sb-move-up");
    var down = span.querySelector(".sb-move-down");
    var want = dir < 0 ? up : down;
    var twin = dir < 0 ? down : up;
    var target = (want && !want.disabled) ? want : twin;
    if (target && !target.disabled) { target.focus(); }
  }

  function moveItem(a, dir) {
    var sec = a.parentNode;
    var items = Array.prototype.slice.call(sec.querySelectorAll(":scope > a"));
    var i = items.indexOf(a);
    var j = moveIndex(i, dir, items.length);
    if (j === -1) { return; }
    sec.insertBefore(a, dir < 0 ? items[j] : items[j].nextSibling);
    saveOrder();
    buildMoveControls();
    focusMoveButton(a.nextElementSibling, dir);
    announce(fmt(tr("sb_moved", "{label}, position {n} of {m}"),
      { label: labelOf(a), n: j + 1, m: items.length }));
  }

  function moveSection(sec, dir) {
    var secs = Array.prototype.slice.call(sideNav.querySelectorAll(".nav-section"));
    var i = secs.indexOf(sec);
    var j = moveIndex(i, dir, secs.length);
    if (j === -1) { return; }
    sideNav.insertBefore(sec, dir < 0 ? secs[j] : secs[j].nextSibling);
    saveOrder();
    buildMoveControls();
    var head = sec.querySelector(":scope > .nav-group");
    focusMoveButton(sec.querySelector(":scope > .sb-move-group"), dir);
    announce(fmt(tr("sb_moved", "{label}, position {n} of {m}"),
      { label: head ? labelOf(head) : "", n: j + 1, m: secs.length }));
  }

  // Block navigation while editing (so a drag-click doesn't follow the link).
  sideNav.addEventListener("click", function (e) {
    if (editing && e.target.closest("a")) { e.preventDefault(); }
  });

  function getDragAfter(container, selector, y) {
    var els = Array.prototype.slice.call(container.querySelectorAll(":scope > " + selector + ":not(.sb-dragging)"));
    var closest = { offset: -Infinity, el: null };
    els.forEach(function (child) {
      var box = child.getBoundingClientRect();
      var offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) { closest = { offset: offset, el: child }; }
    });
    return closest.el;
  }

  // Snapshot the whole nav order so a cancelled drag can restore it (SB-07).
  function snapshotDom() {
    return Array.prototype.map.call(sideNav.querySelectorAll(".nav-section"), function (sec) {
      return { sec: sec, items: Array.prototype.slice.call(sec.querySelectorAll(":scope > a")) };
    });
  }
  function restoreDom(snap) {
    snap.forEach(function (entry) {
      sideNav.appendChild(entry.sec);
      entry.items.forEach(function (a) { entry.sec.appendChild(a); });
    });
  }

  // Section drag (dragged by its heading; the section wrapper is the draggable).
  // Reordering is an edit-mode action only (#545): outside it a nav link keeps its native drag
  // (to the tab strip, a bookmarks bar) and an accidental drag never saves a new order.
  sideNav.addEventListener("dragstart", function (e) {
    if (!editing) { return; }
    var sec = e.target.closest(".nav-section");
    var a = e.target.closest("a");
    if (a) {
      // Item drag.
      curDrag = { type: "item", el: a, container: a.parentNode };
      a.classList.add("sb-dragging");
    } else if (sec) {
      curDrag = { type: "section", el: sec, container: sideNav };
      sec.classList.add("sb-dragging");
    }
    if (curDrag) { dragSnapshot = snapshotDom(); }
    if (e.dataTransfer) { e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", ""); } catch (er) {} }
  });

  sideNav.addEventListener("dragover", function (e) {
    if (!editing || !curDrag) { return; }
    if (curDrag.type === "section") {
      e.preventDefault();
      var after = getDragAfter(sideNav, ".nav-section", e.clientY);
      if (after == null) { sideNav.appendChild(curDrag.el); }
      else if (after !== curDrag.el) { sideNav.insertBefore(curDrag.el, after); }
    } else if (curDrag.type === "item") {
      // Only reorder within the item's own section.
      var overSec = e.target.closest(".nav-section");
      if (overSec !== curDrag.container) { return; }
      e.preventDefault();
      var afterA = getDragAfter(curDrag.container, "a", e.clientY);
      if (afterA == null) { curDrag.container.appendChild(curDrag.el); }
      else if (afterA !== curDrag.el) { curDrag.container.insertBefore(curDrag.el, afterA); }
    }
  });

  sideNav.addEventListener("drop", function (e) { if (curDrag) { e.preventDefault(); } });

  sideNav.addEventListener("dragend", function (e) {
    if (!editing || !curDrag) { return; }
    curDrag.el.classList.remove("sb-dragging");
    curDrag = null;
    // SB-07: Escape / drop outside a valid target leaves dropEffect 'none' - the
    // live dragover mutations must be rolled back, not committed.
    var cancelled = !!(e && e.dataTransfer && e.dataTransfer.dropEffect === "none");
    if (cancelled && dragSnapshot) { restoreDom(dragSnapshot); }
    else { saveOrder(); }
    dragSnapshot = null;
    // T8: the drag moved anchors away from their move buttons; rebuild them.
    if (editing) { buildMoveControls(); }
  });

  /* ---------------- Cross-tab + viewport reactions ---------------- */

  function syncCollapsedFromLs() { renderCollapsed(ls("lsSidebarCollapsed") === "1"); }
  function syncWidthFromLs() {
    var w = parseInt(ls("lsSidebarWidth"), 10);
    if (w >= MIN_WIDTH && w <= MAX_WIDTH) { root.style.setProperty("--sidebar-w", w + "px"); }
    else { root.style.removeProperty("--sidebar-w"); }
    syncWidthUi();
  }
  // T8: applyOrder moves anchors; while editing, their move buttons follow.
  function syncOrderFromLs() {
    applyOrder();
    // applyOrder rebuilds the section nodes, so the fresh ones need their drag state.
    if (editing) { setDraggables(true); buildMoveControls(); }
  }

  // SB-03: another tab changed our state - mirror it here (visual-only; no
  // re-persist, so tabs never ping-pong writes). `storage` fires only in OTHER
  // tabs. e.key === null means localStorage.clear().
  window.addEventListener("storage", function (e) {
    if (!e || e.key === null) { syncOrderFromLs(); syncCollapsedFromLs(); syncWidthFromLs(); return; }
    if (e.key === "lsSidebarOrder") { syncOrderFromLs(); }
    else if (e.key === "lsSidebarCollapsed") { syncCollapsedFromLs(); }
    else if (e.key === "lsSidebarWidth") { syncWidthFromLs(); }
  });

  // SB-04: if the viewport shrinks into mobile mid-edit, leave edit mode cleanly
  // (touch can't drag; the controls become CSS-hidden there).
  window.addEventListener("resize", function () {
    if (!desktopViewport() && editing) { exitEdit(); }
  });

  // SB-05: signal a clean init so the base.html end-of-<body> guard keeps sb-js.
  window.__locSidebarUxReady = true;
})();
