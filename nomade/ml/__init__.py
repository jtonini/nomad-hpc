"""
NØMADE Machine Learning Module

- GNN: What fails (network structure)
- LSTM: When it fails (temporal patterns)
- Autoencoder: Is this normal (anomaly detection)
- Ensemble: Combined prediction
- Persistence: Save/load models and predictions
"""

from .gnn import (
    SimpleGNN, GNNConfig, prepare_job_features,
    build_adjacency_from_edges, evaluate_gnn, FAILURE_NAMES
)

try:
    from .gnn_torch import (
        is_torch_available, FocalLoss, FailureGNN, GNNTrainer,
        train_failure_gnn, prepare_pyg_data
    )
    from .lstm import (
        FailureLSTM, LSTMTrainer, train_failure_lstm,
        JobTrajectoryDataset, generate_synthetic_trajectories
    )
    from .autoencoder import (
        JobAutoencoder, AutoencoderTrainer, train_anomaly_detector,
        prepare_autoencoder_data
    )
    from .ensemble import (
        FailureEnsemble, train_ensemble, train_and_save_ensemble
    )
    from .persistence import (
        init_ml_tables, save_predictions_to_db, load_predictions_from_db,
        save_ensemble_models, load_latest_models, get_prediction_history
    )
except ImportError:
    is_torch_available = lambda: False

__all__ = [
    'SimpleGNN', 'GNNConfig', 'prepare_job_features',
    'build_adjacency_from_edges', 'evaluate_gnn', 'FAILURE_NAMES',
    'is_torch_available', 'FocalLoss', 'FailureGNN', 'GNNTrainer',
    'train_failure_gnn', 'prepare_pyg_data',
    'FailureLSTM', 'LSTMTrainer', 'train_failure_lstm',
    'JobTrajectoryDataset', 'generate_synthetic_trajectories',
    'JobAutoencoder', 'AutoencoderTrainer', 'train_anomaly_detector',
    'prepare_autoencoder_data',
    'FailureEnsemble', 'train_ensemble', 'train_and_save_ensemble',
    'init_ml_tables', 'save_predictions_to_db', 'load_predictions_from_db',
    'save_ensemble_models', 'load_latest_models', 'get_prediction_history'
]

try:
    from .continuous import ContinuousLearner
except ImportError:
    pass
