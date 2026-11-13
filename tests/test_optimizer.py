import numpy as np
import pandas as pd

from myquant.portfolio.optimizer import IndexEnhancementOptimizer, OptimizationConfig


def test_optimizer_returns_a_labeled_solution_or_safe_fallback() -> None:
    codes = pd.Index([f"{i:06d}.SZ" for i in range(60)])
    alpha = pd.Series(np.linspace(-1, 1, len(codes)), index=codes)
    benchmark = pd.Series(1 / len(codes), index=codes)
    covariance = pd.DataFrame(np.eye(len(codes)) * 0.04, index=codes, columns=codes)
    industries = pd.Series(["A"] * 30 + ["B"] * 30, index=codes)
    result = IndexEnhancementOptimizer(OptimizationConfig()).optimize(alpha, benchmark, covariance, industries)
    assert result.status in {"optimal", "heuristic_no_cvxpy"}
    assert abs(result.weights.sum() - 1) < 1e-6
