/*
 * console-wheel-fix.js — injected into the ttyd console page by
 * scripts/make-console-index.sh (served via ttyd's --index).
 *
 * THE BUG: Claude runs on the terminal's ALTERNATE screen, which by definition has no
 * scrollback. xterm.js's wheel handler special-cases exactly that and turns a wheel
 * gesture into CURSOR KEYS:
 *
 *     if (!this.buffer.hasScrollback) {
 *       const n = this.viewport.getLinesScrolled(ev); if (n === 0) return;
 *       const seq = ESC + (applicationCursorKeys ? 'O' : '[') + (ev.deltaY < 0 ? 'A' : 'B');
 *       this.coreService.triggerDataEvent(seq.repeat(n), true);
 *       return this.cancel(ev, true);            // <- also preventDefault()s the gesture
 *     }
 *
 * So one two-finger trackpad scroll over the console sends a burst of ESC [ A / ESC [ B
 * to the PTY and Claude walks its prompt history — a scroll gesture silently rewrites the
 * prompt — while cancel()'s preventDefault() stops the dashboard page from scrolling.
 *
 * THE FIX: xterm.js has a first-class hook for this. attachCustomWheelEventHandler() is
 * consulted at the top of that handler, and returning false makes xterm bail out BEFORE
 * both the arrow translation and the cancel(). Nothing is preventDefault()ed, so the
 * gesture keeps its normal browser behaviour: the console iframe has nothing to scroll,
 * so the browser chains it to the dashboard page around it, which scrolls as usual.
 *
 * We return false ONLY in the exact state that produces the arrow keys, so the rest of
 * the terminal is untouched:
 *   - normal buffer (a shell after Claude exits) -> xterm's own wheel scrollback still
 *     works; blanket-ignoring wheel events breaks it, since xterm scrolls the viewport
 *     itself rather than relying on the element's native scrolling.
 *   - an app that turned mouse reporting ON (vim, htop; Claude does not — the consoles
 *     export CLAUDE_CODE_DISABLE_MOUSE=1) still gets its wheel-as-mouse events.
 * Keyboard Up/Down, clicks, selection, copy/paste, focus and resize never went through
 * this handler at all.
 *
 * Assigning the handler is idempotent — one handler slot on the Terminal, not an
 * addEventListener — so re-running attach() can never register a duplicate and there is
 * nothing to unregister. The poll exists because ttyd constructs the Terminal after this
 * script runs and publishes it as window.term; the identity check also re-arms the hook
 * if ttyd ever replaces the instance.
 */
(function () {
  function core(term) {
    return term._core || term;   // the public Terminal delegates to _core
  }

  /* True when this wheel event is about to be translated into cursor keys. Mirrors
     xterm's own two conditions; falls back to the public buffer type if the internal
     hasScrollback ever moves. */
  function wouldTypeArrowKeys(term) {
    var c = core(term);
    if (c.coreMouseService && c.coreMouseService.areMouseEventsActive) {
      return false;              // mouse reporting is on: wheel goes to the app, not to arrows
    }
    if (c.buffer && typeof c.buffer.hasScrollback === 'boolean') {
      return !c.buffer.hasScrollback;
    }
    return !!(term.buffer && term.buffer.active &&
              term.buffer.active.type === 'alternate');
  }

  var armed = null;
  function attach() {
    var term = window.term;
    if (!term || term === armed ||
        typeof term.attachCustomWheelEventHandler !== 'function') {
      return;
    }
    term.attachCustomWheelEventHandler(function () {
      /* false = "xterm, leave this event alone" (and, crucially, do NOT preventDefault
         it, so the page still scrolls); anything else = xterm's normal handling. */
      return !wouldTypeArrowKeys(term);
    });
    armed = term;
  }

  attach();
  setInterval(attach, 250);
})();
