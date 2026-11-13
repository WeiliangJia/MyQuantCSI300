from __future__ import annotations

from pathlib import Path

import pandas as pd

from myquant.simulation.metrics import performance_report


def write_weekly_report(
    output_path: Path,
    nav: pd.DataFrame,
    benchmark: pd.DataFrame,
    fills: pd.DataFrame,
    factor_attribution: pd.DataFrame | None = None,
) -> Path:
    """Write a compact Markdown report for the latest simulation week."""
    metrics = performance_report(nav, benchmark)
    latest_date = pd.Timestamp(nav["trade_date"].max()).date()
    lines = [f"# 模拟盘周报 — {latest_date}", "", "## 相对沪深300绩效", ""]
    lines.extend([f"- {key}: {value:.4%}" if "ratio" not in key else f"- {key}: {value:.2f}" for key, value in metrics.items()])
    lines.extend(["", "## 成交", "", f"- 本周订单/成交记录：{len(fills)}"])
    if factor_attribution is not None and not factor_attribution.empty:
        lines.extend(["", "## 因子归因", "", factor_attribution.tail(10).to_markdown(index=False)])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
