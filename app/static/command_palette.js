/* Search dialog (Ctrl+K, or click / type in the top bar). It owns no routes, permissions or
   persisted state: pages come from the rendered sidebar, results from GET /search/suggest
   (the same server-side access checks as the /search page), Enter opens /search?q=. */
(function () {
  'use strict';
  // #595: the top bar is position: sticky. Chrome reveals the caret of a focused field
  // against the bar's in-flow position, so on a scrolled page every keystroke in the
  // search box scrolled the page up 56px (until it reached the top). Pin the scroll
  // position for the frames that follow an edit; a real scroll by the person is a
  // later event and is never held back.
  var held = null;
  (function keepScrollWhileTyping() {
    var box = document.getElementById('topsearch-q');
    if (!box) { return; }
    function release() { held = null; }
    function hold() {
      held = {x: window.scrollX, y: window.scrollY};
      window.requestAnimationFrame(function () { window.requestAnimationFrame(release); });
    }
    box.addEventListener('beforeinput', hold);
    window.addEventListener('scroll', function () {
      if (held && window.scrollY !== held.y) { window.scrollTo(held.x, held.y); }
    }, {passive: true});
  }());
  var dialog = document.getElementById('command-palette');
  var trigger = document.querySelector('[data-command-palette-open]');
  var form = document.querySelector('.topsearch-form');
  if (!dialog || !trigger || !form || !dialog.showModal) { return; }
  var box = form.querySelector('[name="q"]');
  var input = dialog.querySelector('[role="combobox"]');
  var list = dialog.querySelector('[role="listbox"]');
  var status = dialog.querySelector('[role="status"]');
  var MIN_QUERY = 2, DEBOUNCE_MS = 200;
  var items = [], results = [], optionEls = [], hits = [], note = '', active = 0, touched = false;
  var opener, openedAt = null, timer = null, pending = null, fetchedFor = null, releaseTimer = null;
  var GROUPS = ['projects', 'documents', 'tm', 'glossary', 'workflows', 'connections'];
  function fold(value) { return value.normalize('NFD').replace(/[̀-ͯ]/g, '').toLocaleLowerCase(); }
  function readNavigation() {
    var heading = '';
    var sidebar = document.querySelector('.sidebar');
    var drawerHidden = sidebar && getComputedStyle(sidebar).visibility === 'hidden';
    return Array.from(document.querySelectorAll('.side-nav .nav-group, .side-nav a[href]')).flatMap(function (el) {
      if (el.classList.contains('nav-group')) { heading = el.textContent.trim(); return []; }
      // Test visibility before showModal makes the background inert. Links inside a
      // collapsed sidebar group have no layout box (display: none) but are still
      // permitted pages, so layout rectangles are deliberately not required. Role and
      // feature gates are applied server-side: a gated link is never rendered.
      if (el.closest('[hidden]') ||
          (getComputedStyle(el).visibility === 'hidden' && (!drawerHidden || el.style.visibility === 'hidden'))) { return []; }
      var label = el.querySelector('.nav-label');
      if (!label) {
        label = el.cloneNode(true);
        label.querySelectorAll('[aria-hidden="true"], .badge').forEach(function (n) { n.remove(); });
      }
      return [{label: label.textContent.trim(), group: heading, href: el.getAttribute('href')}];
    });
  }
  function select(index) {
    active = index;
    optionEls.forEach(function (el, i) { el.setAttribute('aria-selected', String(i === active)); });
    var option = optionEls[active];
    if (option) {
      input.setAttribute('aria-activedescendant', option.id);
      option.scrollIntoView({block: 'nearest'});
    } else { input.removeAttribute('aria-activedescendant'); }
  }
  function query() { return input.value.replace(/\s+/g, ' ').trim(); }
  function searchHref(q) { return '/search?q=' + encodeURIComponent(q); }
  // The flat, ordered list of options and the headings between them.
  function build(q, needle) {
    var out = [];
    if (q.length >= MIN_QUERY) {
      out.push({section: null, label: dialog.dataset.searchLabel.replace('{query}', function () { return q; }),
                href: searchHref(q), all: true});
    }
    var pages = items.filter(function (item) { return fold(item.label + ' ' + item.group).includes(needle); });
    pages.forEach(function (item, i) {
      out.push({section: i === 0 ? dialog.dataset.pagesLabel : null, label: item.label, small: item.group,
                href: item.href, page: true});
    });
    hits.forEach(function (g) {
      (g.hits || []).forEach(function (hit, i) {
        out.push({section: i === 0 ? (dialog.dataset['group' + g.key.charAt(0).toUpperCase() + g.key.slice(1)] || g.label) : null,
                  label: hit.title, small: hit.subtitle || '', href: hit.href, result: true});
      });
    });
    return out;
  }
  function render() {
    var q = query(), needle = fold(q);
    var previous = touched && results[active] ? results[active].href : null;
    results = build(q, needle);
    list.replaceChildren();
    optionEls = [];
    var group = null, groupIndex = 0;
    results.forEach(function (item, i) {
      if (item.section) {
        var heading = document.createElement('div');
        heading.id = 'command-palette-group-' + (groupIndex++);
        heading.className = 'command-palette-heading';
        heading.textContent = item.section;
        group = document.createElement('div');
        group.setAttribute('role', 'group');
        group.setAttribute('aria-labelledby', heading.id);
        list.append(heading, group);
      } else if (item.all || !group) {
        group = list;
      }
      var option = document.createElement('div');
      option.id = 'command-palette-option-' + i;
      option.setAttribute('role', 'option');
      option.className = 'command-palette-option';
      if (item.page) { option.dataset.href = item.href; }
      if (item.result) { option.dataset.resultHref = item.href; }
      if (item.all) { option.dataset.searchAll = '1'; option.classList.add('command-palette-all'); }
      var name = document.createElement('span'); name.textContent = item.label; option.append(name);
      if (item.small) { var small = document.createElement('small'); small.textContent = item.small; option.append(small); }
      group.append(option);
      optionEls.push(option);
    });
    if (note) {
      var line = document.createElement('div');
      line.className = 'command-palette-note'; line.textContent = note;
      list.append(line);
    }
    var keep = previous ? results.findIndex(function (item) { return item.href === previous; }) : -1;
    select(keep >= 0 ? keep : 0);
    // The count names page and result matches; the "search everything" row is always there.
    var matches = results.filter(function (item) { return !item.all; }).length;
    status.textContent = matches === 0 ? dialog.dataset.countNoneLabel
      : (matches === 1 ? dialog.dataset.countOneLabel : dialog.dataset.countLabel).replace('{count}', matches);
  }
  function activate() {
    var item = results[active];
    if (item && item.href) { window.location.assign(item.href); }
  }
  function cancelFetch() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (pending) { pending.abort(); pending = null; }
  }
  // Debounced live results. A response for an older query is dropped.
  function fetchResults() {
    var q = query();
    cancelFetch();
    if (q.length < MIN_QUERY) {
      hits = []; fetchedFor = null;
      note = q.length ? dialog.dataset.shortLabel : '';
      return;
    }
    if (fetchedFor !== q) { hits = []; }
    note = dialog.dataset.waitLabel;
    timer = setTimeout(function () {
      timer = null;
      var controller = typeof AbortController === 'function' ? new AbortController() : null;
      pending = controller;
      fetch('/search/suggest?q=' + encodeURIComponent(q), {
        credentials: 'same-origin', headers: {'Accept': 'application/json'},
        signal: controller ? controller.signal : undefined
      }).then(function (response) {
        return response.json().then(function (body) { return {ok: response.ok, body: body}; });
      }).then(function (result) {
        if (query() !== q || !dialog.open) { return; }
        pending = null;
        if (!result.ok || !result.body || !result.body.ok) {
          hits = []; fetchedFor = null;
          note = (result.body && result.body.error) || dialog.dataset.errorLabel;
        } else {
          hits = (result.body.groups || []).filter(function (g) {
            return GROUPS.indexOf(g.key) >= 0 && g.hits && g.hits.length;
          });
          fetchedFor = q; note = '';
        }
        render();
      }).catch(function (error) {
        if (error && error.name === 'AbortError') { return; }
        if (query() !== q) { return; }
        pending = null; hits = []; fetchedFor = null; note = dialog.dataset.errorLabel; render();
      });
    }, DEBOUNCE_MS);
  }
  list.addEventListener('click', function (event) {
    var option = event.target.closest('[role="option"]');
    if (!option) { return; }
    input.focus();
    touched = true;
    select(optionEls.indexOf(option));
    activate();
  });
  function open(initial) {
    if (dialog.open || document.querySelector('dialog[open], .app.nav-open')) { return; }
    opener = document.activeElement;
    // While a keystroke is being handled the browser may already have moved the page; the
    // position before it is the one to come back to.
    openedAt = held || {x: window.scrollX, y: window.scrollY};
    items = readNavigation(); hits = []; note = ''; touched = false; fetchedFor = null;
    input.value = initial || '';
    fetchResults(); render();
    // Blur the opener first so the dialog remembers no element: the browser's own focus
    // restore on close scrolls that element into view (the sticky top bar jumps the page),
    // and the close handler below refocuses it without scrolling instead.
    if (opener && opener !== document.body && typeof opener.blur === 'function') { opener.blur(); }
    dialog.showModal(); input.setAttribute('aria-expanded', 'true'); input.focus();
    if (input.value) { input.setSelectionRange(input.value.length, input.value.length); }
  }
  // Show the platform's modifier name; the key handler already accepts Ctrl or Cmd.
  if (/mac|iphone|ipad/i.test((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || '')) {
    var hint = trigger.querySelector('kbd');
    if (hint) { hint.textContent = 'Cmd+K'; }
    trigger.setAttribute('aria-label', trigger.getAttribute('aria-label').replace('Ctrl+K', 'Cmd+K'));
  }
  trigger.hidden = false;
  trigger.addEventListener('click', function () { open(''); });
  // The top bar is the entry point to the same dialog: a click opens it with the box's text,
  // and typing moves what was typed into the dialog and puts the box back as it was. Tabbing
  // into the box changes nothing (no context change on focus), and Enter in the box still
  // submits the form, which opens the full /search page without JavaScript.
  if (box) {
    box.addEventListener('click', function () { open(box.value); });
    var moveTyping = function () {
      var typed = box.value;
      box.value = box.defaultValue;
      open(typed);
    };
    box.addEventListener('input', function (event) { if (!event.isComposing && !dialog.open) { moveTyping(); } });
    box.addEventListener('compositionend', function () { if (!dialog.open) { moveTyping(); } });
  }
  // Give focus back to the opener without scrolling, and keep the page where it was (the
  // page cannot have scrolled while the dialog was modal). Safe to run twice.
  function restoreOpener() {
    if (opener && opener.isConnected && document.activeElement !== opener) { opener.focus({preventScroll: true}); }
    if (openedAt) {
      held = openedAt;
      window.scrollTo(held.x, held.y);
      clearTimeout(releaseTimer);
      releaseTimer = setTimeout(function () { held = null; }, 300);
    }
  }
  // Close synchronously (the close event is queued), so focus and scroll are settled as soon
  // as Escape or the close button has been handled.
  function closeDialog() {
    if (!dialog.open) { return; }
    dialog.close();
    restoreOpener();
  }
  dialog.querySelector('[data-command-palette-close]').addEventListener('click', closeDialog);
  dialog.addEventListener('cancel', function (event) { event.preventDefault(); closeDialog(); });
  dialog.addEventListener('close', function () {
    cancelFetch();
    input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant');
    restoreOpener();
  });
  dialog.addEventListener('keydown', function (event) {
    if (event.key !== 'Tab') { return; }
    var close = dialog.querySelector('[data-command-palette-close]');
    if (!event.shiftKey && document.activeElement === input) { event.preventDefault(); close.focus(); }
    else if (event.shiftKey && document.activeElement === close) { event.preventDefault(); input.focus(); }
  });
  input.addEventListener('input', function () { touched = false; fetchResults(); render(); });
  input.addEventListener('keydown', function (event) {
    if (event.isComposing) { return; }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      touched = true;
      if (optionEls.length) { select((active + (event.key === 'ArrowDown' ? 1 : -1) + optionEls.length) % optionEls.length); }
    } else if (event.key === 'Enter') { event.preventDefault(); activate(); }
  });
  // Bubble at window, after page/editor handlers. Never take typing or handled keys.
  window.addEventListener('keydown', function (event) {
    var target = event.target;
    if (event.defaultPrevented || event.isComposing || event.repeat || event.altKey || event.shiftKey ||
        !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'k' ||
        target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"]')) { return; }
    if (dialog.open || document.querySelector('dialog[open], .app.nav-open')) { return; }
    event.preventDefault(); open('');
  });
}());
