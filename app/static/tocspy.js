/* docs-toc scroll-spy (help page only, wired from help.html).

   Marks the TOC link for the section currently in view with `.on` - the same
   Terracotta-Wash active state the side-nav uses (styled in style.css; NO
   side-stripe) - plus aria-current="true" for assistive tech. Vanilla, no
   libraries.

   Guarantees:
   - Degrades silently: no IntersectionObserver, no .docs-toc, or no matching
     sections -> does nothing, the TOC stays hover-only (its pre-scroll-spy
     behavior). The links keep working as plain in-page anchors regardless.
   - Anchor click / keyboard activation still lands the section via the native
     `#hash` jump (and `.doc-section` already carries scroll-margin-top). The
     click handler marks the link immediately; the observer then re-syncs as the
     target settles into view.
   - Injects NO smooth scroll, so `prefers-reduced-motion` is honored by default
     (there is no motion to reduce here). */
(function () {
  var toc = document.querySelector('.docs-toc');
  if (!toc) { return; }
  var links = toc.querySelectorAll('a[href^="#"]');
  if (!links.length || !('IntersectionObserver' in window)) { return; }

  // id -> its TOC link, plus the observed section elements (only those a link
  // actually points at, so unrelated ids on the page are ignored).
  var linkFor = {};
  var sections = [];
  Array.prototype.forEach.call(links, function (a) {
    var id = (a.getAttribute('href') || '').slice(1);
    if (!id) { return; }
    var sec = document.getElementById(id);
    if (sec) { linkFor[id] = a; sections.push(sec); }
  });
  if (!sections.length) { return; }

  // Single activation path for EVERY trigger (observer, click, hash-on-load,
  // bottom-edge), so .on and aria-current can never drift apart.
  var current = null;
  function activate(id) {
    if (id === current || !linkFor[id]) { return; }
    if (current && linkFor[current]) {
      linkFor[current].classList.remove('on');
      linkFor[current].removeAttribute('aria-current');
    }
    linkFor[id].classList.add('on');
    linkFor[id].setAttribute('aria-current', 'true');
    current = id;
  }

  // Bottom-edge special case. The activation band is [80px, 30% of viewport]
  // from the top (the rootMargin below); a last section shorter than
  // (viewport - 30%·viewport) of remaining scroll can therefore NEVER get its
  // top into the band - e.g. at a 1000px viewport the band ends at 300px but a
  // fully-scrolled short last section tops out around ~345px, so scrolling
  // alone could never activate the final TOC link. When the page is scrolled
  // to (within 2px of) the bottom, the last resolvable section IS the current
  // one - activate it directly.
  function atBottom() {
    var d = document.scrollingElement || document.documentElement;
    return d.scrollTop + window.innerHeight >= d.scrollHeight - 2;
  }
  function activateLastIfAtBottom() {
    if (!atBottom()) { return false; }
    activate(sections[sections.length - 1].id);
    return true;
  }

  // A section counts as "current" while its top sits in the band just below the
  // sticky topbar (the -80px top inset mirrors .doc-section's scroll-margin-top).
  // The bottom inset keeps only the upper slice active so exactly one section
  // wins at a time. Among any that qualify, pick the one nearest the top.
  var live = {};
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) { live[e.target.id] = true; }
      else { delete live[e.target.id]; }
    });
    // Bottom edge wins over the nearest-top pick (see atBottom notes above).
    if (activateLastIfAtBottom()) { return; }
    var topId = null, topY = Infinity;
    Object.keys(live).forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) { return; }
      var y = el.getBoundingClientRect().top;
      if (y < topY) { topY = y; topId = id; }
    });
    // If nothing qualifies (e.g. between bands), keep the last active link lit
    // rather than clearing - avoids a flicker to no-selection while scrolling.
    if (topId) { activate(topId); }
  }, { rootMargin: '-80px 0px -70% 0px', threshold: 0 });

  sections.forEach(function (s) { io.observe(s); });

  // The observer only fires when a section EDGE crosses the band, and the final
  // pixels of scroll to the page bottom may cross none (the same geometry that
  // strands the last section outside the band) - so the bottom-edge check also
  // needs a scroll trigger. Passive + two comparisons; inert until the bottom.
  window.addEventListener('scroll', activateLastIfAtBottom, { passive: true });

  // Immediate feedback on click (before the scroll settles and the observer
  // catches up). Works for keyboard activation too (Enter fires a click).
  toc.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a[href^="#"]') : null;
    if (a) { activate((a.getAttribute('href') || '').slice(1)); }
  });

  // Honor a deep link on load (#section) even before the observer's first pass.
  if (location.hash && linkFor[location.hash.slice(1)]) {
    activate(location.hash.slice(1));
  }
})();
