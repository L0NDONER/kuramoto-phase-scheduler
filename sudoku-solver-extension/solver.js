// Pure Sudoku logic. No DOM access. board: 9x9 array, 0 = empty.

function isValidPlacement(board, row, col, val) {
  for (let i = 0; i < 9; i++) {
    if (board[row][i] === val) return false;
    if (board[i][col] === val) return false;
  }
  const boxRow = row - (row % 3);
  const boxCol = col - (col % 3);
  for (let r = 0; r < 3; r++) {
    for (let c = 0; c < 3; c++) {
      if (board[boxRow + r][boxCol + c] === val) return false;
    }
  }
  return true;
}

function findEmptyCell(board) {
  let best = null;
  let bestCandidates = 10;
  for (let row = 0; row < 9; row++) {
    for (let col = 0; col < 9; col++) {
      if (board[row][col] !== 0) continue;
      let count = 0;
      for (let val = 1; val <= 9; val++) {
        if (isValidPlacement(board, row, col, val)) count++;
      }
      if (count < bestCandidates) {
        bestCandidates = count;
        best = [row, col];
        if (count <= 1) return best;
      }
    }
  }
  return best;
}

function solveSudoku(board) {
  const empty = findEmptyCell(board);
  if (!empty) return true;
  const [row, col] = empty;
  for (let val = 1; val <= 9; val++) {
    if (isValidPlacement(board, row, col, val)) {
      board[row][col] = val;
      if (solveSudoku(board)) return true;
      board[row][col] = 0;
    }
  }
  return false;
}

function isValidBoard(board) {
  for (let row = 0; row < 9; row++) {
    for (let col = 0; col < 9; col++) {
      const val = board[row][col];
      if (val === 0) continue;
      board[row][col] = 0;
      const ok = isValidPlacement(board, row, col, val);
      board[row][col] = val;
      if (!ok) return false;
    }
  }
  return true;
}

// Exposed for content.js in the content-script context.
if (typeof window !== "undefined") {
  window.SudokuSolver = { solveSudoku, isValidBoard };
}
