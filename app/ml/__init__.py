"""ML package — re-exports the training entry point.

`train_pipeline` is re-exported for convenience (`from app.ml import
train_pipeline`). Declared in `__all__` so linters do not flag it as unused.
"""
from app.ml.pipeline import train_pipeline

__all__ = ["train_pipeline"]
