/* bup web UI (NOT the on-wiki BooksUp gadget) — inline preview + Run for the
   worklist views.
   The "Run" button expands the proposed change in-row, with Add/Skip per
   citation and a Confirm & save that POSTs /apply. Requests are plain (no
   streaming / no server threads — Toolforge's uWSGI runs without thread
   support). If a request runs long (the live wiki read backing off on 429s),
   client-side staged messages reassure the user it's still working. No
   framework, no build step. */

(function () {
  "use strict";

  function rowOf(el) { return el.closest("tr"); }

  function expandAfter(tr) {
    var n = tr.nextElementSibling;
    return (n && n.classList.contains("bu-expand")) ? n : null;
  }

  function wikiOf(tr) {
    var run = tr.querySelector(".bu-run");
    return (run && run.getAttribute("data-wiki")) || "";
  }

  function qs(wiki) { return wiki ? ("?wiki=" + encodeURIComponent(wiki)) : ""; }

  // Show `initial` now, then rewrite the text at each [ms, text] step until
  // cancelled. Used while a request is in flight so a slow wiki read (retrying
  // on 429s) doesn't look like a hang. Returns a cancel function.
  function staged(setText, initial, steps) {
    setText(initial);
    var timers = steps.map(function (s) {
      return setTimeout(function () { setText(s[1]); }, s[0]);
    });
    return function () { timers.forEach(clearTimeout); };
  }

  // Toggle the inline preview for a row's Run button.
  function toggleRun(run) {
    var tr = rowOf(run);
    var open = expandAfter(tr);
    if (open) { open.remove(); run.classList.remove("bu-open"); return; }

    var id = run.getAttribute("data-id");
    var cols = tr.children.length;
    var ex = document.createElement("tr");
    ex.className = "bu-expand";
    ex.innerHTML = '<td colspan="' + cols + '"><div class="bu-loading"></div></td>';
    tr.after(ex);
    run.classList.add("bu-open");

    var cell = ex.firstElementChild;
    var setMsg = function (t) {
      var l = cell.querySelector(".bu-loading");
      if (l) l.textContent = t;
    };
    var cancel = staged(setMsg, "Loading preview…", [
      [8000, "Still loading — the wiki API may be busy…"],
      [25000, "Still working — retrying…"]
    ]);

    fetch("/preview-fragment/" + id + qs(wikiOf(tr)), {
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" }
    })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) { cancel(); cell.innerHTML = html; })
      .catch(function (err) {
        cancel();
        cell.innerHTML = '<div class="bu-error">Could not load preview: ' +
                         err.message + "</div>";
      });
  }

  function closePanel(btn) {
    var ex = btn.closest("tr.bu-expand");
    if (!ex) return;
    var dataRow = ex.previousElementSibling;
    ex.remove();
    if (dataRow) {
      var run = dataRow.querySelector(".bu-run");
      if (run) run.classList.remove("bu-open");
    }
  }

  // Replace the data row's action cell with a result message (+ optional [diff]
  // link to the edit).
  function finishRow(dataRow, ex, msg, cls, diffUrl) {
    if (ex) ex.remove();
    if (!dataRow) return;
    var run = dataRow.querySelector(".bu-run");
    if (run) run.classList.remove("bu-open");
    dataRow.classList.add("bu-done");
    var cell = dataRow.lastElementChild;
    if (cell) {
      var html = '<span class="bu-done-msg ' + (cls || "") + '">' + msg + "</span>";
      if (diffUrl) {
        html += ' <a class="bu-difflink" href="' + diffUrl +
                '" target="_blank" rel="noopener noreferrer">[diff]</a>';
      }
      cell.innerHTML = html;
    }
  }

  // Add (default) / Skip toggle for a single proposed change. Skip grays the
  // item out and excludes it from the save (mirrors the BooksUp gadget).
  function setSkip(btn, skipped) {
    var li = btn.closest("li.bu-cite");
    if (!li) return;
    var add = li.querySelector(".bu-add");
    var skip = li.querySelector(".bu-skip");
    li.classList.toggle("bu-skipped", skipped);
    add.classList.toggle("bu-on", !skipped);
    skip.classList.toggle("bu-on", skipped);
  }

  function doApply(btn) {
    var id = btn.getAttribute("data-id");
    var bar = btn.parentNode;
    var status = bar.querySelector(".bu-confirm-status");
    var cancelBtn = bar.querySelector(".bu-cancel");
    var ex = btn.closest("tr.bu-expand");
    var dataRow = ex ? ex.previousElementSibling : null;
    var panel = btn.closest(".bu-preview");

    // Only the non-skipped changes (by citation index) are applied.
    var items = panel.querySelectorAll("li.bu-cite:not(.bu-skipped)");
    var indices = Array.prototype.map.call(items, function (li) {
      return parseInt(li.getAttribute("data-i"), 10);
    });
    if (!indices.length) {
      status.textContent = "Nothing to add (all skipped).";
      return;
    }

    btn.disabled = true;
    if (cancelBtn) cancelBtn.disabled = true;
    function reenable() {
      btn.disabled = false;
      if (cancelBtn) cancelBtn.disabled = false;
    }

    var cancelMsg = staged(function (t) { status.textContent = t; }, "Saving…", [
      [8000, "Still saving — the wiki API may be busy…"],
      [25000, "Still working — retrying…"]
    ]);

    fetch("/apply/" + id + qs(dataRow ? wikiOf(dataRow) : ""), {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "fetch"
      },
      body: JSON.stringify({ indices: indices })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        cancelMsg();
        if (res.status === "ok") {
          var n = res.count;
          finishRow(dataRow, ex, "✓ Added " + n + " link" + (n === 1 ? "" : "s"),
                    "bu-ok", res.diff_url);
        } else if (res.status === "none") {
          finishRow(dataRow, ex, "No changes remained — removed", "bu-warn");
        } else {
          status.textContent = "Error saving — please try again.";
          reenable();
        }
      })
      .catch(function (err) {
        cancelMsg();
        status.textContent = "Error: " + err.message;
        reenable();
      });
  }

  // One delegated listener handles Run / Cancel / Add / Skip / Confirm.
  document.addEventListener("click", function (e) {
    var run = e.target.closest(".bu-run");
    if (run) { e.preventDefault(); toggleRun(run); return; }

    var cancel = e.target.closest(".bu-cancel");
    if (cancel) { e.preventDefault(); closePanel(cancel); return; }

    var add = e.target.closest(".bu-add");
    if (add) { e.preventDefault(); setSkip(add, false); return; }

    var skip = e.target.closest(".bu-skip");
    if (skip) { e.preventDefault(); setSkip(skip, true); return; }

    var confirm = e.target.closest(".bu-confirm");
    if (confirm) { e.preventDefault(); doApply(confirm); return; }
  });
})();
