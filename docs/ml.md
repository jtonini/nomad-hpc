# ML Framework

NØMAÐ's machine learning framework combines multiple models for robust job failure prediction.

## Architecture Overview
```
┌─────────────────────────────────────────────────────────────────┐
│                    ML Prediction Pipeline                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐    ┌──────────────────────────────────────────┐   │
│  │ Job Data │───▶│           Feature Engineering            │   │
│  └──────────┘    └──────────────────────────────────────────┘   │
│                                    │                            │
│                    ┌───────────────┼───────────────┐            │
│                    ▼               ▼               ▼            │
│              ┌──────────┐   ┌──────────┐   ┌──────────────┐     │
│              │   GNN    │   │   LSTM   │   │ Autoencoder  │     │
│              │ (Graph)  │   │ (Temporal)│   │  (Anomaly)  │     │
│              └────┬─────┘   └────┬─────┘   └──────┬───────┘     │
│                   │              │                │             │
│                   └──────────────┼────────────────┘             │
│                                  ▼                              │
│                         ┌──────────────┐                        │
│                         │   Ensemble   │                        │
│                         │   Combiner   │                        │
│                         └──────┬───────┘                        │
│                                │                                │
│                                ▼                                │
│                      ┌─────────────────┐                        │
│                      │  Risk Score     │                        │
│                      │   (0.0 - 1.0)   │                        │
│                      └─────────────────┘                        │
└─────────────────────────────────────────────────────────────────┘
```

## Model Components

### 1. Graph Neural Network (GNN)

The GNN leverages the job similarity network structure to propagate failure signals.

**Intuition**: Jobs connected in the similarity network share behavioral profiles. If a job's neighbors have high failure rates, the job itself is at elevated risk.

**Architecture**:
```
Input: Node features (17-dim) + Adjacency matrix
  │
  ▼
GraphConv Layer (17 → 64, ReLU)
  │
  ▼
GraphConv Layer (64 → 32, ReLU)
  │
  ▼
Global Mean Pooling
  │
  ▼
Dense Layer (32 → 1, Sigmoid)
  │
  ▼
Output: Failure probability
```

**Key insight**: The GNN learns that certain network neighborhoods are "failure-prone regions" in feature space.

### 2. LSTM (Long Short-Term Memory)

The LSTM detects temporal patterns and early warning trajectories.

**Intuition**: Job failures often have precursors—accelerating memory pressure, increasing I/O wait, declining CPU efficiency. The LSTM learns these temporal signatures.

**Architecture**:
```
Input: Time series of metrics (sequence_length × features)
  │
  ▼
LSTM Layer (hidden_size=64)
  │
  ▼
LSTM Layer (hidden_size=32)
  │
  ▼
Dense Layer (32 → 16, ReLU)
  │
  ▼
Dense Layer (16 → 1, Sigmoid)
  │
  ▼
Output: Failure probability
```

**Sequence construction**: For each job, we collect metrics at regular intervals (default: every 30 seconds) and form a time series.

### 3. Autoencoder (Anomaly Detection)

The autoencoder identifies jobs that deviate from normal behavior.

**Intuition**: Train on successful jobs to learn "normal" patterns. Jobs that reconstruct poorly are anomalous—and anomalies correlate with failures.

**Architecture**:
```
Input: Feature vector (17-dim)
  │
  ▼
Encoder:
  Dense (17 → 12, ReLU)
  Dense (12 → 8, ReLU)
  Dense (8 → 4, ReLU)  ← Latent space
  │
  ▼
Decoder:
  Dense (4 → 8, ReLU)
  Dense (8 → 12, ReLU)
  Dense (12 → 17, Sigmoid)
  │
  ▼
Reconstruction Error = MSE(input, output)
  │
  ▼
Output: Anomaly score (higher = more anomalous)
```

**Training**: Only on COMPLETED (successful) jobs. The model learns what "normal" looks like.

**Inference**: High reconstruction error suggests the job doesn't fit normal patterns.

## Ensemble Combination

Individual model predictions are combined using weighted averaging:

$$\text{Risk Score} = w_1 \cdot P_{GNN} + w_2 \cdot P_{LSTM} + w_3 \cdot S_{AE}$$

**Default weights**:

| Model | Weight | Rationale |
|-------|--------|-----------|
| GNN | 0.4 | Strong structural signal |
| LSTM | 0.35 | Good temporal patterns |
| Autoencoder | 0.25 | Catches outliers |

Weights are tunable via configuration or can be learned via cross-validation.

## Training Pipeline

### Data Preparation
```bash
nomad train --prepare
```

1. Extract completed jobs from database
2. Compute feature vectors
3. Label: COMPLETED=0, FAILED/TIMEOUT/CANCELLED=1
4. Split: 80% train, 10% validation, 10% test
5. Handle class imbalance (failures are rare):
   - Oversample failures
   - Or use class weights

### Model Training
```bash
nomad train
```

For each model:

1. **GNN**: Train on similarity graph with node labels
2. **LSTM**: Train on metric time series
3. **Autoencoder**: Train reconstruction on successful jobs only

Training outputs:
```
~/.local/share/nomad/models/
├── gnn_model.pt
├── lstm_model.pt
├── autoencoder_model.pt
└── ensemble_weights.json
```

### Hyperparameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `learning_rate` | 0.001 | Adam optimizer LR |
| `epochs` | 100 | Training epochs |
| `batch_size` | 32 | Mini-batch size |
| `hidden_dim` | 64 | Hidden layer size |
| `dropout` | 0.2 | Dropout rate |
| `similarity_threshold` | 0.7 | For GNN graph |

## Prediction Pipeline

### Real-Time Scoring
```bash
nomad predict
```

For running jobs:

1. Compute current feature vector
2. Query similar historical jobs
3. Run through ensemble
4. Output risk score (0.0 - 1.0)

### Risk Score Interpretation

| Score | Level | Recommended Action |
|-------|-------|-------------------|
| 0.0 - 0.3 | Low | No action needed |
| 0.3 - 0.6 | Moderate | Monitor more frequently |
| 0.6 - 0.8 | High | Alert user, suggest changes |
| 0.8 - 1.0 | Critical | Immediate intervention |

### Actionable Recommendations

When risk is elevated, NØMAÐ provides specific recommendations based on which features contribute most:
```
⚠️ Job 12345 has elevated failure risk (0.72)

Contributing factors:
  • High NFS write ratio (0.89) — 3x normal
  • Low CPU efficiency (23%) — below 50% threshold
  
Recommendations:
  • Consider using local scratch: export TMPDIR=/scratch/$USER
  • Reduce core count if not using parallelism
```

## Evaluation Metrics

### Classification Metrics

| Metric | Formula | Target |
|--------|---------|--------|
| Precision | TP / (TP + FP) | > 0.7 |
| Recall | TP / (TP + FN) | > 0.8 |
| F1 Score | 2 × P × R / (P + R) | > 0.75 |
| AUC-ROC | Area under ROC curve | > 0.85 |

### Operational Metrics

| Metric | Description |
|--------|-------------|
| Lead time | How early before failure is risk elevated? |
| False alarm rate | Alerts that didn't result in failure |
| Coverage | % of failures that were predicted |

## CLI Commands
```bash
# Train all models
nomad train

# Train specific model
nomad train --model gnn
nomad train --model lstm
nomad train --model autoencoder

# Run predictions
nomad predict

# Generate report
nomad report

# View model performance
nomad ml status
```

## Configuration

In `nomad.toml`:
```toml
[ml]
enabled = true
similarity_threshold = 0.7
retrain_interval_days = 7

[ml.ensemble]
gnn_weight = 0.4
lstm_weight = 0.35
autoencoder_weight = 0.25

[ml.alerts]
high_risk_threshold = 0.7
alert_on_prediction = true
```
