#!/usr/bin/env python3
"""
sudoku.py — Backtracking Sudoku solver paced by the Kuramoto substrate.

Each locked AxisPulse tick advances one step of the solve.
Solve rate = substrate tick rate. WITHDRAW pauses the solver.
Phase geometry and reflex state are recorded per solve step.
"""
import socket, struct, time, sys

AP_GRP  = "239.0.0.2"; AP_PORT = 7404
AP_FMT  = ">HBBIfffffHQ"; AP_SIZE = struct.calcsize(AP_FMT); AP_MAGIC = 0x4158

RS_GRP  = "239.0.0.4"; RS_PORT = 7450
RS_FMT  = "!HBff";     RS_SIZE = struct.calcsize(RS_FMT);   RS_MAGIC = 0x5253
RS_NAME = ["CALM", "ALERT", "WITHDRAW", "PARK", "RECOVER"]

PUZZLE = [
    [0,0,0, 0,0,0, 0,0,1],
    [0,0,0, 4,0,0, 6,0,0],
    [0,6,0, 9,3,0, 0,4,5],
    [8,0,0, 5,0,2, 0,3,9],
    [7,0,0, 0,0,0, 0,0,0],
    [4,0,0, 6,0,1, 0,2,7],
    [0,2,0, 7,5,0, 0,8,6],
    [0,0,0, 1,0,0, 3,0,0],
    [0,0,0, 0,0,0, 0,0,2],
]

def _print_grid(g):
    for r in range(9):
        row = ""
        for c in range(9):
            row += (str(g[r][c]) if g[r][c] else ".") + " "
            if c in (2, 5): row += "| "
        print(row)
        if r in (2, 5): print("-" * 22)

def _valid(g, r, c, n):
    if n in g[r]: return False
    if n in [g[i][c] for i in range(9)]: return False
    br, bc = (r//3)*3, (c//3)*3
    for i in range(3):
        for j in range(3):
            if g[br+i][bc+j] == n: return False
    return True

def _cells(g):
    """Return list of (row, col) for empty cells."""
    return [(r, c) for r in range(9) for c in range(9) if g[r][c] == 0]

def _mcast_in(grp, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(grp) + socket.inet_aton("0.0.0.0"))
    s.setblocking(False)
    return s

ap_sock = _mcast_in(AP_GRP, AP_PORT)
rs_sock = _mcast_in(RS_GRP, RS_PORT)

import selectors
sel = selectors.DefaultSelector()
sel.register(ap_sock, selectors.EVENT_READ, data="ap")
sel.register(rs_sock, selectors.EVENT_READ, data="rs")

# solver state — iterative backtracking
import copy
grid   = copy.deepcopy(PUZZLE)
stack  = []   # (row, col, next_candidate) frames
cells  = _cells(grid)
ptr    = 0    # index into cells

reflex = 0
ticks  = 0
steps  = 0
t0     = time.time()

print("sudoku.py — substrate-paced solver", flush=True)
print(f"Locked ticks advance one backtrack step each.", flush=True)
print(f"WITHDRAW pauses the solver.\n", flush=True)
_print_grid(grid)
print(flush=True)

solved = False

while not solved:
    for key, _ in sel.select(timeout=1.0):
        if key.data == "rs":
            data, _ = rs_sock.recvfrom(16)
            if len(data) >= RS_SIZE:
                f = struct.unpack_from(RS_FMT, data)
                if f[0] == RS_MAGIC and 0 <= f[1] < len(RS_NAME):
                    if f[1] != reflex:
                        reflex = f[1]
                        print(f"[reflex] → {RS_NAME[reflex]}", flush=True)

        elif key.data == "ap":
            data, _ = ap_sock.recvfrom(64)
            if len(data) < AP_SIZE: continue
            f = struct.unpack_from(AP_FMT, data)
            if f[0] != AP_MAGIC or not f[2]: continue

            ticks += 1
            tick   = f[3]
            pd_dev = f[7]
            locked = f[2]

            # pause during WITHDRAW or PARK
            if reflex in (2, 3):
                continue

            # advance one backtrack step
            steps += 1

            if ptr >= len(cells):
                solved = True
                break

            r, c = cells[ptr]

            # find next valid candidate for this cell
            current = grid[r][c]
            placed  = False
            for n in range(current + 1, 10):
                if _valid(grid, r, c, n):
                    stack.append((r, c, n))
                    grid[r][c] = n
                    ptr += 1
                    placed = True
                    break

            if not placed:
                # backtrack
                grid[r][c] = 0
                if not stack:
                    print("No solution found.", flush=True)
                    sys.exit(1)
                pr, pc, pn = stack.pop()
                grid[pr][pc] = 0
                ptr -= 1
                # re-enter this cell's frame with pn as the last tried value
                cells = _cells(grid) if ptr == 0 else cells
                r, c = pr, pc
                # will retry from pn+1 next tick
                grid[r][c] = pn  # restore so next tick increments from here

            if steps % 500 == 0:
                elapsed = time.time() - t0
                remaining = len([cl for cl in cells[ptr:] if grid[cl[0]][cl[1]] == 0])
                print(f"[sudoku] tick={tick}  steps={steps}  ptr={ptr}/{len(cells)}"
                      f"  pd_dev={pd_dev:.4f}  reflex={RS_NAME[reflex]}"
                      f"  elapsed={elapsed:.1f}s", flush=True)

    if solved or ptr >= len(cells):
        solved = True

print(f"\nSolved in {steps} steps, {ticks} ticks, {time.time()-t0:.1f}s\n", flush=True)
_print_grid(grid)
