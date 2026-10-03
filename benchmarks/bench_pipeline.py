"""Measure end-to-end pipeline time and peak memory on the sample repositories.

Run:  python benchmarks/bench_pipeline.py
Each repository runs in this process; peak RSS is read from the OS afterwards. Numbers are indicative
(single machine) and are what the README's performance section quotes (DESIGN.md section 22).
"""

from __future__ import annotations

import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from research_normalizer.config import PipelineConfig  # noqa: E402
from research_normalizer.events import ListSink  # noqa: E402
from research_normalizer.pipeline import run_pipeline  # noqa: E402

REPOS = [ROOT / "samples" / "dairy_cattle_energy", ROOT / "samples" / "pig_decomposition_hawaii"]


def peak_rss_mb() -> float:
    # ru_maxrss is bytes on macOS, kilobytes on Linux.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def main() -> None:
    config = PipelineConfig(deterministic=True)
    print(f"{'repository':30s} {'time_ms':>8s} {'datasets':>9s} {'variables':>10s}")
    for repo in REPOS:
        best = None
        for _ in range(3):
            t = time.perf_counter()
            doc = run_pipeline(repo, config, ListSink())
            dt = (time.perf_counter() - t) * 1000
            best = dt if best is None else min(best, dt)
        print(f"{repo.name:30s} {best:8.1f} {doc.summary.datasets:9d} {doc.summary.variables:10d}")
    print(f"\npeak RSS this process: {peak_rss_mb():.1f} MB")


if __name__ == "__main__":
    main()
