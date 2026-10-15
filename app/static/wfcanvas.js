// Workflow graph: drag to reorder (SPEC-workflow-canvas Phase 1).
//
// Hand-rolled Pointer Events on the SERVER-RENDERED SVG the wf_graph macro emits in
// editable mode. No library, no CDN, and deliberately NO LAYOUT MATH: every drop target
// is a rectangle the server already drew (.wfg-slot with data-gap, .wfg-band with
// data-group), so this file only (a) hit-tests those rectangles, (b) POSTs the drop, and
// (c) shows the server's answer. If the geometry ever changes, it changes in ONE place -
// the macro.
//
// Contract with the markup:
//   .wf-canvas[data-move-url][data-csrf]   wrapper the builder renders around the graph
//   svg.wfg-editable                        the editable graph
//   g.wfg-node[data-step-id][data-position] one per step
//     [data-groupable]                      present iff this step could ever be a lane member
//     [data-group]                          present iff this step is currently IN a group
//   g.wfg-grip                              inside a node; a drag starts HERE and nowhere else
//   rect.wfg-slot[data-gap]                 boundary k: "insert above the step now at index k"
//   line.wfg-slot-mark[data-gap-mark]       the insertion marker for that boundary
//   rect.wfg-band[data-group][data-gap]     a parallel group's lane band (a "run together" drop)
//
// data-gap is an index into the CURRENT list; the seam takes the position the step ENDS
// UP at. Dragging DOWNWARD past your own row consumes one slot, so the final position is
// gap - 1 in that case. That single comparison is the only arithmetic here.
//
// LANE MEMBERSHIP RIDES THE SAME POST. A band drop sends the band's slug; dropping a step
// that is CURRENTLY in a group onto a plain slot sends parallel_group="" - that is how
// dragging OUT of a group works at all. The server applies the move and the slug change
// together and validates the composed result once, so there is no order of operations
// here to get wrong and no half-applied state to compensate for.
//
// Server authority: every drop POSTs immediately and the page reloads on success, so what
// you see afterwards is the server's graph, never an optimistic client guess. A refusal
// (400) is rendered VERBATIM in an inline notice and the node snaps back.
function initWfCanvas() {
  var wrap = document.querySelector('.wf-canvas[data-move-url]');
  if (!wrap) return;
  // Idempotence (the wfmodel.js pattern): the initializer can be reached more than once
  // (a re-run script tag, a future fragment swap). Wiring the same SVG twice would fire
  // two POSTs per drop.
  if (wrap.dataset.wfcanvasReady === '1') return;
  wrap.dataset.wfcanvasReady = '1';
  var svg = wrap.querySelector('svg.wfg-editable');
  if (!svg || !window.PointerEvent) return;
  var notice = wrap.querySelector('.wf-canvas-notice');
  var drag = null;
  // A drop is in flight: the POST has been sent and the page is about to reload. A
  // SECOND drag started in that window would compute its target position from the
  // PRE-move numbering the page is still showing, and land the step somewhere nobody
  // asked for (reproduced in review R2). Cleared on every outcome that does NOT reload.
  var busy = false;

  var SERVER_FAULT = 'Something went wrong on the server - your move may not have ' +
                     'been applied. Reload to see the current order.';

  function say(msg, isError) {
    if (!notice) return;
    notice.textContent = msg || '';
    notice.hidden = !msg;
    notice.className = 'wf-canvas-notice' + (isError ? ' error' : '');
  }

  function clearMarks() {
    var m = svg.querySelectorAll('.wfg-slot-mark');
    for (var i = 0; i < m.length; i++) m[i].setAttribute('opacity', '0');
    var b = svg.querySelectorAll('.wfg-band');
    for (var j = 0; j < b.length; j++) b[j].setAttribute('stroke', 'none');
  }

  function hits(el, x, y) {
    var r = el.getBoundingClientRect();
    return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  }

  // The target under the pointer: a lane band first (it sits inside the row bands and is
  // the more specific intent), else the tiled boundary slot. Bands are skipped entirely
  // for a step that could never be a member - the macro marks eligible steps with
  // data-groupable, which is the server's knowledge, not a rule re-implemented here.
  // A step already in a group is NOT skipped: dropping it on another group's band is a
  // legal cross-group move the composed seam performs in one call.
  function targetAt(x, y) {
    var i, els;
    if (drag.groupable) {
      els = svg.querySelectorAll('.wfg-band');
      for (i = 0; i < els.length; i++) {
        if (hits(els[i], x, y)) {
          return { el: els[i], gap: +els[i].getAttribute('data-gap'),
                   group: els[i].getAttribute('data-group') };
        }
      }
    }
    els = svg.querySelectorAll('.wfg-slot');
    for (i = 0; i < els.length; i++) {
      if (hits(els[i], x, y)) {
        return { el: els[i], gap: +els[i].getAttribute('data-gap'), group: null };
      }
    }
    return null;
  }

  function highlight(t) {
    clearMarks();
    if (!t) return;
    if (t.group !== null) {
      t.el.setAttribute('stroke', 'var(--success)');
      t.el.setAttribute('stroke-width', '2');
      return;
    }
    var m = svg.querySelector('.wfg-slot-mark[data-gap-mark="' + t.gap + '"]');
    if (m) m.setAttribute('opacity', '0.9');
  }

  function end(reset) {
    if (!drag) return;
    clearMarks();
    drag.node.removeAttribute('transform');
    drag.node.classList.remove('wfg-dragging');
    svg.classList.remove('wfg-drag-active');
    try { drag.node.releasePointerCapture(drag.id); } catch (e) { /* already gone */ }
    drag = null;
    if (reset) say('');
  }

  function post(stepId, position, group) {
    busy = true;
    var body = 'csrf_token=' + encodeURIComponent(wrap.dataset.csrf) +
               '&position=' + encodeURIComponent(position);
    if (group !== null && group !== undefined) {
      body += '&parallel_group=' + encodeURIComponent(group);
    }
    say('Saving...', false);
    fetch(wrap.dataset.moveUrl + '/' + stepId + '/move', {
      method: 'POST', credentials: 'same-origin',
      // The route answers a successful move with a 303 to the builder. Letting fetch
      // FOLLOW it downloads the whole page we are about to throw away and then reloads
      // it - the page renders twice. 'manual' turns the redirect into an opaque
      // response we never read, so exactly one render happens: the reload.
      redirect: 'manual',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded',
                 'X-CSRF-Token': wrap.dataset.csrf },
      body: body
    }).then(function (res) {
      // SUCCESS. The route content-negotiates: because this request carries
      // X-CSRF-Token it answers 200 with {"location": "..."} instead of a 303, and that
      // URL already carries the ?notice= the builder renders as a banner (propagation
      // counts, auto-pin, the TM-first sentence). Navigating to it -- rather than
      // reloading the current url -- is what makes the drag path say the same things the
      // move up / move down buttons say. A plain reload is the fallback for any response
      // that is not that shape, including the opaque 303 a non-negotiating build returns.
      if (res.ok) {
        return res.json().then(function (data) {
          if (data && data.location) { window.location = data.location; }
          else { window.location.reload(); }
        }).catch(function () { window.location.reload(); });
      }
      if (res.type === 'opaqueredirect') {
        window.location.reload();
        return null;
      }
      busy = false;
      // A SERVER FAULT IS NOT A REFUSAL. A 5xx means we do not know whether the move
      // landed; telling the user "that move was refused" would be a confident claim
      // about state we cannot see.
      if (res.status >= 500) { say(SERVER_FAULT, true); return null; }
      return res.text().then(function (html) { say(messageFrom(html), true); });
    }).catch(function () {
      busy = false;
      say(SERVER_FAULT, true);
    });
  }

  // The refusal message VERBATIM out of the standard error page. The canvas never
  // paraphrases a validator message (a paraphrase drifts; the real one names the step
  // and the rule), so this only strips the surrounding markup.
  function messageFrom(html) {
    var doc;
    try { doc = new DOMParser().parseFromString(html, 'text/html'); } catch (e) { doc = null; }
    // templates/error.html renders it as <p class="error">.
    var el = doc && (doc.querySelector('p.error') || doc.querySelector('.authcard p'));
    var txt = el ? (el.textContent || '').trim() : '';
    return txt || 'That move was refused. Reload the page to see the current order.';
  }

  svg.addEventListener('pointerdown', function (ev) {
    var grip = ev.target.closest ? ev.target.closest('.wfg-grip') : null;
    if (!grip || drag || busy) return;
    var node = grip.closest('.wfg-node');
    if (!node) return;
    ev.preventDefault();
    // Capture BEFORE committing to the drag: without it the pointer stream can be stolen
    // mid-gesture and we would sit in a drag state no pointerup ever ends. If capture
    // throws we abort cleanly rather than starting a drag we cannot finish, and point
    // the user at the affordance that always works.
    try {
      node.setPointerCapture(ev.pointerId);
    } catch (e) {
      say('Drag is unavailable here - use the move up / move down buttons in the ' +
          'step list.', false);
      return;
    }
    drag = { node: node, id: ev.pointerId, y0: ev.clientY,
             from: +node.getAttribute('data-position'),
             stepId: node.getAttribute('data-step-id'),
             groupable: node.hasAttribute('data-groupable'),
             group: node.getAttribute('data-group'), target: null };
    node.classList.add('wfg-dragging');
    svg.classList.add('wfg-drag-active');
    say('Drop the step where it should run. Press Escape to cancel.', false);
  });

  svg.addEventListener('pointermove', function (ev) {
    if (!drag || ev.pointerId !== drag.id) return;
    ev.preventDefault();
    // The ghost: a CSS transform on the node's own <g>. The graph's own units and the
    // screen's differ, so the pointer delta is scaled by the rendered/viewBox ratio -
    // read off the element the server sized, not recomputed from the step geometry.
    var box = svg.getBoundingClientRect();
    var vb = svg.viewBox.baseVal;
    var k = (vb && vb.height && box.height) ? (vb.height / box.height) : 1;
    drag.node.setAttribute('transform', 'translate(0,' + ((ev.clientY - drag.y0) * k) + ')');
    drag.target = targetAt(ev.clientX, ev.clientY);
    highlight(drag.target);
  });

  svg.addEventListener('pointerup', function (ev) {
    if (!drag || ev.pointerId !== drag.id) return;
    var t = drag.target || targetAt(ev.clientX, ev.clientY);
    var from = drag.from, stepId = drag.stepId, wasIn = drag.group;
    end(false);
    if (!t) { say(''); return; }
    var pos = t.gap > from ? t.gap - 1 : t.gap;
    // What to send for the slug:
    //   band drop            -> that band's slug (join a group, or switch groups)
    //   plain slot, in group -> "" (LEAVE the group; this is the drag-out)
    //   plain slot, no group -> omit entirely (nothing to say about membership)
    var group = t.group !== null ? t.group : (wasIn ? '' : null);
    if (pos === from && group === null) { say(''); return; }
    post(stepId, pos, group);
  });

  svg.addEventListener('pointercancel', function () { end(true); });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && drag) end(true);
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initWfCanvas);
} else {
  initWfCanvas();
}
