// Vendor Studio — shared page JS (loaded on every base-template page).
// Exists so no inline event handler is needed (the CSP forbids inline script).
(function () {
  "use strict";
  // Persona role picker auto-submits its form on change (was an inline onchange).
  document.addEventListener("change", function (e) {
    if (e.target.matches("select[data-persona-role]")) {
      var form = e.target.closest("form");
      if (form) form.submit();
    }
  });
})();
