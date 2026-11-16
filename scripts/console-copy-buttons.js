/*
 * console-copy-buttons.js — injected into the ttyd console page by
 * scripts/make-console-index.sh, next to console-wheel-fix.js and console-drop-relay.js.
 *
 * Always-visible Copy buttons over the parts of Claude's output people copy: quoted
 * text (drafts), tables, code blocks and `! command` lines. Claude's mouse capture stays
 * OFF (CLAUDE_CODE_DISABLE_MOUSE=1 keeps highlight-to-copy working): the buttons are
 * browser elements laid over xterm's screen, built from its buffer, and a click on one
 * never reaches Claude.
 *
 * What it reads is how Claude Code (checked on 2.1.325) draws them:
 *   quote  every row starts "▎ ", blank quote lines included
 *   table  box drawing: ┌─┬─┐ top, │ cell │ rows, ├─┼─┤ between rows, └─┴─┘ bottom
 *   cmd    a line starting "! " (CLAUDE.md's paste-into-the-prompt commands), plus
 *          its `\` continuation lines
 *   code   NO marker: indented, syntax-coloured lines, indistinguishable from prose.
 *          Only the source finds them: framed, this page asks the dashboard page
 *          around it for Claude's recent reply blocks (COPY_SRC_JS in app.py) and
 *          looks for their lines on screen.
 * A block whose source is on screen copies the source — exact, without the line breaks
 * Claude wrapped it at. Otherwise the screen text is copied, bars and borders removed
 * and wrapped lines rejoined. A table copies as tab-separated rows (pastes as columns).
 *
 * Only the rows on screen are read: Claude pages its own conversation, so older blocks
 * get their buttons when PageUp brings them into view. A Claude Code update that draws
 * these differently costs buttons, nothing else. The unframed fullscreen tab has no
 * page to ask, so it gets no code-block buttons.
 */
(function () {
  var QUIET_MS = 1000;      // terminal quiet before asking the framing page for sources
  var ASK_MIN_MS = 3000;    // and never more often than this
  var BTN_COLS = 6;         // room a button needs, in terminal columns
  var QUOTE = /^(\s*)▎( |$)/;
  var CMD = /^(\s*)! \S/;
  var BORDER = /^─{8,}/;    // Claude's prompt box: the conversation ends above it
  var JUMP = /\s+Jump to bottom \([^)]*\)\s*↓?$/;

  var term = null, layer = null, shown = {}, sources = [], srcGen = 0;
  var lastSig = "", scanT = 0, askT = 0, lastAsk = 0, askedRows = null;

  function key(s) { return s.replace(/[\s`*_~]/g, ""); }
  function rstrip(s) { return s.replace(/\s+$/, ""); }

  function screenRows() {
    var buf = term.buffer.active, out = [];
    for (var r = 0; r < term.rows; r++) {
      var line = buf.getLine(buf.viewportY + r);
      // Paged up, Claude writes its "Jump to bottom" hint over the last row it shows.
      out.push(line ? rstrip(line.translateToString(true)).replace(JUMP, "") : "");
    }
    return out;
  }

  /* Rejoin rows Claude wrapped: a row continues the previous one when the previous was
     too full for this row's first word to have fitted on it. `avail` = columns the
     text had; 2 of slack for Claude's right margin. */
  function unwrap(lines, avail) {
    var out = [], prev = "";
    lines.forEach(function (row, i) {
      var t = row.replace(/^\s+/, "");
      if (i && prev && t && prev.length + 1 + t.split(/\s/)[0].length > avail - 2) {
        out[out.length - 1] += " " + t;
      } else {
        out.push(row);
      }
      prev = row;
    });
    return out.join("\n");
  }

  function widest(rows, top, bottom) {
    var w = 0;
    for (var r = top; r <= bottom; r++) w = Math.max(w, rows[r].length);
    return w;
  }

  /* ---- source blocks (code) ------------------------------------------------- */

  /* Rows from `r` against source lines from `i0`; returns the row after the match, or
     -1. Blank source lines may or may not have a blank row. A block cut by the edge of
     the conversation — begun above the screen (i0 > 0), or running into the screen end
     or the prompt box — needs two whole lines on screen: one short line alone (a "}",
     an "END") matches too much. */
  function matchAt(keys, end, r, lines, i0) {
    var row = r, matched = 0;
    function cut() { return matched >= 2 ? row : -1; }
    for (var i = i0; i < lines.length; i++) {
      var want = key(lines[i]);
      if (row >= end) return cut();
      if (!want) { if (!keys[row]) row++; continue; }
      var got = "";
      while (got.length < want.length) {
        if (row >= end) return cut();
        if (!keys[row]) return -1;
        got += keys[row++];
        if (want.lastIndexOf(got, 0) !== 0) return -1;
      }
      matched++;
    }
    return !matched ? -1 : i0 ? cut() : row;
  }

  function codeRegions(rows) {
    var keys = rows.map(key), end = rows.length, out = [];
    for (var b = 0; b < rows.length; b++) if (BORDER.test(rows[b].trim())) { end = b; break; }
    sources.forEach(function (s) {
      if (s[0] !== "code" && s[0] !== "pre") return;
      var lines = String(s[1]).split("\n");
      while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
      if (!lines.length || key(lines.join("")).length < 2) return;
      for (var r = 0; r < end; r++) {
        // Claude indents its replies; a row at the left edge is the prompt's own echo
        // ("❯ …", or "! …" for a command the operator ran), never a reply's code.
        if (!keys[r] || !/^\s/.test(rows[r])) continue;
        // The block's first line, or — only at the top edge — a later one (the block
        // began above the screen).
        var stop = -1;
        for (var i0 = 0; i0 < (r === 0 ? lines.length : 1) && stop < 0; i0++) {
          stop = matchAt(keys, end, r, lines, i0);
        }
        if (stop < 0) continue;
        out.push({kind: "code", top: r, bottom: stop - 1, right: widest(rows, r, stop - 1),
                  text: String(s[1])});
        r = stop - 1;
      }
    });
    return out;
  }

  /* What the terminal shows has no **bold** / `code` markers, so a quote or table copied
     from its source drops them too: what you see is what you copy. */
  function plain(s) {
    return s.replace(/\*\*([^*\n]+)\*\*/g, "$1").replace(/__([^_\n]+)__/g, "$1")
            .replace(/`([^`\n]+)`/g, "$1");
  }

  function sourceText(kind, screenKey) {
    for (var i = sources.length - 1; i >= 0; i--) {
      var s = sources[i];
      if (s[0] !== kind) continue;
      if (kind === "table") {
        if (key(s[1].map(function (row) { return row.join(""); }).join("")) === screenKey) {
          return s[1].map(function (row) { return row.map(plain).join("\t"); }).join("\n");
        }
      } else if (key(String(s[1])) === screenKey) {
        return kind === "quote" ? plain(String(s[1])) : String(s[1]);
      }
    }
    return null;
  }

  /* ---- marked blocks: quotes, tables, commands ------------------------------ */

  function quoteRegions(rows, taken) {
    var out = [];
    for (var r = 0; r < rows.length; r++) {
      var m = QUOTE.exec(rows[r]);
      if (!m || taken[r]) continue;
      var ind = m[1].length, top = r, lines = [];
      while (r < rows.length && (m = QUOTE.exec(rows[r])) && m[1].length === ind) {
        lines.push(rows[r].slice(ind + 2));
        r++;
      }
      r--;
      var text = sourceText("quote", key(lines.join(""))) ||
                 unwrap(lines, term.cols - ind - 2).replace(/^\n+|\n+$/g, "");
      out.push({kind: "quote", top: top, bottom: r, right: widest(rows, top, r), text: text});
    }
    return out;
  }

  function tableRegions(rows, taken) {
    var out = [];
    for (var r = 0; r < rows.length; r++) {
      var m = /^(\s*)[┌├│└]/.exec(rows[r]);
      if (!m || taken[r]) continue;
      var ind = m[1].length, top = r, lines = [], ruled = false;
      while (r < rows.length && !taken[r] && /^[┌├│└]/.test(rows[r].slice(ind)) &&
             rows[r].slice(0, ind).trim() === "") {
        var l = rows[r].slice(ind);
        if (/^[┌├└][─┬┼┴]*[┐┤┘]$/.test(l) && /[┬┼┴]/.test(l)) ruled = true;
        lines.push(l);
        r++;
      }
      r--;
      if (!ruled) continue;              // a box drawing, not a table
      var cells = [], cur = null;
      lines.forEach(function (l) {
        if (l.charAt(0) !== "│") { if (cur) cells.push(cur); cur = null; return; }
        var parts = l.split("│").slice(1, -1).map(function (c) { return c.trim(); });
        if (!cur) { cur = parts; return; }
        parts.forEach(function (c, i) { if (c) cur[i] = cur[i] ? cur[i] + " " + c : c; });
      });
      if (cur) cells.push(cur);
      if (!cells.length) continue;
      var screenKey = key(cells.map(function (row) { return row.join(""); }).join(""));
      var text = sourceText("table", screenKey) ||
                 cells.map(function (row) { return row.join("\t"); }).join("\n");
      out.push({kind: "table", top: top, bottom: r, right: widest(rows, top, r), text: text});
    }
    return out;
  }

  function cmdRegions(rows) {
    var out = [];
    for (var r = 0; r < rows.length; r++) {
      var m = CMD.exec(rows[r]);
      if (!m) continue;
      var ind = m[1].length, top = r, text = rows[r].slice(ind);
      while (r + 1 < rows.length) {
        var next = rows[r + 1], t = next.replace(/^\s+/, "");
        if (!t || CMD.test(next)) break;
        if (/\\$/.test(rows[r])) text += "\n" + t;
        else if (rows[r].length + 1 + t.split(/\s/)[0].length > term.cols - 2) text += " " + t;
        else break;
        r++;
      }
      out.push({kind: "cmd", top: top, bottom: r, right: rows[top].length,
                text: sourceText("cmd", key(text)) || text});
    }
    return out;
  }

  /* ---- drawing -------------------------------------------------------------- */

  function blank(row, from, to) {
    return row !== undefined && row.slice(Math.max(0, from), to).trim() === "";
  }

  /* Where a block's button goes, as {row, col: its right edge}: the blank row above the
     block, flush with the block's right edge; else the top row's right end, past the
     text. A command's goes right after it, on its own row. */
  function spot(reg, rows) {
    var cols = term.cols;
    if (reg.kind === "cmd" && reg.right + 1 + BTN_COLS <= cols) {
      return {row: reg.top, left: reg.right + 1};
    }
    var right = Math.min(cols, Math.max(reg.right, BTN_COLS));
    if (reg.top > 0 && blank(rows[reg.top - 1], right - BTN_COLS, right)) {
      return {row: reg.top - 1, right: right};
    }
    return {row: reg.top, right: cols};
  }

  function copyText(text) {
    function legacy() {
      var ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
      document.body.appendChild(ta);
      ta.select(); ta.setSelectionRange(0, text.length);
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta);
      return ok ? Promise.resolve() : Promise.reject(new Error("copy refused"));
    }
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(legacy);
    }
    return legacy();
  }

  function button(reg) {
    var b = document.createElement("button");
    b.type = "button"; b.className = "mc-copy"; b.textContent = "Copy";
    b.title = "Copy this " + ({quote: "quoted text", table: "table (tab-separated)",
                                cmd: "command", code: "code block"})[reg.kind];
    // xterm starts a selection on mousedown; keep it (and focus) where it was.
    b.addEventListener("mousedown", function (e) { e.preventDefault(); e.stopPropagation(); });
    b.addEventListener("click", function (e) {
      e.preventDefault(); e.stopPropagation();
      copyText(reg.text).then(function () { flash(b, "Copied", true); },
                              function () { flash(b, "Copy failed", false); });
      if (term) term.focus();
    });
    return b;
  }

  function place(b, at, cellW, rowH, screenW) {
    b.style.top = (at.row * rowH + 1) + "px";
    b.style.height = Math.max(12, rowH - 2) + "px";
    b.style.left = at.left !== undefined ? (at.left * cellW) + "px" : "";
    b.style.right = at.left !== undefined ? "" : Math.max(0, screenW - at.right * cellW) + "px";
  }

  function flash(b, label, ok) {
    b.textContent = label;
    b.classList.toggle("ok", ok);
    setTimeout(function () { b.textContent = "Copy"; b.classList.remove("ok"); }, 1500);
  }

  function ensureLayer() {
    var screen = term.element && term.element.querySelector(".xterm-screen");
    if (!screen) return null;
    if (!layer || layer.parentNode !== screen) {
      layer = document.createElement("div");
      layer.className = "mc-copy-layer";
      screen.appendChild(layer);
    }
    return screen;
  }

  function scan() {
    scanT = 0;
    if (!term || !term.buffer) return;
    var screen = ensureLayer();
    if (!screen) return;
    var rows = screenRows();
    var text = rows.join("\n");
    var sig = text + "|" + srcGen + "|" + term.cols + "x" + term.rows + "|" +
              screen.clientWidth + "x" + screen.clientHeight;
    if (sig === lastSig) return;
    lastSig = sig;
    if (text !== askedRows) askSoon();

    var regions = codeRegions(rows), taken = {};
    regions.forEach(function (g) { for (var r = g.top; r <= g.bottom; r++) taken[r] = true; });
    // A code block that is one `! command` already has the command's own button.
    var cmds = cmdRegions(rows);
    regions = regions.filter(function (g) {
      return !cmds.some(function (c) { return c.top === g.top && c.bottom === g.bottom; });
    });
    regions = regions.concat(quoteRegions(rows, taken), tableRegions(rows, taken), cmds);

    // Claude's spinner rewrites the screen several times a second while it works: an
    // unchanged button is kept (and moved), so it neither flickers nor loses its flash.
    var cellW = screen.clientWidth / term.cols, rowH = screen.clientHeight / term.rows;
    var used = {}, next = {};
    regions.forEach(function (reg) {
      var at = spot(reg, rows);
      var slot = at.row + ":" + (at.left !== undefined ? "L" + at.left : "R" + at.right);
      if (used[slot]) return;
      used[slot] = true;
      var id = slot + "|" + reg.kind + "|" + reg.text;
      var b = shown[id] || button(reg);
      place(b, at, cellW, rowH, screen.clientWidth);
      if (b.parentNode !== layer) layer.appendChild(b);
      next[id] = b;
    });
    Object.keys(shown).forEach(function (id) {
      if (!next[id] && shown[id].parentNode) shown[id].parentNode.removeChild(shown[id]);
    });
    shown = next;
  }

  function schedule() {
    if (!scanT) scanT = setTimeout(scan, 120);
  }

  /* Ask the framing page for Claude's reply blocks once the output has settled; the
     reply (below) re-scans. Unframed (the fullscreen tab) there is no one to ask. */
  function askSoon() {
    if (window.parent === window) return;
    clearTimeout(askT);
    askT = setTimeout(function () {
      if (document.hidden) return;
      var wait = lastAsk + ASK_MIN_MS - Date.now();
      if (wait > 0) { askT = setTimeout(askSoon, wait); return; }
      lastAsk = Date.now();
      askedRows = term ? screenRows().join("\n") : null;
      window.parent.postMessage({type: "miss-claude:copy-src?"}, "*");
    }, QUIET_MS);
  }

  window.addEventListener("message", function (ev) {
    var d = ev.data;
    if (ev.source !== window.parent || !d || d.type !== "miss-claude:copy-src" ||
        !Array.isArray(d.blocks)) return;
    sources = d.blocks;
    srcGen++;
    schedule();
  });

  var style = document.createElement("style");
  style.textContent =
    ".mc-copy-layer{position:absolute;inset:0;pointer-events:none;z-index:20}" +
    ".mc-copy{position:absolute;pointer-events:auto;box-sizing:border-box;margin:0;" +
    "padding:0 7px;font:11px/1 system-ui,-apple-system,sans-serif;color:#cfd8e3;" +
    "background:#27303b;border:1px solid #4d5a69;border-radius:4px;cursor:pointer;" +
    "user-select:none;-webkit-user-select:none;white-space:nowrap;opacity:.9}" +
    ".mc-copy:hover{background:#364252;color:#fff;opacity:1}" +
    ".mc-copy.ok{background:#1d6a40;border-color:#2e9a5f;color:#fff}";
  document.head.appendChild(style);

  function attach() {
    var t = window.term;
    if (!t || t === term || typeof t.onRender !== "function") return;
    term = t;
    lastSig = "";
    t.onRender(schedule);
    t.onResize(schedule);
    if (typeof t.onScroll === "function") t.onScroll(schedule);
    schedule();
  }
  window.addEventListener("resize", schedule);
  attach();
  setInterval(attach, 250);
})();
