"""Scoring rules, as pure functions.

A field is scored like an information-extraction task in which "not found" is an answer:

- TP: the reference has a value and the prediction matches it.
- FP: the prediction has a value that is wrong, or the reference says there is none.
- FN: the reference has a value and the prediction is missing or wrong.

So a wrong number counts against both precision and recall, and a null is never rewarded
unless the reference is null too. Exact match is the share of documents where prediction and
reference are identical, including both being null.

Numbers match within a relative tolerance (default 0.5%): the model copies "391,035" in
millions while XBRL stores 391,035,000,000, and some tables round to a tenth of a billion.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from dataclasses import asdict, dataclass

NUMERIC_TOLERANCE = 0.005


@dataclass(frozen=True)
class FieldScore:
    n: int
    tp: int
    fp: int
    fn: int
    exact: int

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        denominator = 2 * self.tp + self.fp + self.fn  # same as 2PR / (P + R)
        return 2 * self.tp / denominator if denominator else None

    @property
    def exact_match(self) -> float | None:
        return self.exact / self.n if self.n else None

    def as_row(self) -> dict:
        return {**asdict(self), "precision": self.precision, "recall": self.recall,
                "f1": self.f1, "exact_match": self.exact_match}  # fmt: skip


def numbers_match(pred: float | None, gold: float | None, tol: float = NUMERIC_TOLERANCE) -> bool:
    if pred is None or gold is None:
        return pred is None and gold is None
    if gold == 0:
        return pred == 0
    return abs(pred - gold) <= tol * abs(gold)


def score_numeric(
    pairs: Iterable[tuple[float | None, float | None]], tol: float = NUMERIC_TOLERANCE
) -> FieldScore:
    """pairs of (prediction, reference)."""
    n = tp = fp = fn = exact = 0
    for pred, gold in pairs:
        n += 1
        exact += pred == gold
        close = numbers_match(pred, gold, tol)
        if gold is not None and pred is not None and close:
            tp += 1
            continue
        if pred is not None:
            fp += 1
        if gold is not None:
            fn += 1
    return FieldScore(n, tp, fp, fn, exact)


def score_labels(pairs: Iterable[tuple[Hashable, Hashable]]) -> dict[Hashable, FieldScore]:
    """Per-label scores for a classification (prediction, reference): one-vs-rest, so a
    wrong label is an FP for the predicted label and an FN for the true one."""
    pairs = list(pairs)
    labels = {g for _, g in pairs} | {p for p, _ in pairs}
    out = {}
    for label in sorted(labels, key=str):
        tp = sum(p == label and g == label for p, g in pairs)
        fp = sum(p == label and g != label for p, g in pairs)
        fn = sum(p != label and g == label for p, g in pairs)
        exact = sum(p == g for p, g in pairs if g == label)
        out[label] = FieldScore(sum(g == label for _, g in pairs), tp, fp, fn, exact)
    return out


def macro_f1(scores: dict[Hashable, FieldScore]) -> float | None:
    f1s = [s.f1 for s in scores.values() if s.n and s.f1 is not None]
    return sum(f1s) / len(f1s) if f1s else None
