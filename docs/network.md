# Network Methodology

NØMAÐ's prediction engine uses similarity networks to identify job failure patterns. This approach draws inspiration from biogeographical network analysis.

## Theoretical Foundation

### From Biogeography to HPC

The methodology is inspired by Vilhena & Antonelli (2015), who used network analysis to identify biogeographical regions from species distribution data. Just as biogeographical regions emerge from species patterns rather than being predefined, NØMAÐ allows job behavior patterns to emerge from metric data.

| Biogeography Concept | NØMAÐ Analog |
|---------------------|---------------|
| Species | Jobs |
| Geographic regions | Compute resources (nodes, partitions) |
| Emergent biomes | Job behavior clusters |
| Species ranges | Resource usage patterns |
| Transition zones | Domain boundaries (CPU↔GPU, NFS↔local) |

### Why Cosine Similarity?

NØMAÐ uses **cosine similarity on continuous feature vectors** rather than Simpson similarity on categorical presence/absence data:

- **Magnitude matters**: CPU efficiency of 80% vs 20% is significant, not just "used CPU"
- **Multi-dimensional**: Jobs have 17+ continuous metrics
- **Shape over scale**: Cosine similarity captures resource *profiles*, not absolute consumption

A job requesting 64GB with 50% utilization has a similar profile to one requesting 8GB with 50% utilization—both represent reasonable memory sizing—even though absolute consumption differs by 8x.

## Network Construction

### Step 1: Feature Vector Extraction

Each completed job produces a 19-dimensional feature vector with all values bounded [0-1]:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Feature Vector for Similarity Analysis                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  FROM SACCT (job outcome):              FROM IOSTAT (system I/O):           │
│  ┌────────────────────────────────┐     ┌────────────────────────────────┐  │
│  │  1. health_score        [0-1]  │     │ 11. avg_iowait_percent   [0-1] │  │
│  │  2. cpu_efficiency      [0-1]  │     │ 12. peak_iowait_percent  [0-1] │  │
│  │  3. memory_efficiency   [0-1]  │     │ 13. avg_device_util      [0-1] │  │
│  │  4. used_gpu            [0,1]  │     └────────────────────────────────┘  │
│  │  5. had_swap            [0,1]  │                                         │
│  └────────────────────────────────┘     FROM MPSTAT (CPU cores):            │
│                                         ┌────────────────────────────────┐  │
│  FROM JOB_MONITOR (I/O behavior):       │ 14. avg_core_busy        [0-1] │  │
│  ┌────────────────────────────────┐     │ 15. core_imbalance_ratio [0-1] │  │
│  │  6. total_write_gb      [0-1]  │     │ 16. max_core_busy        [0-1] │  │
│  │  7. write_rate_mbps     [0-1]  │     └────────────────────────────────┘  │
│  │  8. nfs_ratio           [0-1]  │                                         │
│  │  9. runtime_minutes     [0-1]  │     FROM VMSTAT (memory pressure):      │
│  │ 10. write_intensity     [0-1]  │     ┌────────────────────────────────┐  │
│  └────────────────────────────────┘     │ 17. avg_memory_pressure  [0-1] │  │
│                                         │ 18. peak_swap_activity   [0-1] │  │
│                                         │ 19. avg_procs_blocked    [0-1] │  │
│                                         └────────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

All features are pre-bounded to [0-1], so no z-score normalization is needed.
Cosine similarity naturally handles the multi-dimensional comparison.

### Step 2: Cosine Similarity Matrix

For jobs $a$ and $b$ with feature vectors $\vec{a}$ and $\vec{b}$:

$$\text{similarity}(a, b) = \frac{\vec{a} \cdot \vec{b}}{|\vec{a}| \cdot |\vec{b}|}$$

Values range from -1 (opposite profiles) to +1 (identical profiles).

### Step 3: Edge Creation

Edges connect jobs with similarity ≥ threshold (default: 0.7):
```python
if cosine_similarity(job_a, job_b) >= 0.7:
    network.add_edge(job_a, job_b)
```

**Threshold trade-offs:**

| Threshold | Network Density | Clusters | Use Case |
|-----------|-----------------|----------|----------|
| 0.9+ | Sparse | Tight, specific | Anomaly detection |
| 0.7 (default) | Moderate | Balanced | General prediction |
| 0.5 | Dense | Broad patterns | Exploratory analysis |

### Step 4: Community Detection

Connected components and modularity-based clustering identify job communities—groups with similar resource profiles.

## Bipartite Network Approach

For advanced analysis, NØMAÐ implements Vilhena & Antonelli's bipartite approach:
```
┌──────────────┐          ┌──────────────┐
│    Jobs      │──────────│ Resource Bins│
├──────────────┤          ├──────────────┤
│ job_1001     │────┬────▶│ cpu_high     │
│ job_1002     │────┤     │ cpu_low      │
│ job_1003     │────┼────▶│ mem_high     │
│ job_1004     │────┤     │ mem_low      │
│ ...          │────┴────▶│ io_nfs_heavy │
└──────────────┘          └──────────────┘
```

1. **Discretize features** into bins (e.g., cpu_high, cpu_low)
2. **Create bipartite graph**: jobs connected to their resource bins
3. **Project onto job-job network**: jobs sharing bins are connected
4. **Weight by overlap**: more shared bins = stronger connection

This approach:

- Treats each resource bin as a "site" (biogeography analogy)
- Reveals emergent behavioral regions
- Handles missing data gracefully

## Network Metrics

### Assortativity

Measures whether failed jobs cluster together:

$$r = \frac{\sum_{ij}(A_{ij} - k_i k_j / 2m) \delta(c_i, c_j)}{2m - \sum_{ij}(k_i k_j / 2m) \delta(c_i, c_j)}$$

- **Positive**: Failed jobs connect to failed jobs (pattern exists)
- **Zero**: Random mixing (no predictive signal)
- **Negative**: Failed jobs connect to successful jobs (unusual)

### Clustering Coefficient

Local clustering indicates behavioral cohesion:

$$C_i = \frac{2 |\{e_{jk}\}|}{k_i(k_i - 1)}$$

High clustering = consistent failure patterns.

### Statistical Significance

NØMAÐ tests whether observed patterns exceed random chance using permutation tests:

1. Shuffle failure labels 1000 times
2. Compute metric for each shuffle
3. Calculate z-score: $z = (observed - \mu_{null}) / \sigma_{null}$
4. Report significance if $|z| > 2$

## Visualization

The dashboard provides a 3D force-directed network visualization:

- **Node color**: Green (healthy) to Red (failed)
- **Node position**: Fruchterman-Reingold layout
- **Axes**: NFS ratio, local I/O, I/O wait
- **Regions**: "Safe zone" vs "danger zone" emerge from data

## References

Vilhena, D.A., Antonelli, A. (2015). A network approach for identifying and delimiting biogeographical regions. *Nature Communications* 6:6848. DOI: [10.1038/ncomms7848](https://doi.org/10.1038/ncomms7848)
