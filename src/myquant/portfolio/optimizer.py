from __future__ import annotations

from dataclasses import dataclass

try:  # cvxpy is preferred, but the research stack remains readable without native solver wheels.
    import cvxpy as cp
except ImportError:  # pragma: no cover - exercised by environments without optional solver support
    cp = None  # type: ignore[assignment]
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class OptimizationConfig:
    holdings: int = 50
    min_weight: float = 0.005
    max_weight: float = 0.04
    max_one_way_turnover: float = 0.20
    max_tracking_error: float = 0.08
    max_sector_deviation: float = 0.05
    risk_aversion: float = 8.0
    transaction_cost_penalty: float = 0.002


@dataclass(frozen=True)
class OptimizationResult:
    weights: pd.Series
    status: str
    expected_tracking_error: float
    one_way_turnover: float


def estimate_covariance(returns: pd.DataFrame, annualization: int = 252) -> pd.DataFrame:
    """Two-year daily covariance with diagonal shrinkage, suitable for transparent v1 risk control."""
    cov = returns.tail(504).cov() * annualization
    diagonal = np.diag(np.diag(cov.to_numpy()))
    shrunk = 0.8 * cov.to_numpy() + 0.2 * diagonal
    return pd.DataFrame(shrunk, index=cov.index, columns=cov.columns)


class IndexEnhancementOptimizer:
    def __init__(self, config: OptimizationConfig) -> None:
        self.config = config

    def optimize(
        self,
        alpha: pd.Series,
        benchmark: pd.Series,
        covariance: pd.DataFrame,
        industries: pd.Series,
        previous: pd.Series | None = None,
    ) -> OptimizationResult:
        universe = benchmark.index.intersection(alpha.index).intersection(covariance.index)
        if len(universe) < self.config.holdings:
            return self._fallback(previous, benchmark, "insufficient_universe")
        alpha = alpha.reindex(universe).fillna(alpha.min() if not alpha.empty else 0.0)
        benchmark = benchmark.reindex(universe).fillna(0.0)
        benchmark = benchmark / benchmark.sum()
        covariance = covariance.reindex(index=universe, columns=universe).fillna(0.0)
        covariance = (covariance + covariance.T) / 2 + np.eye(len(universe)) * 1e-8
        industries = industries.reindex(universe).fillna("UNKNOWN")
        previous = (previous.reindex(universe).fillna(0.0) if previous is not None else benchmark.copy())
        selected = self._select_candidates(alpha, industries)
        if cp is None:
            return self._heuristic(alpha, benchmark, covariance, previous, selected)
        selection_mask = universe.isin(selected)
        n = len(universe)
        weights = cp.Variable(n)
        active = weights - benchmark.to_numpy()
        constraints = [
            cp.sum(weights) == 1,
            weights >= 0,
            weights[~selection_mask] == 0,
            weights[selection_mask] >= self.config.min_weight,
            weights[selection_mask] <= self.config.max_weight,
            cp.sum(cp.pos(weights - previous.to_numpy())) <= self.config.max_one_way_turnover,
            cp.quad_form(active, cp.psd_wrap(covariance.to_numpy())) <= self.config.max_tracking_error**2,
        ]
        for sector in industries.unique():
            mask = (industries == sector).to_numpy().astype(float)
            benchmark_sector = float(mask @ benchmark.to_numpy())
            constraints += [
                mask @ weights <= benchmark_sector + self.config.max_sector_deviation,
                mask @ weights >= max(0.0, benchmark_sector - self.config.max_sector_deviation),
            ]
        objective = cp.Maximize(
            alpha.to_numpy() @ weights
            - self.config.risk_aversion * cp.quad_form(active, cp.psd_wrap(covariance.to_numpy()))
            - self.config.transaction_cost_penalty * cp.sum(cp.abs(weights - previous.to_numpy()))
        )
        problem = cp.Problem(objective, constraints)
        try:
            problem.solve(solver=cp.CLARABEL)
        except (cp.SolverError, ValueError):
            return self._fallback(previous, benchmark, "solver_error")
        if problem.status not in {cp.OPTIMAL, cp.OPTIMAL_INACCURATE} or weights.value is None:
            return self._fallback(previous, benchmark, f"{problem.status}")
        outcome = pd.Series(np.maximum(weights.value, 0.0), index=universe)
        active_values = outcome - benchmark
        te = float(np.sqrt(active_values.to_numpy() @ covariance.to_numpy() @ active_values.to_numpy()))
        turnover = float(np.maximum(outcome - previous, 0.0).sum())
        return OptimizationResult(outcome, "optimal", te, turnover)

    def _select_candidates(self, alpha: pd.Series, industries: pd.Series) -> pd.Index:
        ranked = alpha.sort_values(ascending=False)
        selected: list[str] = []
        for sector in industries.loc[ranked.index].drop_duplicates():
            candidate = ranked.index[industries.loc[ranked.index] == sector]
            if len(candidate):
                selected.append(candidate[0])
        for code in ranked.index:
            if code not in selected:
                selected.append(code)
            if len(selected) >= self.config.holdings:
                break
        return pd.Index(selected[: self.config.holdings])

    @staticmethod
    def _fallback(previous: pd.Series | None, benchmark: pd.Series, status: str) -> OptimizationResult:
        weights = previous.copy() if previous is not None and previous.sum() > 0 else benchmark / benchmark.sum()
        return OptimizationResult(weights, status, 0.0, 0.0)

    def _heuristic(
        self,
        alpha: pd.Series,
        benchmark: pd.Series,
        covariance: pd.DataFrame,
        previous: pd.Series,
        selected: pd.Index,
    ) -> OptimizationResult:
        """Deterministic research fallback; callers must reject it if risk checks fail.

        A production run should install cvxpy and use the constrained solution. This fallback
        intentionally exposes its status instead of silently claiming that hard constraints held.
        """
        ranks = alpha.loc[selected].rank(method="first", ascending=False)
        tilt = (len(selected) + 1 - ranks) / (len(selected) * (len(selected) + 1) / 2)
        weights = pd.Series(0.0, index=benchmark.index)
        weights.loc[selected] = tilt
        weights = weights.clip(upper=self.config.max_weight)
        weights.loc[selected] = weights.loc[selected].clip(lower=self.config.min_weight)
        weights = weights / weights.sum()
        active = weights - benchmark
        te = float(np.sqrt(active.to_numpy() @ covariance.to_numpy() @ active.to_numpy()))
        turnover = float(np.maximum(weights - previous, 0.0).sum())
        return OptimizationResult(weights, "heuristic_no_cvxpy", te, turnover)
