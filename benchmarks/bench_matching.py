"""Compare RapidFuzz against Python's difflib for variable-name matching.

Run:  python benchmarks/bench_matching.py
Shows why we chose RapidFuzz: same top-1 accuracy, far faster (DESIGN.md section 7, Ref [5]).
"""

from __future__ import annotations

import difflib
import random
import time

from rapidfuzz import fuzz, process

WORDS = ["temp", "depth", "site", "income", "annual", "household", "species", "count",
         "weight", "date", "ph", "salinity", "age", "sex", "region", "score", "total",
         "mean", "max", "min"]


def build_names(n: int) -> tuple[list[str], list[str]]:
    rnd = random.Random(7)
    readme = sorted({"_".join(rnd.sample(WORDS, 3)) for _ in range(n * 2)})[:n]
    columns = [v.replace("_", " ").title() if i % 3 else v.upper().replace("_", "-")
               for i, v in enumerate(readme)]
    return columns, readme


def norm(s: str) -> str:
    return s.lower().replace("-", " ").replace("_", " ")


def main() -> None:
    columns, readme = build_names(500)

    # extractOne per column (no numpy dependency); choices pre-normalised for a fair comparison.
    choices = {i: norm(v) for i, v in enumerate(readme)}
    t = time.perf_counter()
    rf_correct = 0
    for i, col in enumerate(columns):
        best = process.extractOne(norm(col), choices, scorer=fuzz.ratio)
        if best is not None and best[2] == i:
            rf_correct += 1
    rf_ms = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    dl_correct = 0
    for i, col in enumerate(columns):
        scores = [difflib.SequenceMatcher(None, norm(col), norm(v)).ratio() for v in readme]
        dl_correct += int(max(range(len(scores)), key=scores.__getitem__) == i)
    dl_ms = (time.perf_counter() - t) * 1000

    print(f"{'method':10s} {'time_ms':>9s} {'top1':>8s}")
    print(f"{'rapidfuzz':10s} {rf_ms:9.1f} {rf_correct}/{len(columns)}")
    print(f"{'difflib':10s} {dl_ms:9.1f} {dl_correct}/{len(columns)}")
    print(f"\nspeedup: {dl_ms / rf_ms:.0f}x")


if __name__ == "__main__":
    main()
