"""Bias-corrected failure prevalence for a monitoring period."""

from __future__ import annotations

from typing import Any, Sequence


def corrected_mode_prevalence(
    sample_preds: Sequence[int],
    test_labels: Sequence[int],
    test_preds: Sequence[int],
    confidence: float = 0.95,
    bootstrap_iterations: int = 20000,
    seed: int | None = 7,
) -> dict[str, Any]:
    """Bias-corrected live prevalence for one mode from sampled verdicts.

    The contract, precisely:

      1. ``raw`` is the uncorrected flag rate: ``mean(sample_preds)``.
      2. Compute the frozen judge's failure sensitivity and pass specificity
         from ``test_labels`` and ``test_preds``. Both use the monitoring
         convention that 1 means a failure is present. Failure sensitivity is
         the flagged fraction of human-labeled failures. Pass specificity is
         the unflagged fraction of human-labeled passes.
      3. Compute the Rogan-Gladen point estimate, then resample the held-out
         records and sampled predictions to obtain a percentile-bootstrap
         interval. Use a seeded NumPy generator so the committed result is
         reproducible.
      4. Resample the monitoring predictions and the paired held-out records
         independently with replacement. Keep their original sample sizes.
         Discard a draw if the correction cannot be computed. Clamp each
         retained estimate to [0, 1], then take the percentile interval.
         Raise ``ValueError`` if no replicate is valid.

    Args:
        sample_preds: the judge's 0/1 verdicts over the UNIFORM BASE sample
            only (never the risk strata; they are biased toward failure by
            design).
        test_labels: human labels for the frozen Homework 5 judge's test
            split.
        test_preds: the frozen judge's predictions on that test split.
        confidence: interval confidence level.
        bootstrap_iterations: number of percentile-bootstrap replicates.
        seed: numpy seed for a reproducible interval; None leaves the RNG
            untouched.

    Returns:
        {"raw", "corrected", "ci_low", "ci_high", "confidence",
         "failure_sensitivity", "pass_specificity", "n_sample"}
        with "corrected" clamped to [0, 1] and rates rounded to 4 places.

    Raises:
        ValueError: if an input is empty, the held-out inputs have different
            lengths, a value is not 0 or 1, a class is absent, the judge is
            missing a usable correction, or no bootstrap replicate is valid.
    """
    import numpy as np

    if not sample_preds:
        raise ValueError("sample_preds is empty")
    if not test_labels or not test_preds:
        raise ValueError("the held-out labels and predictions must be nonempty")
    if len(test_labels) != len(test_preds):
        raise ValueError(
            f"held-out lengths differ: {len(test_labels)} labels, {len(test_preds)} predictions"
        )
    for name, values in (
        ("sample_preds", sample_preds),
        ("test_labels", test_labels),
        ("test_preds", test_preds),
    ):
        if any(v not in (0, 1) for v in values):
            raise ValueError(f"{name} must hold only 0 or 1")

    labels = np.asarray(test_labels, dtype=int)
    preds = np.asarray(test_preds, dtype=int)
    sample = np.asarray(sample_preds, dtype=int)
    if labels.sum() == 0 or (1 - labels).sum() == 0:
        raise ValueError("the held-out labels need both a failure and a pass class")

    def rates(lab: "np.ndarray", pre: "np.ndarray") -> tuple[float, float] | None:
        """Failure sensitivity and pass specificity, failure-positive."""
        failures, passes = lab == 1, lab == 0
        if not failures.any() or not passes.any():
            return None
        return float(pre[failures].mean()), float(1 - pre[passes].mean())

    base = rates(labels, preds)
    if base is None:  # pragma: no cover - guarded by the class check above
        raise ValueError("the held-out labels need both classes")
    sensitivity, specificity = base

    def corrected(raw_rate: float, sens: float, spec: float) -> float | None:
        """Rogan-Gladen: strip the judge's own error out of the flag rate.

        The denominator is the judge's signal (Youden's J). At zero the judge
        is uninformative and the correction is undefined, which is a refusal
        rather than a number.
        """
        denominator = sens + spec - 1
        if abs(denominator) < 1e-12:
            return None
        return (raw_rate + spec - 1) / denominator

    raw = float(sample.mean())
    point = corrected(raw, sensitivity, specificity)
    if point is None:
        raise ValueError(
            "the frozen judge has no usable correction: sensitivity + specificity == 1"
        )
    point = min(max(point, 0.0), 1.0)

    # Two independent sources of uncertainty, resampled together: how many
    # failures this sample happened to contain, and how well the judge's
    # measured rates pin down its true accuracy. Ignoring the second would
    # report an interval that is too narrow to be honest.
    rng = np.random.default_rng(seed)
    n_sample, n_test = sample.size, labels.size
    replicates: list[float] = []
    for _ in range(bootstrap_iterations):
        draw = sample[rng.integers(0, n_sample, n_sample)]
        idx = rng.integers(0, n_test, n_test)
        drawn = rates(labels[idx], preds[idx])
        if drawn is None:
            continue
        value = corrected(float(draw.mean()), *drawn)
        if value is None:
            continue
        replicates.append(min(max(value, 0.0), 1.0))
    if not replicates:
        raise ValueError("no bootstrap replicate produced a usable correction")

    tail = (1 - confidence) / 2
    low, high = np.percentile(replicates, [100 * tail, 100 * (1 - tail)])
    return {
        "raw": round(raw, 4),
        "corrected": round(point, 4),
        "ci_low": round(float(low), 4),
        "ci_high": round(float(high), 4),
        "confidence": confidence,
        "failure_sensitivity": round(sensitivity, 4),
        "pass_specificity": round(specificity, 4),
        "n_sample": int(n_sample),
    }
