from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from fieldproof.analysis.cohorts import ARMS, ArmSummary
from fieldproof.analysis.contrast import MarketEstimate, display
from fieldproof.design.contracts import Policy

RATES = ("manual", "alarm", "failure")
RATE_SCALE = 10_000


@dataclass(frozen=True)
class Interval:
    low: Fraction
    high: Fraction

    def weighted(self, weight: Fraction):
        return Interval(self.low * weight, self.high * weight)

    def add(self, other: Interval):
        return Interval(self.low + other.low, self.high + other.high)

    def subtract(self, other: Interval):
        return Interval(self.low - other.high, self.high - other.low)

    def payload(self):
        return {
            "low_bp": display(self.low, RATE_SCALE),
            "high_bp": display(self.high, RATE_SCALE),
        }


def population_interval(arm: ArmSummary, name: str) -> Interval:
    """保持合格设备的观测率，计算全体入组设备均值的界限。"""
    total = len(arm.recruited)
    included = len(arm.eligible)
    if total == 0:
        return Interval(Fraction(0), Fraction(1))
    measured = sum((unit.metric(name) for unit in arm.eligible), Fraction(0))
    return Interval(measured / total, (measured + total - included) / total)


def range_verdict(bound: Interval, limit_bp: int, direction: str) -> str:
    boundary = Fraction(limit_bp, RATE_SCALE)
    if direction == "at_least":
        if bound.low >= boundary:
            return "robust"
        if bound.high < boundary:
            return "cannot_pass"
    else:
        if bound.high <= boundary:
            return "robust"
        if bound.low > boundary:
            return "cannot_pass"
    return "depends_on_unqualified_units"


def range_readout(estimate: MarketEstimate, policy: Policy) -> dict:
    scores = {
        "manual": (policy.min_manual_saving_bp, "at_least"),
        "alarm": (policy.max_alarm_increase_bp, "at_most"),
        "failure": (policy.max_failure_increase_bp, "at_most"),
    }
    market_ranges = {
        name: {arm: Interval(Fraction(0), Fraction(0)) for arm in ARMS}
        for name in RATES
    }
    missing_by_cell = []
    for cell in estimate.cells:
        unknown = {}
        weight = Fraction(cell.weight_bp, RATE_SCALE)
        for arm, summary in cell.arms.items():
            missing = len(summary.recruited) - len(summary.eligible)
            unknown[arm] = {
                "unqualified_units": missing,
                "has_no_enrollment": not summary.recruited,
                "market_weight_exposed_bp": display(
                    weight * (Fraction(missing, len(summary.recruited))
                              if summary.recruited else 1),
                    RATE_SCALE,
                ),
            }
            for name in RATES:
                contribution = population_interval(summary, name).weighted(weight)
                market_ranges[name][arm] = market_ranges[name][arm].add(contribution)
        missing_by_cell.append({"cell": cell.key, "arms": unknown})
    metrics = {}
    for name, arms in market_ranges.items():
        left, right = (
            ("standard", "assisted") if name == "manual" else ("assisted", "standard")
        )
        delta = arms[left].subtract(arms[right])
        limit, direction = scores[name]
        metrics[name] = {
            **{arm: interval.payload() for arm, interval in arms.items()},
            "effect": {
                "definition": f"{left}_minus_{right}",
                **delta.payload(),
                "boundary_bp": limit,
                "direction": direction,
                "verdict": range_verdict(delta, limit, direction),
            },
        }
    return {
        "market_id": estimate.market.market_id,
        "interpretation": "Unqualified devices can have any rate between zero and one",
        "unknown_by_cell": missing_by_cell,
        "metrics": metrics,
    }
