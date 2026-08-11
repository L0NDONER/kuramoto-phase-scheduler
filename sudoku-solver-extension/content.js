// Generic sudoku grid detector/reader/writer. Works across arbitrary
// sudoku sites by trying several strategies in order of confidence and
// producing a uniform array of 81 "cell adapters" -- { read(), write(digit) }
// in row-major order (n = row*9 + col). scrapeBoard()/injectSolution() are
// strategy-agnostic; only detectGrid() knows about specific DOM shapes.
//
// True universality isn't achievable without image recognition (canvas/
// WebGL-rendered puzzles have no DOM to read) -- this covers the DOM-based
// patterns that cover the large majority of real web sudoku implementations:
// <input> grids, contenteditable grids, ARIA gridcells, and (as a named
// fallback for custom click+keypress widgets like The Times') a generic
// child-count heuristic. Sites that don't match any of these still have the
// diagnostic Scan/Logger tools to investigate manually.

function describeElement(el) {
  return {
    tag: el.tagName,
    id: el.id || null,
    classes: el.className && typeof el.className === "string" ? el.className.split(/\s+/).filter(Boolean) : [],
    attrs: Array.from(el.attributes).map((a) => `${a.name}="${a.value}"`),
    outerHTMLSample: el.outerHTML.slice(0, 300)
  };
}

// ---------- ordering ----------

// Sort DOM elements into row-major visual order using bounding boxes,
// rather than trusting DOM order (grids are laid out with CSS in DOM order
// most of the time, but not always -- this is robust to either case).
function sortRowMajor(elements) {
  const withRects = elements.map((el) => ({ el, rect: el.getBoundingClientRect() }));
  const rowHeight = Math.min(...withRects.map((w) => w.rect.height)) || 1;
  withRects.sort((a, b) => {
    const rowA = Math.round(a.rect.top / rowHeight);
    const rowB = Math.round(b.rect.top / rowHeight);
    if (rowA !== rowB) return rowA - rowB;
    return a.rect.left - b.rect.left;
  });
  return withRects.map((w) => w.el);
}

// ---------- strategies ----------

function tryInputGrid() {
  let inputs = Array.from(document.querySelectorAll("input")).filter(
    (el) => el.maxLength === 1 || el.getAttribute("maxlength") === "1"
  );
  if (inputs.length !== 81) {
    inputs = Array.from(document.querySelectorAll("input")).filter((el) => {
      const t = (el.type || "text").toLowerCase();
      return t === "text" || t === "tel" || t === "number";
    });
    if (inputs.length !== 81) return null;
  }
  const ordered = sortRowMajor(inputs);
  return {
    strategy: "input",
    cells: ordered.map((el) => ({
      read: () => {
        const v = el.value.trim();
        return v === "" ? 0 : parseInt(v, 10);
      },
      write: (digit) => {
        el.value = String(digit);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }))
  };
}

function tryContentEditableGrid() {
  const els = Array.from(document.querySelectorAll("[contenteditable]"));
  if (els.length !== 81) return null;
  const ordered = sortRowMajor(els);
  return {
    strategy: "contenteditable",
    cells: ordered.map((el) => ({
      read: () => {
        const v = el.textContent.trim();
        return v === "" ? 0 : parseInt(v, 10);
      },
      write: (digit) => {
        el.textContent = String(digit);
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }))
  };
}

function tryAriaGridCells() {
  const els = Array.from(document.querySelectorAll('[role="gridcell"]'));
  if (els.length !== 81) return null;
  const ordered = sortRowMajor(els);
  return {
    strategy: "ariaGridCell",
    cells: ordered.map((el) => {
      const input = el.querySelector("input");
      return {
        read: () => {
          const v = (input ? input.value : el.textContent).trim();
          return v === "" ? 0 : parseInt(v, 10);
        },
        write: (digit) => {
          if (input) {
            input.value = String(digit);
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
          } else {
            el.textContent = String(digit);
          }
        }
      };
    })
  };
}

// The Times' custom SVG widget: <g class="cell-group"> containing
// <rect id="cell-N"> and a sibling <text class="cell-number">. No editable
// element exists; entry is click-to-select then keydown-a-digit, simulated
// via synthetic pointer/mouse/keyboard events. Confirmed working against
// the live puzzle.
function tryTimesSvg() {
  const first = document.getElementById("cell-0");
  if (!first) return null;
  const cells = [];
  for (let n = 0; n < 81; n++) {
    const rect = document.getElementById(`cell-${n}`);
    if (!rect) return null;
    const cellGroup = rect.parentElement;
    const textEl = cellGroup.querySelector(".cell-number");
    cells.push({
      read: () => {
        const v = textEl ? textEl.textContent.trim() : "";
        return v === "" ? 0 : parseInt(v, 10);
      },
      write: (digit) => dispatchClickThenKey(rect, digit)
    });
  }
  return { strategy: "timesSvg", cells };
}

// Last-resort fallback: find a container with exactly 81 same-tag children
// (the fingerprint of a 9x9 grid drawn as plain divs/tds with no
// input/contenteditable/aria markup), and drive entry the same
// click+keydown way as the Times widget. Read via the cell's own text.
function tryGenericChildCount() {
  const all = document.querySelectorAll("body *");
  for (const el of all) {
    const children = Array.from(el.children);
    if (children.length !== 81) continue;
    const tagCounts = new Map();
    for (const c of children) tagCounts.set(c.tagName, (tagCounts.get(c.tagName) || 0) + 1);
    const [tag, count] = [...tagCounts.entries()].sort((a, b) => b[1] - a[1])[0];
    if (count !== 81) continue;
    const ordered = sortRowMajor(children.filter((c) => c.tagName === tag));
    if (ordered.length !== 81) continue;
    return {
      strategy: "genericChildCount",
      cells: ordered.map((el) => ({
        read: () => {
          const v = el.textContent.trim();
          return v === "" || isNaN(parseInt(v, 10)) ? 0 : parseInt(v, 10);
        },
        write: (digit) => dispatchClickThenKey(el, digit)
      }))
    };
  }
  return null;
}

function dispatchClickThenKey(el, digit) {
  const rectBounds = el.getBoundingClientRect();
  const clientX = rectBounds.left + rectBounds.width / 2;
  const clientY = rectBounds.top + rectBounds.height / 2;
  const pointerOpts = { bubbles: true, cancelable: true, composed: true, clientX, clientY };
  el.dispatchEvent(new PointerEvent("pointerdown", pointerOpts));
  el.dispatchEvent(new MouseEvent("mousedown", pointerOpts));
  el.dispatchEvent(new PointerEvent("pointerup", pointerOpts));
  el.dispatchEvent(new MouseEvent("mouseup", pointerOpts));
  el.dispatchEvent(new MouseEvent("click", pointerOpts));

  const keyOpts = {
    bubbles: true, cancelable: true, composed: true,
    key: String(digit), code: `Digit${digit}`, keyCode: 48 + digit, which: 48 + digit
  };
  document.dispatchEvent(new KeyboardEvent("keydown", keyOpts));
  document.dispatchEvent(new KeyboardEvent("keyup", keyOpts));
}

// Tried in order of confidence: standard editable-element patterns first
// (unambiguous, high confidence), custom widgets last (heuristic, lower
// confidence, more likely to need per-site adjustment).
const STRATEGIES = [tryInputGrid, tryContentEditableGrid, tryAriaGridCells, tryTimesSvg, tryGenericChildCount];

let cachedGrid = null;

function detectGrid() {
  if (cachedGrid) return cachedGrid;
  for (const strategy of STRATEGIES) {
    const result = strategy();
    if (result) {
      cachedGrid = result;
      console.log(`[SudokuSolver] Detected grid via strategy: ${result.strategy}`);
      return result;
    }
  }
  return null;
}

// ---------- board scraping / injection (strategy-agnostic) ----------

let lastEmptyCellIndices = [];

function scrapeBoard(grid) {
  const board = Array.from({ length: 9 }, () => Array(9).fill(0));
  lastEmptyCellIndices = [];
  for (let n = 0; n < 81; n++) {
    const val = grid.cells[n].read();
    const row = Math.floor(n / 9);
    const col = n % 9;
    board[row][col] = val;
    if (val === 0) lastEmptyCellIndices.push(n);
  }
  return board;
}

function injectSolution(grid, board) {
  for (const n of lastEmptyCellIndices) {
    const row = Math.floor(n / 9);
    const col = n % 9;
    grid.cells[n].write(board[row][col]);
  }
}

function injectSingleCell(grid, n, digit) {
  grid.cells[n].write(digit);
}

// ---------- diagnostic scan (manual fallback for unsupported sites) ----------

function scanByChildCount() {
  const candidates = [];
  const all = document.querySelectorAll("body *");
  for (const el of all) {
    const children = Array.from(el.children);
    if (children.length < 9) continue;
    const tagCounts = new Map();
    for (const child of children) tagCounts.set(child.tagName, (tagCounts.get(child.tagName) || 0) + 1);
    for (const [tag, count] of tagCounts) {
      if (count === 81 || count === 9) {
        const sampleChild = children.find((c) => c.tagName === tag);
        candidates.push({
          method: "childCount",
          container: describeElement(el),
          childTag: tag,
          childCount: count,
          sampleChild: describeElement(sampleChild),
          nestedInputs: el.querySelectorAll("input").length,
          nestedContentEditable: el.querySelectorAll("[contenteditable]").length
        });
      }
    }
  }
  return candidates;
}

function scanByKeyword() {
  const re = /grid|board|sudoku|cell|puzzle/i;
  const matches = [];
  for (const el of document.querySelectorAll("body *")) {
    const idClass = (el.id || "") + " " + (typeof el.className === "string" ? el.className : "");
    const testId = el.getAttribute && (el.getAttribute("data-testid") || "");
    if (re.test(idClass) || re.test(testId || "")) {
      matches.push({ method: "keyword", el: describeElement(el), childElementCount: el.childElementCount });
    }
  }
  return matches.slice(0, 60);
}

function scanForGrid() {
  const childCountCandidates = scanByChildCount();
  const keywordMatches = scanByKeyword();
  const detected = detectGrid();
  const result = {
    url: location.href,
    autoDetected: detected ? detected.strategy : null,
    childCountCandidates,
    keywordMatches,
    globals: {
      canvasCount: document.querySelectorAll("canvas").length,
      svgCount: document.querySelectorAll("svg").length,
      inputCount: document.querySelectorAll("input").length,
      contentEditableCount: document.querySelectorAll("[contenteditable]").length,
      ariaGridCells: document.querySelectorAll('[role="gridcell"]').length
    }
  };
  console.log("[SudokuSolver] Grid scan result:", result);
  console.log(JSON.stringify(result, null, 2));
  return result;
}

// ---------- interaction logger (diagnostic, for adding new custom-widget sites) ----------

let interactionLogHandlers = null;

function describeTarget(el) {
  if (!el) return null;
  return `${el.tagName}${el.id ? "#" + el.id : ""}${typeof el.className === "string" && el.className ? "." + el.className.split(/\s+/).join(".") : ""}`;
}

function startInteractionLogger() {
  if (interactionLogHandlers) return;
  const types = ["pointerdown", "mousedown", "mouseup", "click", "keydown", "keyup"];
  interactionLogHandlers = types.map((type) => {
    const handler = (e) => console.log(`[SudokuSolver] ${type}`, { target: describeTarget(e.target), key: e.key, code: e.code });
    document.addEventListener(type, handler, true);
    return { type, handler };
  });
  console.log("[SudokuSolver] Interaction logger started. Click an empty cell and press a digit key now.");
}

function stopInteractionLogger() {
  if (!interactionLogHandlers) return;
  for (const { type, handler } of interactionLogHandlers) document.removeEventListener(type, handler, true);
  interactionLogHandlers = null;
  console.log("[SudokuSolver] Interaction logger stopped.");
}

// ---------- message handling ----------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "scan") {
    sendResponse({ ok: true, result: scanForGrid() });
  } else if (msg.action === "solve") {
    try {
      const grid = detectGrid();
      if (!grid) {
        sendResponse({ ok: false, error: "Could not auto-detect a sudoku grid on this page. Try Scan (diagnostic)." });
        return;
      }
      const board = scrapeBoard(grid);
      if (!window.SudokuSolver.isValidBoard(board)) {
        sendResponse({ ok: false, error: "Invalid starting board (detection may be misreading cells)." });
        return;
      }
      const solved = window.SudokuSolver.solveSudoku(board);
      if (!solved) {
        sendResponse({ ok: false, error: "No solution found." });
        return;
      }
      injectSolution(grid, board);
      sendResponse({ ok: true, strategy: grid.strategy });
    } catch (err) {
      sendResponse({ ok: false, error: err.message });
    }
  } else if (msg.action === "hint") {
    try {
      const grid = detectGrid();
      if (!grid) {
        sendResponse({ ok: false, error: "Could not auto-detect a sudoku grid on this page. Try Scan (diagnostic)." });
        return;
      }
      const board = scrapeBoard(grid);
      if (!window.SudokuSolver.isValidBoard(board)) {
        sendResponse({ ok: false, error: "Invalid starting board (detection may be misreading cells)." });
        return;
      }
      if (lastEmptyCellIndices.length === 0) {
        sendResponse({ ok: false, error: "Grid is already full." });
        return;
      }
      const solved = window.SudokuSolver.solveSudoku(board);
      if (!solved) {
        sendResponse({ ok: false, error: "No solution found." });
        return;
      }
      const n = lastEmptyCellIndices[Math.floor(Math.random() * lastEmptyCellIndices.length)];
      const row = Math.floor(n / 9);
      const col = n % 9;
      injectSingleCell(grid, n, board[row][col]);
      sendResponse({ ok: true, strategy: grid.strategy });
    } catch (err) {
      sendResponse({ ok: false, error: err.message });
    }
  } else if (msg.action === "toggleLogger") {
    if (interactionLogHandlers) {
      stopInteractionLogger();
      sendResponse({ ok: true, logging: false });
    } else {
      startInteractionLogger();
      sendResponse({ ok: true, logging: true });
    }
  }
  return true;
});
