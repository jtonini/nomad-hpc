import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
fig, ax = plt.subplots(1, 1, figsize=(8, 8))
ax.set_xlim(0, 8)
ax.set_ylim(0, 9)
ax.set_aspect('equal')
ax.axis('off')

# Colors (top to bottom)
c_dispatch = '#a6611a'  # Brown - Alert Dispatcher
c_alert = '#dfc27d'     # Tan - Alert Engine
c_engine = '#f5f5f5'    # Light gray - Monitoring/Prediction
c_data = '#80cdc1'      # Teal light - Data Layer
c_collect = '#018571'   # Teal dark - Collectors

def add_box(ax, x, y, w, h, label, sublabel=None, color='#3498db', section_label=None):
    box = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.1",
                         facecolor=color, edgecolor='black', linewidth=2, alpha=0.85)
    ax.add_patch(box)
    # Use dark text for light backgrounds
    text_color = 'black' if color in ['#f5f5f5', '#dfc27d', '#80cdc1'] else 'white'
    ax.text(x + w/2, y + h/2 + (0.12 if sublabel else 0), label,
            ha='center', va='center', fontsize=11, fontweight='bold', color=text_color)
    if sublabel:
        ax.text(x + w/2, y + h/2 - 0.2, sublabel,
                ha='center', va='center', fontsize=8, color=text_color, style='italic')
    if section_label:
        ax.text(x - 0.15, y + h/2, section_label, ha='right', va='center',
                fontsize=9, fontweight='bold', color='#555')

def add_arrow(ax, start, end):
    ax.annotate('', xy=end, xytext=start,
                arrowprops=dict(arrowstyle='->', color='#333', lw=1.5,
                               shrinkA=2, shrinkB=2))

# Title
ax.text(4, 8.5, 'NØMADE Architecture', ha='center', va='center',
        fontsize=16, fontweight='bold')

# Boxes with section labels on the left
add_box(ax, 1, 7.0, 6, 0.8, 'ALERT DISPATCHER', 'Email · Slack · Webhook · Dashboard',
        c_dispatch, 'Notification')
add_box(ax, 1, 5.5, 6, 0.8, 'ALERT ENGINE', 'Rules · Derivatives · Deduplication · Cooldowns',
        c_alert, 'Alerting')

# Two engines side by side
add_box(ax, 1, 3.7, 2.8, 1.2, 'MONITORING', 'Threshold-based', c_engine, 'Analysis')
add_box(ax, 4.2, 3.7, 2.8, 1.2, 'PREDICTION', 'ML Ensemble', c_engine)

add_box(ax, 1, 2.2, 6, 0.8, 'DATA LAYER', 'SQLite · Time-series · Job History · I/O Samples',
        c_data, 'Storage')
add_box(ax, 1, 0.5, 6, 1.0, 'COLLECTORS', 'disk · slurm · job_metrics · iostat · mpstat · vmstat · gpu · nfs',
        c_collect, 'Collection')

# Arrows - shorter, data flows UP
add_arrow(ax, (4, 1.5), (4, 2.2))       # Collectors to Data Layer
add_arrow(ax, (2.4, 3.0), (2.4, 3.7))   # Data Layer to Monitoring
add_arrow(ax, (5.6, 3.0), (5.6, 3.7))   # Data Layer to Prediction
add_arrow(ax, (2.4, 4.9), (2.4, 5.5))   # Monitoring to Alert Engine
add_arrow(ax, (5.6, 4.9), (5.6, 5.5))   # Prediction to Alert Engine
add_arrow(ax, (4, 6.3), (4, 7.0))       # Alert Engine to Dispatcher

plt.tight_layout()
plt.savefig('architecture.png', dpi=150, bbox_inches='tight',
            facecolor='white', edgecolor='none')
print("Saved architecture.png")
