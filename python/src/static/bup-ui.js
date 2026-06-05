/* bup web UI (NOT the on-wiki BooksUp gadget) — inline preview + Run for the
   worklist views.
   The "Run" button expands the proposed change in-row, with Add/Skip per
   citation and a Confirm & save that POSTs /apply. /preview-fragment and /apply
   stream Server-Sent Events: 'retry' events (with attempt/wait/reason) while the
   live wiki read backs off, then a final 'result' (or 'error') event. No
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

  function retryMessage(p) {
    return "The wiki API is busy (" + p.reason + ") — retrying… (attempt " +
           p.attempt + ", waiting " + p.wait + "s)";
  }

  // Parse one "event: …\ndata: …" SSE block and dispatch it.
  function dispatchFrame(block, onRetry, onResult, onError) {
    var ev = "message", data = "";
    block.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) ev = line.slice(6).trim();
      else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
    });
    if (!data) return;
    var payload;
    try { payload = JSON.parse(data); } catch (e) { return; }
    if (ev === "retry") onRetry(payload);
    else if (ev === "result") onResult(payload);
    else if (ev === "error") onError(payload.message || "error");
  }

  // Issue a request whose response is an SSE stream of retry/result/error
  // events. Degrades gracefully for non-streaming early returns (missing row,
  // not-logged-in) and for browsers without a streaming body reader.
  function streamRequest(url, opts, onRetry, onResult, onError) {
    opts = opts || {};
    opts.credentials = "same-origin";
    fetch(url, opts).then(function (r) {
      var ct = r.headers.get("Content-Type") || "";
      if (ct.indexOf("text/event-stream") === -1) {
        return r.text().then(function (t) {
          if (ct.indexOf("application/json") !== -1) {
            try { onResult(JSON.parse(t)); return; } catch (e) {}
          }
          if (!r.ok) { onError("HTTP " + r.status); return; }
          onResult({ html: t });            // non-streaming HTML (e.g. empty)
        });
      }
      if (!r.body || !r.body.getReader) {     // no streaming reader available
        return r.text().then(function (t) {
          t.split("\n\n").forEach(function (b) {
            dispatchFrame(b, onRetry, onResult, onError);
          });
        });
      }
      var reader = r.body.getReader();
      var dec = new TextDecoder();
      var buf = "";
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) {
            if (buf.trim()) dispatchFrame(buf, onRetry, onResult, onError);
            return;
          }
          buf += dec.decode(res.value, { stream: true });
          var parts = buf.split("\n\n");
          buf = parts.pop();                  // keep trailing partial frame
          parts.forEach(function (b) {
            dispatchFrame(b, onRetry, onResult, onError);
          });
          return pump();
        });
      }
      return pump();
    }).catch(function (e) { onError(e.message); });
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
    ex.innerHTML = '<td colspan="' + cols + '">' +
                   '<div class="bu-loading">Loading preview…</div></td>';
    tr.after(ex);
    run.classList.add("bu-open");

    var cell = ex.firstElementChild;
    streamRequest(
      "/preview-fragment/" + id + qs(wikiOf(tr)),
      { headers: { "X-Requested-With": "fetch" } },
      function onRetry(p) {
        var l = cell.querySelector(".bu-loading");
        if (l) l.textContent = retryMessage(p);
      },
      function onResult(d) { cell.innerHTML = (d.html != null) ? d.html : ""; },
      function onError(m) {
        cell.innerHTML = '<div class="bu-error">Could not load preview: ' +
                         m + "</div>";
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

  // Replace the data row's action cell with a result message and dim the row.
  function finishRow(dataRow, ex, msg, cls) {
    if (ex) ex.remove();
    if (!dataRow) return;
    var run = dataRow.querySelector(".bu-run");
    if (run) run.classList.remove("bu-open");
    dataRow.classList.add("bu-done");
    var cell = dataRow.lastElementChild;
    if (cell) {
      cell.innerHTML = '<span class="bu-done-msg ' + (cls || "") + '">' +
                       msg + "</span>";
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
    var cancel = bar.querySelector(".bu-cancel");
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
    if (cancel) cancel.disabled = true;
    status.textContent = "Saving…";

    function reenable() {
      btn.disabled = false;
      if (cancel) cancel.disabled = false;
    }

    streamRequest(
      "/apply/" + id + qs(dataRow ? wikiOf(dataRow) : ""),
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "fetch"
        },
        body: JSON.stringify({ indices: indices })
      },
      function onRetry(p) { status.textContent = retryMessage(p); },
      function onResult(res) {
        if (res.status === "ok") {
          var n = res.count;
          finishRow(dataRow, ex, "✓ Added " + n + " link" + (n === 1 ? "" : "s"),
                    "bu-ok");
        } else if (res.status === "none") {
          finishRow(dataRow, ex, "No changes remained — removed", "bu-warn");
        } else {
          status.textContent = "Error saving — please try again.";
          reenable();
        }
      },
      function onError(m) { status.textContent = "Error: " + m; reenable(); });
  }

  // One delegated listener handles Run / Cancel / Confirm (incl. injected nodes).
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
