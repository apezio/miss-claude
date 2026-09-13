# Canvas dashboard (`/canvas`) — map for feature workers

Everything lives in `app.py`. Find it with `scripts/outline app.py canvas` (and `... chat` for the
embedded chat page). Tests: `python3 -m unittest tests/test_canvas.py`. Try it live with
`scripts/dev-instance start` → `http://127.0.0.1:4209/canvas` (the `probe` mission can be put on
the canvas by POSTing a layout to `/canvas/layout` or via right-click → *Add mission*).

## What it is

A spatial index: each mission is a **card** (titlebar + the mission's chat page in an iframe) on
an infinite scrolling **world**. Cards drag, resize, multi-select, take a colour, and can sit in
**groups** (labelled rectangles) or next to **notes** (free-text labels). The page polls activity
so a card turns yellow (Claude working) / red (waiting for you, with a ding) / grey (no Claude).
Those three colours are operator-tunable: right-click empty canvas → *Status colours…* (a panel
of swatches per state, saved in the shared layout; JS paints them as `--st-*` CSS variables).

## Server side (Python, ~150 lines, near line 1300)

| Piece | Role |
|---|---|
| `CANVAS_FILE` (`MISSION_CANVAS_FILE`, default `~/.miss-claude/canvas.json`) | the ONE shared layout file |
| `clean_canvas_layout(raw)` | normalizes any untrusted layout to exactly `{cards, groups, notes, hidden}`; bad entries are dropped one at a time, never the whole file. **Every new persisted field must be added here** (and to its test) or it will be silently stripped. |
| `read_canvas_layout` / `write_canvas_layout` | file I/O through the cleaner; atomic write, last write wins |
| `canvas_state()` | the `/canvas.json` payload: `layout`, `missions{name:{state,turn,running,live}}`, `all` names. One tmux snapshot; `mission_activity_detail()` (≈line 1000) classifies the transcript and names the wait (`turn` = uuid of the entry that ended the turn, `""` unless waiting) |
| `render_canvas_page()` | shell markup + `INITIAL` (a `canvas_state()`), then `CANVAS_JS`; `CANVAS_CSS` is spliced into `page()`'s style block |
| routes in `Handler` | GET `/canvas`, GET `/canvas.json`, POST `/canvas/layout` (whole layout as JSON), POST `/m/<n>/console/start` (▶), POST `/m/<n>/kill` (⏸ and ✕), POST `/spawn` with `canvas=1` (New mission → JSON + headless console) |

Layout schema (all ints are world pixels, clamped ≥ 0):

```
cards:  {name: {x,y,w,h,color,ding}}             color ∈ CANVAS_COLORS ("none" = untinted);
                                                 ding: the card's own bell, false only if explicit
groups: [{id,x,y,w,h,label,color}]               id matches CANVAS_ID_RE
notes:  [{id,x,y,w,h,text,color,size[,pin]}]     size ∈ s|m|l|xl, text ≤ CANVAS_NOTE_MAX_TEXT;
                                                 pin = card name the note is docked under
hidden: [names]                                  cards the operator removed; not auto-re-added
state_colors: {working,waiting,off}              which palette colour each activity state paints
                                                 (defaults yellow/red/gray; never "none")
```

## Browser side (`CANVAS_JS`, one IIFE, ~630 lines; `CANVAS_CSS` above it)

Read it top to bottom by its `// ----` section headers:

1. **constants / globals** — `SCALE` (world → screen; the zoom), default sizes, `layout` (the in-memory copy of the file), `states` (last activity per card),
   `drag` (the active pointer gesture or null), `dirty` (unsaved local edits).
2. **URLs** — `chatUrl/fullUrl/ctxUrl/killUrl/startUrl`, token appended by `q()`.
3. **ding** — WebAudio two-tone, unlocked on first pointerdown, master mute in `localStorage`;
   each card's titlebar 🔔 (`setBell`, `cards[n].ding` in the layout) silences that card alone.
4. **geometry** — `worldPoint(ev)` (screen → world), `overlaps`, `freeSlot`, `contentBounds`,
   `fitWorld` (sizes `#world` in world px: content + margin, never smaller than the viewport),
   `cardsInside/notesInside(group)` (membership = centre point, nothing stored).
   **zoom** — `SCALE` + `transform: scale()` on `#world` (origin 0 0), 25 %–250 %: `setZoom`,
   `zoomAt(s, clientX, clientY)` (keeps the world point under the pointer fixed by re-setting the
   viewport's scroll), `zoomFit`, `wheelZoom`, `zoomKey`. Inputs: option/alt + wheel, ctrl + wheel (= trackpad
   pinch, `passive:false` so the browser's own zoom is suppressed) on `#viewport`, the bar's
   − / % (= reset) / + / Fit, ctrl + = − 0. A wheel over a card goes to its iframe, so
   `bindFrameZoom` listens inside each (same-origin) chat frame on load and translates its client
   coords through the frame's scaled rect; the cross-origin ttyd terminal throws and is skipped.
   Zoom + scroll live in this browser's `localStorage` (`canvas-view`), NOT the layout — the
   layout stays in world px and `worldPoint` divides by `SCALE`, so drag/resize/marquee need no
   zoom awareness. `#world.zooming` (300 ms after the last wheel) adds `will-change` for the gesture.
5. **DOM** — `buildCard/buildGroup/buildNote` create elements once; `render()` reconciles the DOM
   to `layout` (removes strays, places every rect, applies colour/size/text) and skips anything
   currently being dragged. `setState(el, st, running)` paints activity + play/pause + `.acked`.
6. **persistence** — `markDirty()` debounces `save()` (POST whole layout). `dirty` stays set until
   the server echoes the same JSON, and while it is set a poll will NOT overwrite `layout`.
7. **polling** — `poll()` every 5 s (`tick()`; a hidden tab still polls every `HIDDEN_MS` 15 s so the ding fires in the background, with the per-card context polls skipped): converges `layout` on the server copy,
   auto-adds live consoles not in `hidden`, updates states, dings on working→waiting, then
   `pollCtx()` refreshes context badges every 30 s.
8. **selection** — `.selected` class on elements; `select/clearSel/selected`; `rectOf(el)` maps a
   DOM element back to its layout record (the one place that knows the three kinds).
9. **pointer** — ONE `pointerdown` on `#world` decides the gesture: grip → resize; card titlebar
   or note body → move (the selection moves together); group label/body → move group + members;
   empty world → marquee. `pointermove`/`pointerup` on `document` apply it. While a gesture is
   on, `body.dragging`/`body.resizing` set `pointer-events:none` on iframes — without that the
   iframe swallows the pointer and the gesture dies.
    **Dropping a drag of notes (and nothing else) on a card sends, it does not move**: while
    such a drag hovers a card (`cardAt`, geometry not `elementFromPoint`) the card shows a
    dashed `.droptarget` outline; on release `dropNotesOn` snaps every dragged note back to its
    origin (the layout is untouched, nothing saved) and `postMessage`s `{type:"chat-send", text}`
    into the card's chat iframe for each non-empty note — the chat page sends it exactly as if
    typed (bubble, `/console/key`, focus-ack). A paused or terminal-mode card refuses with a bar
    hint instead. Pinned notes get the same gesture: dragging one starts a send-only drag
    (`snapPinned` skips notes in `drag.moving` so the dock doesn't yank it back mid-drag) and
    wherever it is released — a card or empty canvas — it returns to its dock.
10. **groups / notes** — create, rename (prompt), edit (a textarea swapped into the note; blur
    commits — the world's `pointerdown` blurs it by hand because it `preventDefault()`s).
    A **pinned note** (card menu → *Add note*, `newPinnedNote`) carries `pin` = the card's
    name: `snapPinned()` recomputes its x/y from the card (stacked under earlier pinned notes
    of the same card) on every render and pointer move, so it never moves house — a drag on it
    is the send-onto-a-card gesture above (it snaps back to its dock), group moves skip it
    (`notesInside` excludes it), and the grip still resizes it. `removeCard` drops the card's pinned notes; a pinned note whose card is gone
    from the layout is unpinned where it stands. *Unpin (free note)* on its menu frees it.
11. **context menu** — `#cmenu` built per right-click from `item/sep/swatches/sizeRow` helpers;
    branches on note / card / group / world.
12. **card buttons** — ✕ (kill + remove + add to `hidden`), ⏸ (kill, keep card), ▶ (start),
    ⌨ (toggle the card's iframe between the chat view and the raw ttyd terminal —
    `termUrl(n)` = `CONSOLE_BASE` + `/?arg=<name>`, the same URL the mission page iframes;
    `CONSOLE_BASE` is `_console_base(host_header)` baked in by `render_canvas_page`. State
    is a `.terminal` class on the card, not persisted: a reload is back to chat. While the
    terminal shows, the ctx-rail `postMessage` targets a cross-origin page and is silently
    dropped, and connecting to ttyd re-runs console-launch.sh, so ⌨ on a paused card also
    resurrects its console — same as opening the mission page).
    The titlebar's **model badge** ("Fable 5.1 · M ▾", painted by `paintModel` from the context
    poll) is a **menu**: `modelMenu()` lists `MODELS` (Python `CANVAS_MODELS`, env
    `MISSION_MODELS`, labels via `modelName()`), ticks the current one, and picking an entry
    POSTs `/console/key` (`action=text`, `text=/model <id>`, `submit=1`, session
    `SESSION_PREFIX + name`) — i.e. it types `/model …` into the console. The badge is not a
    drag handle (`pointerdown` skips `.badge.model`) and only repaints from the transcript after
    Claude's next turn; the bar hint reports the switch / refusal.
13. **spawn** — intercepts the masthead Spawn modal's submit, posts `canvas=1`, drops the card at
    the right-click position (`pendingPos`).
14. **boot** — `layout = INITIAL.layout; render(); poll();`

Cross-frame seam: the embedded chat page (`CHAT_JS`, `?embed=1`) `postMessage`s
`{type:"chat-focus", name}` when its textarea takes focus or anything is sent (Send, Enter, YES SHIP); the canvas adds `.acked` to that card
and records `acked[name] = turn` in `localStorage` (`canvas-acked`), so a reload re-applies the ack for the *same* wait and a new turn's wait is red again.
Its Stop button `postMessage`s `{type:"chat-stop", name}` instead: the wait an operator-clicked
interrupt produces has a *new* turn id that only a later poll sees, so the canvas notes the click
(`preAck`, in-memory, 15 s window) and pre-acks that card's next working→waiting — no red, no ding.
Chat drafts live in the chat page (`localStorage` `chat-draft:<session>`), not the canvas.
The chat page's 🎤 (`#micbtn`, `DICTATION_JS` shared with the mission page's key bar) dictates
into the message box; the card iframe carries `allow="microphone"` for it.

## Adding things — the usual recipe

- **A new per-card/group/note property**: `clean_canvas_layout` (+ test) → `render()` paints it →
  a menu entry sets it → `markDirty()`. Nothing else.
- **A new object kind**: copy the notes pattern — build/render/`rectOf`/pointerdown/marquee
  selector/context-menu branch/`fitWorld` — plus its list in `clean_canvas_layout`.
- **A new card action**: a route in `Handler`, a URL helper, a button in `buildCard`, a branch in
  the card-buttons click handler; refresh with `poll()`.
- Verify with `scripts/check` and a real pointer drag in a browser (Playwright): unit tests only
  see the server side and that the JS strings are present.

## Invariants

- The layout file is the truth; every tab converges to it on the next poll. Never keep state only
  in the DOM.
- Seconds/positions on the wire are plain ints in world coordinates; no epochs, no screen pixels.
- `app.py` never calls Claude on a page load; `canvas_state()` reads transcripts and tmux only.
- Cards are auto-added only for missions with a live Claude; a card the operator removed goes to
  `hidden` and stays off until *Add mission*.
