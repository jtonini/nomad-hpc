"""
Ensemble model combining GNN, LSTM, and Autoencoder.

- GNN: Network structure (what fails)
- LSTM: Temporal patterns (when it fails)
- Autoencoder: Anomaly detection (is this normal)

Combined prediction provides higher confidence alerts.
"""

try:
    import torch
    import torch.nn.functional as F
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

from .gnn import FAILURE_NAMES


if HAS_TORCH:
    
    class FailureEnsemble:
        """
        Ensemble of GNN, LSTM, and Autoencoder for failure prediction.
        
        Combines predictions using weighted voting or stacking.
        """
        
        def __init__(self, gnn_model=None, lstm_model=None, autoencoder=None,
                     weights: dict = None):
            """
            Args:
                gnn_model: Trained FailureGNN
                lstm_model: Trained FailureLSTM
                autoencoder: Trained JobAutoencoder
                weights: Dict of model weights {'gnn': 0.4, 'lstm': 0.3, 'ae': 0.3}
            """
            self.gnn = gnn_model
            self.lstm = lstm_model
            self.ae = autoencoder
            
            self.weights = weights or {'gnn': 0.5, 'lstm': 0.3, 'ae': 0.2}
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
            
            # Move models to device
            if self.gnn:
                self.gnn.to(self.device)
            if self.lstm:
                self.lstm.to(self.device)
            if self.ae:
                self.ae.to(self.device)
        
        def predict_gnn(self, x, edge_index):
            """GNN prediction: class probabilities."""
            if self.gnn is None:
                return None
            self.gnn.eval()
            with torch.no_grad():
                x = x.to(self.device)
                edge_index = edge_index.to(self.device)
                logits = self.gnn(x, edge_index)
                return F.softmax(logits, dim=1)
        
        def predict_lstm(self, trajectories):
            """LSTM prediction: class probabilities."""
            if self.lstm is None:
                return None
            self.lstm.eval()
            with torch.no_grad():
                x = trajectories.to(self.device)
                logits = self.lstm(x)
                return F.softmax(logits, dim=1)
        
        def predict_anomaly(self, features):
            """Autoencoder: anomaly scores (higher = more anomalous)."""
            if self.ae is None:
                return None
            self.ae.eval()
            with torch.no_grad():
                x = features.to(self.device)
                errors = self.ae.reconstruction_error(x)
                # Normalize to 0-1 range
                errors = (errors - errors.min()) / (errors.max() - errors.min() + 1e-8)
                return errors
        
        def predict(self, gnn_data=None, lstm_data=None, ae_data=None,
                    return_components: bool = False) -> dict:
            """
            Combined ensemble prediction.
            
            Args:
                gnn_data: Tuple of (x, edge_index) for GNN
                lstm_data: Tensor of trajectories for LSTM
                ae_data: Tensor of features for Autoencoder
                return_components: If True, return individual model predictions
                
            Returns:
                Dict with predictions, confidences, and optionally components
            """
            n_samples = None
            components = {}
            
            # GNN predictions
            gnn_probs = None
            if gnn_data and self.gnn:
                x, edge_index = gnn_data
                gnn_probs = self.predict_gnn(x, edge_index)
                n_samples = gnn_probs.size(0)
                components['gnn'] = gnn_probs
            
            # LSTM predictions
            lstm_probs = None
            if lstm_data is not None and self.lstm:
                lstm_probs = self.predict_lstm(lstm_data)
                n_samples = n_samples or lstm_probs.size(0)
                components['lstm'] = lstm_probs
            
            # Autoencoder anomaly scores
            ae_scores = None
            if ae_data is not None and self.ae:
                ae_scores = self.predict_anomaly(ae_data)
                n_samples = n_samples or ae_scores.size(0)
                components['ae_anomaly'] = ae_scores
            
            if n_samples is None:
                return {'error': 'No valid predictions'}
            
            # Combine predictions
            combined_probs = torch.zeros(n_samples, 8, device=self.device)
            total_weight = 0
            
            if gnn_probs is not None:
                combined_probs += self.weights['gnn'] * gnn_probs
                total_weight += self.weights['gnn']
            
            if lstm_probs is not None:
                combined_probs += self.weights['lstm'] * lstm_probs
                total_weight += self.weights['lstm']
            
            if ae_scores is not None:
                # Convert anomaly score to failure probability boost
                # High anomaly = boost failure classes, reduce SUCCESS
                ae_boost = ae_scores.unsqueeze(1).expand(-1, 8)
                # Reduce success probability, boost failure probabilities
                ae_adjustment = torch.zeros_like(combined_probs)
                ae_adjustment[:, 0] = -ae_scores  # Reduce SUCCESS
                ae_adjustment[:, 1:] = ae_scores.unsqueeze(1).expand(-1, 7) / 7  # Boost failures
                combined_probs += self.weights['ae'] * ae_adjustment
                total_weight += self.weights['ae']
            
            # Normalize
            if total_weight > 0:
                combined_probs = combined_probs / total_weight
            
            # Ensure valid probabilities
            combined_probs = F.softmax(combined_probs, dim=1)
            
            # Get predictions
            pred_classes = combined_probs.argmax(dim=1)
            confidences = combined_probs.max(dim=1).values
            
            result = {
                'predictions': pred_classes.cpu().tolist(),
                'confidences': confidences.cpu().tolist(),
                'probabilities': combined_probs.cpu(),
                'predicted_names': [FAILURE_NAMES.get(p, f'Class {p}') for p in pred_classes.cpu().tolist()]
            }
            
            if return_components:
                result['components'] = {k: v.cpu() if torch.is_tensor(v) else v 
                                        for k, v in components.items()}
            
            return result
        
        def get_high_risk_jobs(self, predictions: dict, threshold: float = 0.7) -> list:
            """
            Identify jobs at high risk of failure.
            
            Args:
                predictions: Output from predict()
                threshold: Confidence threshold for failure prediction
                
            Returns:
                List of (job_idx, predicted_failure, confidence) tuples
            """
            high_risk = []
            
            for idx, (pred, conf) in enumerate(zip(predictions['predictions'], 
                                                    predictions['confidences'])):
                if pred != 0 and conf >= threshold:  # Not SUCCESS and high confidence
                    high_risk.append({
                        'job_idx': idx,
                        'predicted_failure': FAILURE_NAMES.get(pred, f'Class {pred}'),
                        'confidence': conf,
                        'probabilities': predictions['probabilities'][idx].tolist()
                    })
            
            # Sort by confidence
            high_risk.sort(key=lambda x: -x['confidence'])
            return high_risk


    def train_ensemble(jobs: list, edges: list, epochs: int = 100,
                       verbose: bool = True) -> dict:
        """
        Train all three models and create ensemble.
        
        Args:
            jobs: List of job dicts
            edges: Similarity edges
            epochs: Training epochs per model
            verbose: Print progress
            
        Returns:
            Dict with ensemble and individual models
        """
        from .gnn_torch import train_failure_gnn, prepare_pyg_data
        from .lstm import train_failure_lstm, generate_synthetic_trajectories
        from .autoencoder import train_anomaly_detector
        
        results = {}
        
        # 1. Train GNN
        if verbose:
            print("=" * 60)
            print("Training GNN (network structure)")
            print("=" * 60)
        gnn_result = train_failure_gnn(jobs, edges, epochs=epochs, verbose=verbose)
        results['gnn'] = gnn_result
        
        # 2. Train LSTM
        if verbose:
            print("\n" + "=" * 60)
            print("Training LSTM (temporal patterns)")
            print("=" * 60)
        lstm_result = train_failure_lstm(jobs, epochs=epochs, verbose=verbose)
        results['lstm'] = lstm_result
        
        # 3. Train Autoencoder
        if verbose:
            print("\n" + "=" * 60)
            print("Training Autoencoder (anomaly detection)")
            print("=" * 60)
        ae_result = train_anomaly_detector(jobs, epochs=epochs, verbose=verbose)
        results['autoencoder'] = ae_result
        
        # 4. Create ensemble
        ensemble = FailureEnsemble(
            gnn_model=gnn_result['model'],
            lstm_model=lstm_result['model'],
            autoencoder=ae_result['model']
        )
        results['ensemble'] = ensemble
        
        # 5. Evaluate ensemble
        if verbose:
            print("\n" + "=" * 60)
            print("Ensemble Summary")
            print("=" * 60)
            print(f"GNN test accuracy:    {gnn_result['test_results']['accuracy']:.2%}")
            print(f"LSTM test accuracy:   {lstm_result['test_accuracy']:.2%}")
            print(f"Autoencoder F1:       {ae_result['results'].get('f1', 0):.2%}")
        
        return results


else:
    class FailureEnsemble:
        def __init__(self, *args, **kwargs):
            raise ImportError("PyTorch required")
    
    def train_ensemble(*args, **kwargs):
        raise ImportError("PyTorch required")


if __name__ == '__main__':
    if not HAS_TORCH:
        print("PyTorch not available")
    else:
        import sqlite3
        
        print("Training Ensemble on simulation data...")
        print(f"Device: {'CUDA' if torch.cuda.is_available() else 'CPU'}")
        print()
        
        # Load data
        conn = sqlite3.connect('vm-simulation/nomade.db')
        conn.row_factory = sqlite3.Row
        jobs = [dict(row) for row in conn.execute("SELECT * FROM jobs").fetchall()]
        print(f"Loaded {len(jobs)} jobs")
        
        # Build edges
        from nomade.viz.server import build_similarity_network
        network = build_similarity_network(jobs, method='cosine', threshold=0.7, max_edges=15000)
        edges = [{'source': e['source'], 'target': e['target']} for e in network['edges']]
        print(f"Built {len(edges)} cosine edges")
        
        # Train ensemble
        results = train_ensemble(jobs, edges, epochs=100, verbose=True)
        
        print("\n" + "=" * 60)
        print("ENSEMBLE READY")
        print("=" * 60)


def train_and_save_ensemble(db_path: str, models_dir: str = None, 
                            epochs: int = 100, verbose: bool = True) -> dict:
    """
    Train ensemble on database jobs and save results.
    
    This is the main entry point for continuous training.
    """
    import sqlite3
    from pathlib import Path
    from .persistence import init_ml_tables, save_predictions_to_db
    
    if models_dir is None:
        models_dir = Path(db_path).parent / 'ml_models'
    
    # Initialize tables
    init_ml_tables(db_path)
    
    # Load jobs from database
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    jobs = [dict(row) for row in conn.execute("SELECT * FROM jobs").fetchall()]
    conn.close()
    
    if not jobs:
        return {'status': 'error', 'message': 'No jobs in database'}
    
    if verbose:
        print(f"Training ensemble on {len(jobs)} jobs...")
    
    # Build edges
    from nomade.viz.server import build_similarity_network
    network = build_similarity_network(jobs, method='cosine', threshold=0.7, max_edges=15000)
    edges = [{'source': e['source'], 'target': e['target']} for e in network['edges']]
    if verbose:
        print(f"Built {len(edges)} cosine similarity edges")

    # Train ensemble
    results = train_ensemble(jobs, edges, epochs=epochs, verbose=verbose)
    
    # Prepare predictions for storage
    gnn_results = results['gnn']['test_results']
    lstm_results = results['lstm']
    ae_results = results['autoencoder']['results']
    
    # Get ensemble predictions for all jobs
    ensemble = results['ensemble']
    
    from .gnn_torch import prepare_pyg_data
    from .autoencoder import prepare_autoencoder_data
    from .lstm import generate_synthetic_trajectories
    
    gnn_data = prepare_pyg_data(jobs, edges)
    ae_features, _, _ = prepare_autoencoder_data(jobs)
    trajectories, _ = generate_synthetic_trajectories(jobs)
    
    import torch
    traj_tensor = torch.stack([torch.tensor(t, dtype=torch.float) for t in trajectories])
    
    # Run ensemble prediction
    # Get device and move data
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    gnn_data.x = gnn_data.x.to(device)
    gnn_data.edge_index = gnn_data.edge_index.to(device)
    traj_tensor = traj_tensor.to(device)
    ae_features = ae_features.to(device)
    
    pred_result = ensemble.predict(
        gnn_data=(gnn_data.x, gnn_data.edge_index),
        lstm_data=traj_tensor,
        ae_data=ae_features,
        return_components=True
    )
    
    # Compute anomaly scores from autoencoder
    # Get device from trained model
    device = next(results["autoencoder"]["model"].parameters()).device
    
    ae_model = results["autoencoder"]["model"]
    ae_model.eval()
    with torch.no_grad():
        ae_errors = ae_model.reconstruction_error(ae_features.to(device))
    
    threshold = results['autoencoder']['threshold']
    
    # Build job-level predictions
    job_predictions = []
    high_risk = []
    
    for i, job in enumerate(jobs):
        pred_class = pred_result['predictions'][i]
        confidence = pred_result['confidences'][i]
        anomaly_score = float(ae_errors[i])
        is_anomaly = anomaly_score > threshold
        
        job_pred = {
            'job_id': job.get('job_id', str(i)),
            'job_idx': i,
            'predicted_class': pred_class,
            'predicted_name': FAILURE_NAMES.get(pred_class, f'Class {pred_class}'),
            'confidence': round(confidence, 4),
            'anomaly_score': round(anomaly_score, 4),
            'is_anomaly': is_anomaly,
            'actual_failure': job.get('failure_reason', 0)
        }
        
        job_predictions.append(job_pred)
        
        # High risk: anomaly OR predicted failure with high confidence
        if is_anomaly or (pred_class != 0 and confidence > 0.5):
            high_risk.append(job_pred)
    
    # Sort high risk by anomaly score
    high_risk.sort(key=lambda x: -x['anomaly_score'])
    
    # Build summary
    predictions = {
        'status': 'trained',
        'n_jobs': len(jobs),
        'n_anomalies': sum(1 for jp in job_predictions if jp['is_anomaly']),
        'threshold': round(threshold, 4),
        'high_risk': high_risk[:100],  # Top 100
        'job_predictions': job_predictions,
        'summary': {
            'gnn_accuracy': round(gnn_results['accuracy'], 4),
            'lstm_accuracy': round(lstm_results['test_accuracy'], 4),
            'ae_precision': round(ae_results.get('precision', 0), 4),
            'ae_recall': round(ae_results.get('recall', 0), 4),
            'epochs': epochs,
            'n_edges': len(edges)
        }
    }
    
    # Save to database
    prediction_id = save_predictions_to_db(db_path, predictions)
    predictions['prediction_id'] = prediction_id
    
    if verbose:
        print(f"\n{'='*60}")
        print("ENSEMBLE TRAINED AND SAVED")
        print(f"{'='*60}")
        print(f"Prediction ID: {prediction_id}")
        print(f"High-risk jobs: {len(high_risk)}")
        print(f"Anomalies: {predictions['n_anomalies']}")
    
    return predictions
