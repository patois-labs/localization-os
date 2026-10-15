/* Issue #593: an export button that a delivery check refuses opens a dialog on the SAME page
   instead of showing raw JSON. Progressive enhancement: any link or GET form marked
   data-export-dialog is fetched with `X-Export-Dialog: 1`; a file comes back as a download,
   a refusal comes back as {ok:false, refusal:{...}} and is shown in a native modal <dialog>
   (focus trapped by the browser, Escape closes, focus returns to the control). Without JS
   the link still works and the server answers a browser with its own refusal page.
   All text is set with textContent; nothing from the response is parsed as HTML. */
(function () {
  'use strict';
  if (!window.fetch || !window.HTMLDialogElement) { return; }
  var dialog = null;
  var opener = null;

  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (text) { n.textContent = text; }
    return n;
  }

  function filenameFrom(resp, fallback) {
    var cd = resp.headers.get('Content-Disposition') || '';
    var star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
    if (star) { try { return decodeURIComponent(star[1]); } catch (e) { /* fall through */ } }
    var plain = /filename="?([^";]+)"?/i.exec(cd);
    return plain ? plain[1] : fallback;
  }

  var busy = false;

  /* Screen-reader announcements. The page-level region exists from load (a region added
     together with its text is often not announced); the dialog has its own while open. */
  function pageLive() {
    var s = document.getElementById('export-page-live');
    if (!s) {
      s = el('p', { 'class': 'sr-only', id: 'export-page-live', role: 'status', 'aria-live': 'polite' });
      document.body.appendChild(s);
    }
    return s;
  }

  function say(msg) {
    var inDialog = document.getElementById('export-dialog-live');
    var s = (dialog && dialog.open && inDialog) ? inDialog : pageLive();
    s.textContent = '';
    window.setTimeout(function () { s.textContent = msg; }, 50);
  }

  function closeDialog() {
    if (dialog && dialog.open) { dialog.close(); }
  }

  function ensureDialog() {
    if (dialog) { return dialog; }
    dialog = el('dialog', {
      id: 'export-dialog', 'class': 'component-dialog', 'aria-labelledby': 'export-dialog-title',
      'data-component-dialog': ''
    });
    dialog.addEventListener('close', function () {
      if (opener && opener.isConnected) { opener.focus(); }
    });
    document.body.appendChild(dialog);
    return dialog;
  }

  function showRefusal(r, retryUrl) {
    var d = ensureDialog();
    d.textContent = '';
    pageLive().textContent = '';
    var head = el('div', { 'class': 'component-dialog-heading' });
    head.appendChild(el('h2', { id: 'export-dialog-title', tabindex: '-1' }, r.headline));
    d.appendChild(head);
    d.appendChild(el('p', { id: 'export-dialog-plain' }, r.plain));
    d.appendChild(el('p', { id: 'export-dialog-why' }, r.why));

    if (r.items && r.items.length) {
      d.appendChild(el('h3', { id: 'export-dialog-items-h' }, r.items_heading));
      var ul = el('ul', { id: 'export-dialog-items', 'aria-labelledby': 'export-dialog-items-h' });
      r.items.forEach(function (it) {
        ul.appendChild(el('li', {}, it.label + ': ' + it.problem));
      });
      d.appendChild(ul);
      if (r.items_more) {
        d.appendChild(el('p', { 'class': 'muted', id: 'export-dialog-more' },
          'Showing the first ' + r.items.length + ' of ' + r.items_total + '. ' +
          (r.review_url ? 'Open Post Editor' : (r.paragraphs_url ? 'View paragraphs' :
            (r.empties_url ? 'Review empty targets' : 'The full list'))) + ' shows the rest.'));
      }
    }

    d.appendChild(el('h3', { id: 'export-dialog-steps-h' }, 'What to do'));
    var ol = el('ol', { id: 'export-dialog-steps' });
    (r.steps || []).forEach(function (s) { ol.appendChild(el('li', {}, s)); });
    d.appendChild(ol);

    var actions = el('div', { 'class': 'row-actions', id: 'export-dialog-actions', role: 'group', 'aria-label': 'Actions' });
    if (r.review_url) {
      actions.appendChild(el('a', { 'class': 'btn-primary', id: 'export-act-review', href: r.review_url }, 'Open Post Editor'));
    }
    if (r.paragraphs_url) {
      actions.appendChild(el('a', { 'class': 'btn-primary', id: 'export-act-paragraphs', href: r.paragraphs_url }, 'View paragraphs'));
    }
    if (r.empties_url) {
      actions.appendChild(el('a', { 'class': 'btn-primary', id: 'export-act-empties', href: r.empties_url }, 'Review empty targets'));
    }
    if (r.export_anyway_url) {
      // The risky action is deliberately the quiet button; the safe fixes above stay primary.
      var any = el('button', { type: 'button', 'class': 'btn-secondary', id: 'export-act-anyway' }, r.anyway_label);
      any.addEventListener('click', function () {
        run(r.export_anyway_url, null, true);
      });
      actions.appendChild(any);
    }
    var close = el('button', { type: 'button', 'class': 'ghost', id: 'export-act-close' }, 'Close');
    close.addEventListener('click', closeDialog);
    actions.appendChild(close);
    d.appendChild(actions);
    if (r.export_anyway_url && r.anyway_note) {
      d.appendChild(el('p', { 'class': 'muted', id: 'export-anyway-note' }, r.anyway_note));
    } else if (r.override_refused) {
      d.appendChild(el('p', { 'class': 'muted', id: 'export-anyway-note' },
        'Your role cannot override this check. Ask someone with edit rights (translator or above).'));
    } else if (r.not_bypassable) {
      d.appendChild(el('p', { 'class': 'muted', id: 'export-nobypass-note' },
        'This check cannot be bypassed. Once it is resolved it clears by itself and the export works.'));
    }
    d.appendChild(el('p', { 'class': 'sr-only', id: 'export-dialog-live', role: 'status', 'aria-live': 'polite' }));
    if (!d.open) { d.showModal(); }
    var t = document.getElementById('export-dialog-title');
    if (t) { t.focus(); }
  }

  function download(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = el('a', { href: url, download: name });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
  }

  function run(url, control, fromDialog) {
    if (busy) { return; }
    busy = true;
    say('Preparing the export.');
    if (control && control.setAttribute) { control.setAttribute('aria-busy', 'true'); }
    function done() {
      busy = false;
      if (control && control.removeAttribute) { control.removeAttribute('aria-busy'); }
    }
    fetch(url, {
      credentials: 'same-origin',
      headers: { 'X-Export-Dialog': '1', 'Accept': 'application/json, */*;q=0.5' }
    }).then(function (resp) {
      var type = resp.headers.get('Content-Type') || '';
      if (type.indexOf('application/json') === 0) {
        return resp.json().then(function (body) {
          if (body && body.refusal) { showRefusal(body.refusal, url); return; }
          // Some other JSON refusal: let the browser show it the normal way.
          window.location.href = url;
        });
      }
      if (!resp.ok || type.indexOf('text/html') === 0) { window.location.href = url; return; }
      return resp.blob().then(function (blob) {
        var name = filenameFrom(resp, 'export');
        var left = parseInt(resp.headers.get('X-Export-Quarantined') || '0', 10);
        var subs = parseInt(resp.headers.get('X-Export-Substituted') || '0', 10);
        if (fromDialog) { closeDialog(); }
        download(blob, name);
        var note = 'Export ready: ' + name + '.';
        if (left > 0) { note += ' ' + left + ' document(s) were left out; the ZIP lists them in _QUARANTINED.txt.'; }
        if (subs > 0) { note += ' ' + subs + ' item(s) were delivered as source text.'; }
        say(note);
      });
    }).then(done, function () { done(); window.location.href = url; });
  }

  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-export-dialog]');
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) { return; }
    e.preventDefault();
    opener = a;
    run(a.href, a, false);
  });

  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (!f.matches || !f.matches('form[data-export-dialog]') || e.defaultPrevented) { return; }
    e.preventDefault();
    opener = e.submitter || f.querySelector('button[type="submit"]');
    var q = new URLSearchParams(new FormData(f)).toString();
    run(f.action + (q ? '?' + q : ''), f, false);
  }, true);
})();
