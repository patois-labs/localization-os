/* Project automation page: show UTC times next to the reader's local time.
   Display only. The schedule itself is stored and computed in UTC. */
(function () {
  "use strict";
  function fmt(d, opts) {
    try {
      return d.toLocaleString([], opts);
    } catch (e) {
      return "";
    }
  }
  document.querySelectorAll("time.local-dt").forEach(function (el) {
    var raw = el.getAttribute("datetime");
    var d = new Date(raw);
    if (isNaN(d.getTime())) {
      return;
    }
    el.textContent = fmt(d, { dateStyle: "medium", timeStyle: "short" });
    el.title = raw + " (UTC)";
    // The text is now the reader's local time: relabel the "(UTC)" note next to it so it
    // does not claim a local time is UTC, and keep the UTC time beside it.
    var zone = el.nextElementSibling;
    if (zone && zone.classList && zone.classList.contains("local-dt-zone")) {
      var utc = fmt(d, { timeZone: "UTC", hour: "numeric", minute: "2-digit" });
      zone.textContent = utc ? "(your time; " + utc + " UTC)" : "(your time)";
    }
  });
  document.querySelectorAll(".utc-time").forEach(function (el) {
    var hm = (el.getAttribute("data-utc") || "").split(":");
    if (hm.length !== 2) {
      return;
    }
    // Convert on the date of the next run (else today), so daylight saving time is right.
    var ref = new Date(el.getAttribute("data-ref") || "");
    if (isNaN(ref.getTime())) {
      ref = new Date();
    }
    var d = new Date(
      Date.UTC(ref.getUTCFullYear(), ref.getUTCMonth(), ref.getUTCDate(), parseInt(hm[0], 10), parseInt(hm[1], 10))
    );
    var local = fmt(d, { hour: "numeric", minute: "2-digit" });
    if (local) {
      el.textContent = el.textContent + " (" + local + " your time)";
    }
  });
})();
