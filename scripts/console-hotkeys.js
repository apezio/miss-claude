/*
 * console-hotkeys.js — injected into the ttyd console page by
 * scripts/make-console-index.sh.
 *
 * The mission page binds Ctrl+Space (🎤 dictation on/off) and Ctrl+F (⤢ Full), but
 * while the terminal has focus every key lands in this cross-origin iframe, never on
 * that page. So this script hands those two keys up to the parent instead of typing
 * them at Claude.
 *
 * Only once the parent has said it handles them ("miss-claude:hotkeys" in answer to
 * our "miss-claude:hotkeys?"): a console opened in its own tab, or framed by a page
 * without the bindings (the canvas), keeps Ctrl+F / Ctrl+Space as terminal keys.
 * Capture phase on window, so it runs before xterm's own handler on its textarea.
 */
(function () {
  if (window.parent === window) return;
  var claimed = {};

  function which(e) {
    if (!e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return null;
    if (e.code === "Space" || e.key === " ") return "mic";
    if (e.code === "KeyF" || e.key === "f") return "full";
    return null;
  }

  window.addEventListener("keydown", function (e) {
    var k = which(e);
    if (!k || !claimed[k]) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    window.parent.postMessage({type: "miss-claude:hotkey", key: k}, "*");
  }, true);

  window.addEventListener("message", function (e) {
    var d = e.data;
    if (e.source !== window.parent || !d || d.type !== "miss-claude:hotkeys") return;
    claimed = {};
    (d.keys || []).forEach(function (k) { claimed[k] = true; });
  });
  window.parent.postMessage({type: "miss-claude:hotkeys?"}, "*");
})();
