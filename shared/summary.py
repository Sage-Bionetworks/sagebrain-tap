"""Rounding and distribution summaries shared by the describe_data modules.

Both ingests write a characteristics JSON that gets committed, so the shaping
rules live here rather than in each module: a summary is only useful as a diff
of one release against the next, and that needs the same rounding, the same
quantiles and the same key names on both sides."""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]


def repo_relative(path: Path | str) -> str:
    """A path as the repository sees it, when it is inside the repository.

    The pipelines resolve every path to an absolute one before handing it to a
    subprocess, so without this a committed manifest would record whose
    checkout generated it and diff against itself on the next machine.
    """
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        return str(resolved)


def num(value: float, digits: int = 6) -> float:
    """Round for stable diffs.

    Float noise in the last places of a mean would otherwise make every
    regeneration look like a change.
    """
    return round(float(value), digits)


def quantile(ordered: Sequence[float], fraction: float) -> float:
    """Nearest-rank quantile over an already-sorted sequence.

    Nearest-rank rather than an interpolating definition: these summarise gene
    counts and set sizes, so a reported quantile should be a value that some
    pathway actually has.
    """
    if not ordered:
        raise ValueError("quantile of an empty sequence")
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def distribution(values: Iterable[float], *, digits: int = 2) -> dict:
    """n, mean and the quartile spine of a list that fits in memory.

    Quantiles, not just mean and sd: every distribution these modules describe
    -- genes per pathway, pathways per gene, edges per sample -- is heavily
    right-skewed, and a mean on its own reads as a typical value when it is not.
    """
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "mean": None, "min": None, "p25": None,
                "median": None, "p75": None, "p95": None, "max": None}

    # Counts stay counts: a median of 3 genes reads as a measurement, 3.0 reads
    # as a rounding artifact. Only the mean is meaningfully fractional.
    integral = all(float(value).is_integer() for value in ordered)
    def order_statistic(value: float) -> float | int:
        return int(value) if integral else num(value, digits)

    return {
        "n": len(ordered),
        "mean": num(statistics.fmean(ordered), digits),
        "min": order_statistic(ordered[0]),
        "p25": order_statistic(quantile(ordered, 0.25)),
        "median": order_statistic(quantile(ordered, 0.50)),
        "p75": order_statistic(quantile(ordered, 0.75)),
        "p95": order_statistic(quantile(ordered, 0.95)),
        "max": order_statistic(ordered[-1]),
    }
