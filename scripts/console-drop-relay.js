/*
 * console-drop-relay.js — injected into the ttyd console page by
 * scripts/make-console-index.sh, next to console-wheel-fix.js.
 *
 * The mission page uploads files dropped onto it (MISSION_JS wireDrop in app.py), but
 * the console is a CROSS-ORIGIN iframe: a drag over the terminal is delivered to this
 * document, never to the dashboard, and nothing here takes it — so the browser does its
 * default and opens the file in place of the console.
 *
 * So, only when framed and only for drags that carry files:
 *   - dragenter / dragover tell the parent, which raises its drop overlay OVER this
 *     iframe; from then on the drag is the parent's, and the drop lands on the overlay.
 *     dragover is not optional: when the previous drag was dropped on that overlay, this
 *     frame never saw it leave, so Chrome skips dragenter on the NEXT drag here — the
 *     overlay then came up exactly every other time. dragover fires either way, and
 *     stops once the overlay covers the frame, so it posts only a handful of times.
 *   - dragover / drop are preventDefault()ed, so the browser never opens the file.
 *   - a drop that still lands here (released before the overlay came up) is handed to
 *     the parent as File objects (structured clone carries them) to upload the same way.
 * The parent accepts these messages only from its own console iframe (ev.source).
 * postMessage goes to "*": the parent's origin is the dashboard's port, which this page
 * doesn't know, and any page framing the console can already take a drop over its own
 * area directly, so the target restriction would protect nothing.
 *
 * Text drags, and the unframed fullscreen console tab, are left entirely alone.
 */
(function () {
  if (window.parent === window) return;

  function hasFiles(ev) {
    var t = ev.dataTransfer && ev.dataTransfer.types;
    return !!t && Array.prototype.indexOf.call(t, 'Files') !== -1;
  }

  function dragging(ev) {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'copy';
    window.parent.postMessage({type: 'miss-claude:file-drag'}, '*');
  }
  document.addEventListener('dragenter', dragging, true);
  document.addEventListener('dragover', dragging, true);

  document.addEventListener('drop', function (ev) {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    ev.stopPropagation();
    window.parent.postMessage({
      type: 'miss-claude:file-drop',
      files: Array.prototype.slice.call(ev.dataTransfer.files)
    }, '*');
  }, true);
})();
