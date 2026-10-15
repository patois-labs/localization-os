/* Help page scroll regions: only regions whose content really overflows become
   keyboard focusable (tabindex 0), so keyboard users do not tab through dozens of
   regions that have nothing to scroll. Presentation only; recomputed on resize.
   Regions are looked up on every pass: some Help sections (host memory settings)
   sit after this script tag and do not exist yet when it first runs. */
(function () {
  function sync() {
    var regions = document.querySelectorAll('[data-scroll-region]');
    for (var i = 0; i < regions.length; i++) {
      var el = regions[i];
      if (el.scrollWidth > el.clientWidth + 1) { el.setAttribute('tabindex', '0'); }
      else { el.removeAttribute('tabindex'); }
    }
  }
  // The scan reads layout for every region on the largest page: coalesce the load
  // events and resize bursts into one pass per frame.
  var queued = false;
  function schedule() {
    if (queued) { return; }
    queued = true;
    var run = function () { queued = false; sync(); };
    if (window.requestAnimationFrame) { window.requestAnimationFrame(run); } else { setTimeout(run, 0); }
  }
  sync();
  document.addEventListener('DOMContentLoaded', schedule);
  window.addEventListener('resize', schedule);
  window.addEventListener('load', schedule);
})();
