# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Machine Learning Module

- GNN: What fails (network structure)
- LSTM: When it fails (temporal patterns)
- Autoencoder: Is this normal (anomaly detection)
- Ensemble: Combined prediction
- Persistence: Save/load models and predictions
"""

from .gnn import (
    FAILURE_NAMES,
    GNNConfig,
    SimpleGNN,
    build_adjacency_from_edges,
    evaluate_gnn,
    prepare_job_features,
)

def _check_numpy_abi() -> bool:
    """Check if NumPy ABI is compatible with installed C extensions.

    pyarrow, numexpr, and bottleneck compiled against NumPy 1.x crash
    fatally (not catchable) under NumPy 2.x. We check package metadata
    to detect the mismatch without importing the broken modules.
    """
    try:
        import numpy as np
        np_major = int(np.__version__.split(".")[0])
        if np_major < 2:
            return True  # NumPy 1.x — no ABI issue

        # NumPy 2.x installed. Check if pyarrow is from anaconda (likely 1.x-built)
        from importlib.metadata import requires, PackageNotFoundError
        try:
            import importlib.util
            pa_spec = importlib.util.find_spec("pyarrow")
            if pa_spec and pa_spec.origin and "/anaconda" in str(pa_spec.origin):
                return False  # anaconda pyarrow almost certainly NumPy 1.x-built
            # Also check if pyarrow METADATA has numpy<2 requirement
            from importlib.metadata import metadata
            pa_meta = metadata("pyarrow")
            requires_str = str(pa_meta.get_all("Requires-Dist") or "")
            if "numpy<2" in requires_str or "numpy (<2" in requires_str:
                return False
        except Exception:
            pass

        return True
    except Exception:
        return True


_HAS_TORCH_GEO = _check_numpy_abi()

try:
    if not _HAS_TORCH_GEO:
        raise ImportError("torch_geometric not usable on this system")
    from .autoencoder import (
        AutoencoderTrainer,
        JobAutoencoder,
        prepare_autoencoder_data,
        train_anomaly_detector,
    )
    from .ensemble import FailureEnsemble, train_and_save_ensemble, train_ensemble
    from .gnn_torch import (
        FailureGNN,
        FocalLoss,
        GNNTrainer,
        is_torch_available,
        prepare_pyg_data,
        train_failure_gnn,
    )
    from .lstm import (
        FailureLSTM,
        JobTrajectoryDataset,
        LSTMTrainer,
        generate_synthetic_trajectories,
        train_failure_lstm,
    )
    from .persistence import (
        get_prediction_history,
        init_ml_tables,
        load_latest_models,
        load_predictions_from_db,
        save_ensemble_models,
        save_predictions_to_db,
    )
except Exception:
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
except Exception:
    pass
