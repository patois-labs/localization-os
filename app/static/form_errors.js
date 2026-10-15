/* Page errors (a11y T6, WCAG 3.3.1 / 3.3.3).
 *
 * 1. On load, move focus to the server-rendered error summary (#page-error, role="alert",
 *    tabindex="-1") so a screen reader reads it, unless a modal dialog is already open
 *    (the New project dialog focuses its own error). This also overrides the login
 *    page's autofocus on the username field. When the field the summary names is below
 *    the fold, focus goes to that field instead, with the summary sentence added to its
 *    aria-describedby (#564: the page lands on the form, and the focus stays on screen).
 * 2. The summary's "Go to the field" link focuses the field (a fragment jump alone does
 *    not move focus into a form control); a link whose target is not on this page is
 *    removed rather than left dead.
 * 3. LocA11y.fieldError(input, message, msgEl) / LocA11y.clearFieldError(input, msgEl)
 *    for client-side checks: aria-invalid, a role="alert" message, and that message's id
 *    merged into the field's aria-describedby.
 */
(function () {
  "use strict";

  function modalOpen() {
    return !!document.querySelector("dialog[open], [aria-modal='true']:not([hidden])");
  }

  function describedBy(input) {
    return (input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
  }

  function messageFor(input, msgEl) {
    if (msgEl) { return msgEl; }
    var id = (input.id || "field") + "-client-error";
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement("span");
      el.id = id;
      el.className = "error-inline";
      input.insertAdjacentElement("afterend", el);
    }
    return el;
  }

  function fieldError(input, message, msgEl) {
    if (!input) { return null; }
    var el = messageFor(input, msgEl);
    el.setAttribute("role", "alert");
    el.textContent = message;
    el.hidden = false;
    input.setAttribute("aria-invalid", "true");
    var ids = describedBy(input);
    if (ids.indexOf(el.id) < 0) { ids.push(el.id); }
    input.setAttribute("aria-describedby", ids.join(" "));
    return el;
  }

  function clearFieldError(input, msgEl) {
    if (!input) { return; }
    var el = msgEl || document.getElementById((input.id || "field") + "-client-error");
    if (el) {
      el.hidden = true;
      el.textContent = "";
      var ids = describedBy(input).filter(function (id) { return id !== el.id; });
      if (ids.length) { input.setAttribute("aria-describedby", ids.join(" ")); }
      else { input.removeAttribute("aria-describedby"); }
    }
    // A field the server marked (the page error targets it) stays invalid until the
    // next submit; a purely client-side error clears here.
    var page = document.getElementById("page-error");
    if (!page || !input.id || page.getAttribute("data-error-field") !== input.id) {
      input.removeAttribute("aria-invalid");
    }
  }

  var FOCUSABLE = "a[href], area[href], button, input, select, textarea, iframe, " +
    "[tabindex], [contenteditable]";

  function wireSummary(summary) {
    Array.prototype.forEach.call(summary.querySelectorAll("a[href^='#']"), function (a) {
      var id = decodeURIComponent(a.getAttribute("href").slice(1));
      var target = id && document.getElementById(id);
      if (!target) {
        var p = a.closest("p");
        (p || a).remove();
        return;
      }
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        var details = target.closest("details");
        if (details && !details.open) { details.open = true; }
        // Decide focusability by what the element IS, not by its tag or tabIndex: an
        // <a> without href reports tabIndex 0 yet cannot take focus. The handler cancels
        // the native fragment jump, so a target that is not focusable must be made so.
        if (!target.matches(FOCUSABLE)) {
          target.setAttribute("tabindex", "-1");
        }
        target.focus();
        target.scrollIntoView({ block: "center" });
      });
    });
  }

  window.LocA11y = window.LocA11y || {};
  window.LocA11y.fieldError = fieldError;
  window.LocA11y.clearFieldError = clearFieldError;

  var summary = document.getElementById("page-error");
  if (!summary) { return; }
  wireSummary(summary);
  if (!modalOpen()) {
    // Focus the summary so it is read out. But when the field it names sits below the
    // fold (the upload form on a long project page), land on that field instead of
    // leaving the person a screen above it (#564 m9). Focus moves to the FIELD, not
    // just the scroll: a focused summary scrolled off the top would hide the focus
    // indicator (WCAG 2.2 SC 2.4.11). The summary sentence joins the field's
    // description, so it is still read out with the field.
    var fieldId = summary.getAttribute("data-error-field");
    var field = fieldId ? document.getElementById(fieldId) : null;
    var far = !!field && field.matches(FOCUSABLE) && !field.disabled &&
      field.getClientRects().length > 0 &&
      field.getBoundingClientRect().top > window.innerHeight - 80;
    if (far) {
      var msg = document.getElementById(summary.id + "-msg");
      var ids = describedBy(field);
      if (msg && ids.indexOf(msg.id) < 0) {
        ids.push(msg.id);
        field.setAttribute("aria-describedby", ids.join(" "));
      }
      field.focus({ preventScroll: true });
      if (field.scrollIntoView) { field.scrollIntoView({ block: "center" }); }
    } else {
      summary.focus();
    }
  }
})();
