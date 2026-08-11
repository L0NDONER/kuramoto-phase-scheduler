# Universal Sudoku Solver

## Status

Complete and working. Detects and solves sudoku grids on arbitrary
websites via a multi-strategy DOM detector, not just The Times (the
original target, and still the reason a couple of things are shaped the
way they are).

## What it does

Click the extension icon on a page with a sudoku puzzle:

- **Solve** — detects the grid, scrapes the current state, solves it
  (backtracking with most-constrained-cell ordering), and writes every
  empty cell back into the page.
- **Hint (fill one cell)** — same detect/solve pipeline, but only fills
  in one random empty cell instead of the whole board.
- **Scan Grid (diagnostic)** — runs detection without solving/writing
  anything, and logs the full result (candidate elements, keyword
  matches, whichever detector strategy fired) to the page console. Use
  this first on a site the solver doesn't recognize.
- **Log Interaction (diagnostic)** — records real click/keydown events
  on the page while you manually click a cell and type a digit, so an
  unknown site's cell-write mechanism can be reverse-engineered from
  what actually fires.

## How detection works

`content.js` tries several strategies in order of confidence, producing
a uniform array of 81 `{ read(), write(digit) }` cell adapters — the
solver and injector never know which strategy matched:

1. `<input>` grids
2. `contenteditable` grids
3. ARIA `gridcell` roles
4. a generic child-count heuristic, for custom click+keypress widgets
   that don't use any standard grid markup (this is what The Times
   needs)

Elements are sorted into row-major order by bounding-box position, not
DOM order — grids are usually laid out in DOM order too, but not
always, and this is robust to either case. True universality (e.g.
canvas/WebGL-rendered puzzles with no DOM to read at all) isn't
achievable this way — those still have Scan/Log Interaction as manual
fallbacks, not automatic support.

**Writing back**: never a direct `.value =` assignment — that's
silently ignored by React/Vue-controlled inputs. Real `input`/`keydown`
events are dispatched instead, so the site's own state/validation
actually picks up the change.

**Cross-origin iframes**: The Times embeds its puzzle from a separate
`feeds.thetimes.com` frame, not the top-level page. `content.js` runs
in every frame (`all_frames: true`), and `popup.js` broadcasts each
action to every frame on the tab, using whichever one actually
responds with a detected grid.

## Files

- `manifest.json` — MV3 config. `<all_urls>` host permission is
  intentional (works on any sudoku site you click it on), not
  overreach left over from an earlier draft — detection only ever runs
  when triggered from the popup, never automatically on page load.
- `solver.js` — pure backtracking solver + board validator, no DOM
  access at all.
- `content.js` — grid detector, scraper, injector, and the two
  diagnostic tools, all message-driven from the popup.
- `popup.html` / `popup.js` — the four buttons above, cross-frame
  broadcast logic.
