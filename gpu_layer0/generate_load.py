"""generate_load.py — sustained real GPU compute load via repeated
matrix multiplication, so power draw actually moves instead of sitting
idle at ~10W. Not part of the signal/reflex system -- this is just the
thing being observed, standing in for a real workload.

Usage: python3 generate_load.py [duration_s]
"""
import sys
import time

import torch

duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0

device = torch.device("cuda")
a = torch.randn(4096, 4096, device=device)
b = torch.randn(4096, 4096, device=device)

print(f"[load] running matmul on {torch.cuda.get_device_name(0)} for {duration_s:.0f}s", flush=True)
t0 = time.time()
n = 0
while time.time() - t0 < duration_s:
    c = a @ b
    torch.cuda.synchronize()
    n += 1

print(f"[load] done, {n} matmuls in {time.time()-t0:.1f}s", flush=True)
