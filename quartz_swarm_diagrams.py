#!/usr/bin/env python3
"""
Visualize the quartz-swarm consensus dynamics from tick data.

This shows how the 3-core architecture converges on the true signal
through the median-of-medians consensus mechanism.
"""

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Rectangle
import numpy as np

# Data from the table
data = [
    (0.0, 0.0, 0.0),
    (100.0, 0.0, 0.0),
    (200.0, 0.0, 0.0),
    (300.0, 0.0, 0.0),
    (400.0, 0.0, 0.0),
    (500.0, 0.0, 0.0),
    (600.0, 0.0, 0.0),
    (700.0, 0.0, 0.0),
    (800.0, 0.0, 0.0),
    (900.0, 0.0, 0.0),
    (1000.0, 0.0, 0.0),
]

# Actually, let me create a more meaningful visualization
# that shows the full quartz-swarm architecture

def create_architecture_diagram():
    """Create a visual representation of the quartz→swarm isomorphism."""

    fig, axes = plt.subplots(3, 1, figsize=(14, 10))
    fig.suptitle('QUARTZ → SWARM: The 3-Core Architecture\n'
                 'ILLUSTRATIVE DIAGRAM — not measured data',
                 fontsize=16, fontweight='bold', color='#333333')
    fig.text(0.5, 0.965, 'labels/numbers below are illustrative examples, not measurements',
             ha='center', fontsize=10, style='italic', color='darkred')

    # ============================================================
    # CORE 1: PHYSICAL LAYER
    # ============================================================
    ax1 = axes[0]
    ax1.set_title('CORE 1: PHYSICAL LAYER (The Unfakeable Substrate)',
                  fontsize=12, fontweight='bold', color='#2E86AB')
    ax1.set_xlim(0, 10)
    ax1.set_ylim(0, 1)
    ax1.axis('off')

    # Quartz crystal representation
    crystal = Rectangle((0.5, 0.5), 2, 0.4, facecolor='#2E86AB', alpha=0.3,
                         edgecolor='#2E86AB', linewidth=2)
    ax1.add_patch(crystal)
    ax1.text(1.5, 0.7, 'Quartz Crystal\n(Physical Truth)', ha='center', va='center', fontsize=10)

    # Arrow
    ax1.annotate('', xy=(3.5, 0.7), xytext=(2.8, 0.7),
                 arrowprops=dict(arrowstyle='->', color='#2E86AB', lw=2))
    ax1.text(3.2, 0.85, 'isomorphic to', ha='center', va='center', fontsize=9, style='italic')

    # Meter representation
    meter = Rectangle((4.0, 0.5), 2, 0.4, facecolor='#2E86AB', alpha=0.3,
                       edgecolor='#2E86AB', linewidth=2)
    ax1.add_patch(meter)
    ax1.text(5.0, 0.7, 'Meter Readings\n(Unfakeable Δ)', ha='center', va='center', fontsize=10)

    # Examples
    ax1.text(0.5, 0.15, '• Crystal drift: ±0.001 ppm', fontsize=9, color='#2E86AB')
    ax1.text(0.5, 0.05, '• Local delta: ±0.2 physical units', fontsize=9, color='#2E86AB')
    ax1.text(4.0, 0.15, '• Tamper-proof attestation', fontsize=9, color='#2E86AB')
    ax1.text(4.0, 0.05, '• True median = ground truth', fontsize=9, color='#2E86AB')

    # ============================================================
    # CORE 2: CONSENSUS LAYER
    # ============================================================
    ax2 = axes[1]
    ax2.set_title('CORE 2: CONSENSUS LAYER (Walker Alignment)',
                  fontsize=12, fontweight='bold', color='#A23B72')
    ax2.set_xlim(0, 10)
    ax2.set_ylim(0, 1)
    ax2.axis('off')

    # Walkers representation
    walker_positions = [1.0, 2.5, 4.0, 5.5, 7.0, 8.5]
    for i, pos in enumerate(walker_positions):
        ax2.plot(pos, 0.7, 'o', markersize=20, color='#A23B72', alpha=0.5 + i*0.08)
        ax2.text(pos, 0.85, f'W{i+1}', ha='center', va='center', fontsize=8, color='#A23B72')

    # Movement arrows
    for i in range(len(walker_positions)-1):
        ax2.annotate('', xy=(walker_positions[i+1]-0.3, 0.7),
                     xytext=(walker_positions[i]+0.3, 0.7),
                     arrowprops=dict(arrowstyle='->', color='#A23B72', lw=1, alpha=0.5))

    ax2.text(5.0, 0.55, 'Gossip Protocol → Median Corroboration',
             ha='center', va='center', fontsize=10, color='#A23B72', fontweight='bold')
    ax2.text(5.0, 0.45, 'Walkers align to the tone through distributed consensus',
             ha='center', va='center', fontsize=9, style='italic', color='#A23B72')

    # Example consensus
    ax2.text(0.5, 0.15, '• K=5 gossip rounds', fontsize=9, color='#A23B72')
    ax2.text(0.5, 0.05, '• Belief propagation', fontsize=9, color='#A23B72')
    ax2.text(4.0, 0.15, '• Median-of-K corroboration', fontsize=9, color='#A23B72')
    ax2.text(4.0, 0.05, '• Step-wise convergence', fontsize=9, color='#A23B72')

    # ============================================================
    # CORE 3: GOVERNANCE LAYER
    # ============================================================
    ax3 = axes[2]
    ax3.set_title('CORE 3: GOVERNANCE LAYER (Watchdog Regulation)',
                  fontsize=12, fontweight='bold', color='#F18F01')
    ax3.set_xlim(0, 10)
    ax3.set_ylim(0, 1)
    ax3.axis('off')

    # Watchdog
    ax3.plot(2.0, 0.7, 's', markersize=25, color='#F18F01', alpha=0.7)
    ax3.text(2.0, 0.85, 'Watchdog', ha='center', va='center', fontsize=9, fontweight='bold', color='#F18F01')

    # Check arrows
    ax3.annotate('', xy=(3.5, 0.7), xytext=(2.8, 0.7),
                 arrowprops=dict(arrowstyle='->', color='#F18F01', lw=2))
    ax3.text(3.2, 0.85, 'compares', ha='center', va='center', fontsize=9, color='#F18F01')

    ax3.annotate('', xy=(5.5, 0.7), xytext=(4.2, 0.7),
                 arrowprops=dict(arrowstyle='->', color='#F18F01', lw=2))

    # Kill/Respawn
    ax3.plot(6.0, 0.7, 'v', markersize=20, color='red', alpha=0.7)
    ax3.text(6.0, 0.55, 'KILL', ha='center', va='center', fontsize=9, fontweight='bold', color='red')

    ax3.annotate('', xy=(7.0, 0.5), xytext=(6.5, 0.5),
                 arrowprops=dict(arrowstyle='->', color='green', lw=2))
    ax3.plot(7.5, 0.5, '^', markersize=20, color='green', alpha=0.7)
    ax3.text(7.5, 0.35, 'RESPAWN', ha='center', va='center', fontsize=9, fontweight='bold', color='green')

    # Examples
    ax3.text(0.5, 0.15, '• Detection band: ±0.4', fontsize=9, color='#F18F01')
    ax3.text(0.5, 0.05, '• Warmup: 50ms', fontsize=9, color='#F18F01')
    ax3.text(4.0, 0.15, '• Respawn delay: 60ms', fontsize=9, color='#F18F01')
    ax3.text(4.0, 0.05, '• Byzantine → honest transformation', fontsize=9, color='#F18F01')

    plt.tight_layout()
    plt.savefig('quartz_swarm_architecture.png', dpi=150, bbox_inches='tight')
    print("saved quartz_swarm_architecture.png")


def create_consensus_visualization():
    """Create a visualization of the consensus dynamics."""

    # Create synthetic data that shows the convergence dynamics
    np.random.seed(42)

    time = np.linspace(0, 500, 200)
    true_signal = np.ones_like(time) * 0.0  # True value at 0

    # 8 nodes with initial spread
    nodes = {
        'Scotland': np.random.normal(-0.1, 0.2, len(time)),
        'North': np.random.normal(0.8, 0.15, len(time)),  # Byzantine
        'NorthWest': np.random.normal(-0.05, 0.2, len(time)),
        'Yorkshire': np.random.normal(0.05, 0.18, len(time)),
        'Midlands': np.random.normal(0.9, 0.12, len(time)),  # Byzantine
        'East': np.random.normal(0.85, 0.14, len(time)),  # Byzantine
        'SouthWest': np.random.normal(-0.08, 0.2, len(time)),
        'SouthEast': np.random.normal(0.02, 0.19, len(time)),
    }

    # Add convergence dynamics (exponential decay toward truth)
    for name in nodes:
        if name in ['North', 'Midlands', 'East']:
            # Byzantine nodes start high but get killed/respawned
            nodes[name] = 1.0 * np.exp(-time/50) + 0.1 * np.random.randn(len(time))
        else:
            # Honest nodes converge to 0
            nodes[name] = nodes[name] * np.exp(-time/30) + 0.05 * np.random.randn(len(time))

    # Median-of-medians consensus
    consensus = np.median(np.array(list(nodes.values())), axis=0)

    fig, ax = plt.subplots(figsize=(14, 8))

    # Plot individual nodes
    colors = plt.cm.Set3(np.linspace(0, 1, len(nodes)))
    for i, (name, values) in enumerate(nodes.items()):
        ls = '--' if name in ['North', 'Midlands', 'East'] else '-'
        alpha = 0.4 if name in ['North', 'Midlands', 'East'] else 0.6
        ax.plot(time, values, ls, color=colors[i], lw=1.5, alpha=alpha, label=name)

    # Plot consensus
    ax.plot(time, consensus, 'k-', lw=3, alpha=0.9, label='Consensus (median-of-medians)')

    # Plot true signal
    ax.axhline(0.0, color='blue', ls=':', lw=2, alpha=0.7, label='True signal (physical truth)')

    # Mark regions
    ax.axvspan(0, 50, alpha=0.1, color='gray', label='Warmup period')
    ax.axvspan(50, 200, alpha=0.1, color='orange', label='Watchdog active')
    ax.axvspan(200, 500, alpha=0.1, color='green', label='Converged state')

    # Add event markers
    kill_times = [60, 80, 120]
    respawn_times = [120, 140, 180]
    for kt in kill_times:
        ax.axvline(kt, color='red', ls='-', lw=1, alpha=0.5)
        ax.text(kt, -0.5, 'KILL', rotation=90, color='red', fontsize=8, alpha=0.7)
    for rt in respawn_times:
        ax.axvline(rt, color='green', ls='-', lw=1, alpha=0.5)
        ax.text(rt, -0.55, 'RESPAWN', rotation=90, color='green', fontsize=8, alpha=0.7)

    ax.set_xlabel('Time (ms)', fontsize=12)
    ax.set_ylabel('Belief / Δ', fontsize=12)
    ax.set_title('Quartz → Swarm Consensus Dynamics\n3 Byzantine nodes anchored at +1.0, corrected by watchdog\n'
                 'SYNTHETIC EXAMPLE DATA — not measured from a real run',
                 fontsize=14, fontweight='bold', color='#333333')
    ax.legend(loc='upper right', fontsize=8, ncol=2)
    ax.grid(alpha=0.3)
    ax.set_ylim(-0.8, 1.2)

    plt.tight_layout()
    plt.savefig('quartz_swarm_consensus.png', dpi=150)
    print("saved quartz_swarm_consensus.png")


def create_layer_diagram():
    """Create a stack diagram showing the three layers."""

    fig, ax = plt.subplots(figsize=(12, 8))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 10)
    ax.axis('off')

    # Title
    ax.text(6, 9.5, 'QUARTZ → SWARM: 3-CORE ARCHITECTURE',
            fontsize=16, fontweight='bold', ha='center')
    ax.text(6, 9.1, 'ILLUSTRATIVE DIAGRAM — conceptual, not measured data',
            fontsize=10, style='italic', ha='center', color='darkred')

    # Layer 1: Physical
    rect1 = Rectangle((2, 6.5), 8, 1.5, facecolor='#2E86AB', alpha=0.2, edgecolor='#2E86AB', linewidth=2)
    ax.add_patch(rect1)
    ax.text(3.5, 7.5, 'CORE 1: PHYSICAL', fontweight='bold', color='#2E86AB', fontsize=12)
    ax.text(3.5, 7.0, 'Meter readings · Local Δ · Tamper-proof truth', fontsize=10, color='#2E86AB')
    ax.text(9.5, 7.5, '↕', fontsize=16, color='#2E86AB')
    ax.text(10.5, 7.5, 'Quartz crystal', fontsize=9, style='italic', color='#2E86AB')

    # Arrow down
    ax.annotate('', xy=(6, 5.5), xytext=(6, 6.5),
                arrowprops=dict(arrowstyle='->', color='gray', lw=2))
    ax.text(6.5, 6.0, 'provides truth', fontsize=9, color='gray', rotation=0)

    # Layer 2: Consensus
    rect2 = Rectangle((2, 4.0), 8, 1.5, facecolor='#A23B72', alpha=0.2, edgecolor='#A23B72', linewidth=2)
    ax.add_patch(rect2)
    ax.text(3.5, 5.0, 'CORE 2: CONSENSUS', fontweight='bold', color='#A23B72', fontsize=12)
    ax.text(3.5, 4.5, 'Gossip protocol · Median corroboration · Belief', fontsize=10, color='#A23B72')
    ax.text(9.5, 5.0, '↕', fontsize=16, color='#A23B72')
    ax.text(10.5, 5.0, 'Walkers', fontsize=9, style='italic', color='#A23B72')

    # Arrow down
    ax.annotate('', xy=(6, 3.0), xytext=(6, 4.0),
                arrowprops=dict(arrowstyle='->', color='gray', lw=2))
    ax.text(6.5, 3.5, 'enables regulation', fontsize=9, color='gray', rotation=0)

    # Layer 3: Governance
    rect3 = Rectangle((2, 1.5), 8, 1.5, facecolor='#F18F01', alpha=0.2, edgecolor='#F18F01', linewidth=2)
    ax.add_patch(rect3)
    ax.text(3.5, 2.5, 'CORE 3: GOVERNANCE', fontweight='bold', color='#F18F01', fontsize=12)
    ax.text(3.5, 2.0, 'Watchdog · Kill+respawn · Lifecycle', fontsize=10, color='#F18F01')
    ax.text(9.5, 2.5, '↕', fontsize=16, color='#F18F01')
    ax.text(10.5, 2.5, 'Watchdog', fontsize=9, style='italic', color='#F18F01')

    # Result
    ax.text(6, 0.7, '→ SELF-HEALING, BYZANTINE-RESILIENT SYSTEM ←',
            fontsize=14, fontweight='bold', ha='center', color='darkgreen')
    ax.text(6, 0.2, 'The swarm survives 3+ Byzantine nodes through physical attestation + distributed consensus + governance',
            fontsize=10, ha='center', style='italic', color='gray')

    plt.tight_layout()
    plt.savefig('quartz_swarm_layers.png', dpi=150, bbox_inches='tight')
    print("saved quartz_swarm_layers.png")


if __name__ == "__main__":
    print("Generating quartz-swarm architecture visualizations...")
    print()

    create_architecture_diagram()
    create_consensus_visualization()
    create_layer_diagram()

    print()
    print("All visualizations complete!")
