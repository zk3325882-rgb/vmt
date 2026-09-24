"""Phase 6 — ML, probability calibration & walk-forward learning.

Calibrated probabilities are historical model estimates conditioned on
similar past observations; they are NOT guaranteed future outcomes."""
from app.ml.features import (FEATURE_VERSION, feature_columns,
                             is_forbidden_feature, vectorize)
from app.ml.dataset import build_dataset
from app.ml.calibration import fit_calibrator, reliability_bins
from app.ml.evaluation import binary_metrics, class_distribution
from app.ml.registry import ModelRegistry
from app.ml.training import train_model, walk_forward
from app.ml.prediction import PredictionEngine
from app.ml.drift import check_drift

__all__ = ["FEATURE_VERSION", "feature_columns", "vectorize", "build_dataset",
           "fit_calibrator", "reliability_bins", "binary_metrics",
           "ModelRegistry", "train_model", "walk_forward", "PredictionEngine",
           "check_drift", "is_forbidden_feature"]
