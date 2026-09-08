/*
 * Bob the Tester dashboard — the only script on the page.
 *
 * Everything visible is rendered server-side by dashboard/render.py, so the
 * report is readable with JavaScript switched off entirely.  This file adds
 * three conveniences on top of that:
 *
 *   1. switching between runs (all runs are in the page, one is shown)
 *   2. remembering which run you were looking at across a reload
 *   3. auto-refresh, but only when the page is being served by serve.py —
 *      a file:// copy has nothing to poll and must not try
 */
(function () {
  "use strict";

  var KEY = "bob-the-tester:run";

  function show(runId) {
    var found = false;
    document.querySelectorAll("section.run").forEach(function (section) {
      var match = section.dataset.runId === runId;
      section.hidden = !match;
      found = found || match;
    });
    return found;
  }

  function initPicker() {
    var picker = document.getElementById("run-picker");
    if (!picker) return;

    // A remembered run may be gone (a fresh database, a reset demo), so fall
    // back to whatever the page rendered as current rather than showing an
    // empty page.
    var remembered = null;
    try { remembered = localStorage.getItem(KEY); } catch (e) { /* private mode */ }
    if (remembered && show(remembered)) {
      picker.value = remembered;
    } else {
      show(picker.value);
    }

    picker.addEventListener("change", function () {
      show(picker.value);
      try { localStorage.setItem(KEY, picker.value); } catch (e) { /* ignore */ }
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  /*
   * Poll the fingerprint of the database contents; reload when it moves.
   *
   * The badge this updates describes the PAGE, not the pipeline: "static
   * page" (opened from disk, never changes), "auto-refresh" (served, will
   * reload itself when the database moves), "server unreachable" (the
   * dashboard server went away).  Whether a run is actually in progress is
   * the Status tile's job, and it answers from the database.
   * The fingerprint is cheap to compute (row counts + last timestamps), so
   * polling never competes with a pipeline run for the database.
   */
  function initLiveRefresh() {
    if (!/^https?:$/.test(window.location.protocol)) return;

    var badge = document.getElementById("live-badge");
    var current = document.body.dataset.fingerprint || "";

    setInterval(function () {
      fetch("api/fingerprint", { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) return;
          if (badge) badge.textContent = "auto-refresh";
          if (current && data.fingerprint !== current) window.location.reload();
        })
        .catch(function () {
          if (badge) badge.textContent = "server unreachable";
        });
    }, 4000);
  }

  document.addEventListener("DOMContentLoaded", function () {
    initPicker();
    initLiveRefresh();
  });
})();
