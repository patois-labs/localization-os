/* Project section tabs share ONE saved layout and keep every real card and row. */
(function () {
  'use strict';
  var root = document.getElementById('project-sections');
  var layout = document.getElementById('page-layout');
  if (!root || !layout || !root.querySelector('[role="tab"]')) return;
  var tabs = Array.from(root.querySelectorAll('[data-component-tab]'));
  var cards = Array.from(layout.querySelectorAll('section[data-card]'));
  var groups = {
    overview: 'documents', upload: 'documents', documents: 'documents',
    langs: 'languages', tm: 'memory', glossary: 'glossary',
    members: 'members', budget: 'settings'
  };
  var list = root.querySelector('.component-tab-list');
  function sync() {
    var selected = tabs.find(function (tab) { return tab.getAttribute('aria-selected') === 'true'; });
    if (!selected) return;
    var panel = document.getElementById(selected.hash.slice(1));
    var editing = layout.classList.contains('layout-editing');
    // The existing editor still shows the complete saved layout, across sections.
    list.hidden = editing;
    if (!editing) {
      var edge = list.getBoundingClientRect();
      var active = selected.getBoundingClientRect();
      if (active.right > edge.right) list.scrollLeft += active.right - edge.right;
      else if (active.left < edge.left) list.scrollLeft += active.left - edge.left;
    }
    if (layout.parentNode !== panel) panel.appendChild(layout);
    cards.forEach(function (card) {
      var hidden = !editing && 'project-' + groups[card.dataset.card] !== panel.id;
      if (card.hidden !== hidden) card.hidden = hidden;
    });
    layout.querySelectorAll('.layout-row, .grid2').forEach(function (row) {
      var visible = Array.from(row.querySelectorAll('section[data-card]')).filter(function (card) { return !card.hidden; }).length;
      // An all-hidden row stays displayed (it is an empty zero-height grid) so it keeps its real
      // responsive column count; hiding it left an unrendered grid with a stale column list.
      // CSS view only: data-cols, the row model and saved positions stay intact.
      row.style.setProperty('--project-visible-cols', String(Math.max(1, visible)));
      // An empty row keeps its stylesheet columns (3 wide, 1 at phone width); only rows that show
      // cards collapse to their visible count.
      row.toggleAttribute('data-project-empty', !editing && !visible);
    });
  }
  function select(id) {
    var tab = tabs.find(function (item) { return item.hash === '#' + id; });
    if (!tab) return;
    var url = location.href;
    tab.click();
    history.replaceState(null, '', url);
    sync();
  }
  function reveal(target) {
    if (!target || layout.classList.contains('layout-editing')) return;
    var card = target.closest('[data-card]');
    if (card && groups[card.dataset.card]) select('project-' + groups[card.dataset.card]);
    else if (target.closest('.project-settings')) select('project-settings');
  }
  list.addEventListener('click', sync);
  list.addEventListener('keydown', sync);
  // Capture before the shared component and native focus inspect a hidden field.
  document.addEventListener('invalid', function (event) {
    if (!root.contains(event.target)) return;
    var first = event.target.form && Array.from(event.target.form.elements).find(function (field) {
      return field.willValidate && !field.validity.valid;
    });
    reveal(first || event.target);
  }, true);
  document.addEventListener('click', function (event) {
    var link = event.target.closest('a[href^="#"]');
    if (link && !list.contains(link)) reveal(document.getElementById(link.hash.slice(1)));
  });
  function revealHash() {
    reveal(document.getElementById(location.hash.slice(1)));
    sync();
  }
  window.addEventListener('hashchange', revealHash);
  // Layout editing rebuilds rows; reapply the view without changing its model.
  new MutationObserver(sync).observe(layout, {childList: true, subtree: true, attributes: true, attributeFilter: ['class']});
  sync();
  var invalid = root.querySelector('[aria-invalid="true"]');
  if (invalid) reveal(invalid);
  var query = new URLSearchParams(location.search);
  var err = query.get('err') || '';
  if (/^(status|name|due|content_type|segmentation|pair_providers)$/.test(err)) select('project-settings');
  if (query.has('langs_added') || query.has('locale_added') || query.has('locale_removed')) select('project-languages');
  if (query.has('tm_imported') || query.has('tm_source_added') || query.has('tm_source_removed') || query.has('tm_source_absent')) select('project-memory');
  if (query.has('gl_entries') || query.has('gl_added') || query.has('glossary_pinned') || query.has('glossary_unpinned') || query.has('glossary_pin_absent') || query.has('glossary_cursor')) select('project-glossary');
  if (query.has('member_msg')) select('project-members');
  if (location.hash) revealHash();
})();
