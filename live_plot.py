#!/usr/bin/env python3
"""
Live plot of Pi2 nucleus state.
Usage: ssh pi "tail -n 120 -f /tmp/ns_lan_gain.log" | python3 live_plot.py
"""
import sys, re, collections
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

WINDOW = 120

temlum_buf = collections.deque(maxlen=WINDOW)
ec_buf     = collections.deque(maxlen=WINDOW)
intent_buf = collections.deque(maxlen=WINDOW)

pat = re.compile(r'temlum=([+-]?\d+\.\d+).*e_C=([+-]?\d+\.\d+).*\s+(PARK|UNPARK|HOLD)$')

colors = {'UNPARK': '#238636', 'PARK': '#da3633', 'HOLD': '#9e6a03'}

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 6), sharex=True)
fig.patch.set_facecolor('#0d1117')
for ax in (ax1, ax2):
    ax.set_facecolor('#0d1117')
    ax.tick_params(colors='#8b949e')
    ax.spines[:].set_color('#30363d')
    ax.yaxis.label.set_color('#8b949e')
    ax.xaxis.label.set_color('#8b949e')

line1, = ax1.plot([], [], color='#58a6ff', linewidth=1.2)
line2, = ax2.plot([], [], color='#f78166', linewidth=1.2)
ax1.set_ylabel('temlum')
ax2.set_ylabel('e_C')
ax2.set_xlabel('tick (~1 Hz)')
ax1.set_title('Pi2 nucleus — live', color='#e6edf3', pad=8)
ax1.axhline(0, color='#30363d', linewidth=0.8, linestyle='--')
ax2.axhline(0,       color='#30363d', linewidth=0.8, linestyle='--')
ax2.axhline( 0.004,  color='#238636', linewidth=0.7, linestyle=':', alpha=0.8)
ax2.axhline(-0.003,  color='#da3633', linewidth=0.7, linestyle=':', alpha=0.8)
patches = [mpatches.Patch(color=v, alpha=0.7, label=k) for k, v in colors.items()]
ax1.legend(handles=patches, loc='upper right', framealpha=0.2,
           labelcolor='#e6edf3', facecolor='#161b22')
plt.tight_layout()
plt.ion()
plt.show()

spans1, spans2 = [], []

def redraw():
    n = len(temlum_buf)
    if n < 2:
        return
    t = list(range(n))
    tv = list(temlum_buf)
    ev = list(ec_buf)
    iv = list(intent_buf)

    line1.set_data(t, tv)
    line2.set_data(t, ev)
    ax1.set_xlim(0, n)
    ax2.set_xlim(0, n)
    ax1.set_ylim(min(tv) - 0.05, max(tv) + 0.05)
    ax2.set_ylim(min(ev) - 0.005, max(ev) + 0.005)

    for s in spans1 + spans2:
        s.remove()
    spans1.clear(); spans2.clear()

    prev = 0
    for i in range(1, n):
        if iv[i] != iv[prev] or i == n - 1:
            c = colors.get(iv[prev], '#8b949e')
            spans1.append(ax1.axvspan(t[prev], t[i], alpha=0.18, color=c, linewidth=0))
            spans2.append(ax2.axvspan(t[prev], t[i], alpha=0.10, color=c, linewidth=0))
            prev = i

    fig.canvas.draw_idle()
    plt.pause(0.001)

for line in sys.stdin:
    m = pat.search(line.strip())
    if m:
        temlum_buf.append(float(m.group(1)))
        ec_buf.append(float(m.group(2)))
        intent_buf.append(m.group(3))
        redraw()
