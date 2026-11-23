/*
 * console-reconnect.js — injected into the ttyd console page by
 * scripts/make-console-index.sh, next to console-wheel-fix.js.
 *
 * When the console's websocket closes, ttyd shows "Press ⏎ to Reconnect" and waits
 * for an Enter keydown in the terminal (ttyd 1.7.7: a one-shot terminal.onKey that
 * checks domEvent.key === "Enter"). Nothing else wakes it, so a click on the terminal,
 * any other key, or the mission page's key bar (which types through tmux, never this
 * socket) left the console dead until someone found Enter.
 *
 * So while that prompt is up, any of these sends ttyd the Enter it waits for:
 *   - a click / tap on this page, or any key other than Enter (Enter reaches ttyd
 *     itself — firing ours too would be harmless, but there is no need);
 *   - a "miss-claude:reconnect" message from the page framing the console (the mission
 *     page posts it on any key, click or focus there — CONSOLE_WAKE_JS in app.py).
 * The Enter is a synthetic keydown on xterm's own textarea, so it goes through exactly
 * the path a real one does. It can never type into Claude: it is only sent while the
 * prompt shows, and ttyd's sendData drops input unless the socket is OPEN. The prompt's
 * text is the state check — "Reconnecting..." (already on its way) does not match.
 */
(function () {
  var PROMPT = "to Reconnect";

  function asksForEnter(term) {
    var kids = term.element ? term.element.children : [];
    for (var i = 0; i < kids.length; i++) {
      if ((kids[i].textContent || "").indexOf(PROMPT) !== -1) return true;
    }
    return false;
  }

  function reconnect() {
    var term = window.term;
    if (!term || !term.textarea || !asksForEnter(term)) return;
    var ev = new KeyboardEvent("keydown", {key: "Enter", code: "Enter",
                                           bubbles: true, cancelable: true});
    // The constructor ignores keyCode, and xterm maps Enter by keyCode.
    Object.defineProperty(ev, "keyCode", {get: function () { return 13; }});
    Object.defineProperty(ev, "which", {get: function () { return 13; }});
    term.textarea.dispatchEvent(ev);
  }

  document.addEventListener("pointerdown", reconnect, true);
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") reconnect();
  }, true);
  window.addEventListener("message", function (e) {
    if (e.source === window.parent && e.data && e.data.type === "miss-claude:reconnect") {
      reconnect();
    }
  });
})();
