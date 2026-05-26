# -*- coding: utf-8 -*-
"""Bridge to a_stock_strategy screening results."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

_log = logging.getLogger(__name__)


def load_screen_results(screener_project: str, trade_date: str) -> list[dict]:
    """Load screening results from a_stock_strategy project.

    If results file doesn't exist, run the screen first.
    Returns list of candidate dicts.
    """
    base = Path(screener_project).resolve()
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    result_path = base / "outputs" / f"screen_{trade_date}.json"
    if result_path.exists():
        results = json.loads(result_path.read_text("utf-8"))
        _log.info("Loaded %d candidates from %s", len(results), result_path)
        return results

    _log.warning("No screen results found at %s, running screen...", result_path)
    return _run_screen_and_load(base, trade_date)


def _run_screen_and_load(base: Path, trade_date: str) -> list[dict]:
    """Run the a_stock_strategy screen and load results."""
    import subprocess

    venv_python = base / ".venv" / "Scripts" / "python.exe"
    cmd = [
        str(venv_python), "-m", "src.main", "screen",
        "--date", trade_date,
        "--force-snapshot",
        "--top-n", "5",
        "--max-per-plate", "2",
    ]
    _log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(base), capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"Screen failed: {result.stderr}")

    result_path = base / "outputs" / f"screen_{trade_date}.json"
    return json.loads(result_path.read_text("utf-8"))


def candidates_to_stocks(candidates: list[dict]) -> list[dict]:
    """Extract essential stock info from candidate results."""
    stocks = []
    for c in candidates:
        q = c["quote"]
        ro = c.get("red_open", {})
        stocks.append({
            "code": q["code"],
            "name": q["name"],
            "price": q["price"],
            "pct": q["pct"],
            "turnover": q["turnover"],
            "volume_ratio": q["volume_ratio"],
            "float_market_cap": q["float_market_cap"],
            "ro_score": ro.get("probability", 0),
            "ro_label": ro.get("label", ""),
            "ro_reason": ro.get("reason", ""),
            "matched_rules": c.get("extra", {}).get("matched_rules", []),
            "final_score": c.get("extra", {}).get("final_score", 0),
        })
    # Sort by final_score descending
    stocks.sort(key=lambda s: s["final_score"], reverse=True)
    return stocks
