from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from fieldproof.analysis.contrast import MarketEstimate, display
from fieldproof.design.contracts import Policy


@dataclass(frozen=True)
class Gate:
    name: str
    state: str
    observed: str | None = None
    boundary: int | None = None
    direction: str | None = None

    def payload(self) -> dict:
        return {
            "gate": self.name,
            "state": self.state,
            "observed": self.observed,
            "boundary": self.boundary,
            "direction": self.direction,
        }


def threshold(name: str, observed: Fraction | None, boundary: int, direction: str) -> Gate:
    if observed is None:
        return Gate(name, "unavailable", boundary=boundary, direction=direction)
    passed = observed <= boundary if direction == "at_most" else observed >= boundary
    return Gate(name, "pass" if passed else "fail", display(observed), boundary, direction)


def in_basis_points(number: Fraction | None) -> Fraction | None:
    return number * 10_000 if number is not None else None


def energy_gate(estimate: MarketEstimate, policy: Policy) -> Gate:
    standard = estimate.standard["energy_wh"]
    assisted = estimate.assisted["energy_wh"]
    allowed = policy.max_energy_increase_bp
    if standard is None or assisted is None:
        return Gate("energy_change_bp", "unavailable", boundary=allowed, direction="at_most")
    if standard == 0:
        return Gate(
            "zero_baseline_energy",
            "pass" if assisted == 0 else "fail",
            display(assisted),
            0,
            "at_most",
        )
    return threshold(
        "energy_change_bp", (assisted - standard) / standard * 10_000, allowed, "at_most"
    )


def assess(estimate: MarketEstimate, policy: Policy) -> dict:
    integrity = estimate.integrity()
    manual = estimate.delta("manual")
    unresolved = integrity["unresolved_units"]
    coverage = not any(cell.gaps for cell in estimate.cells)
    gates = [
        Gate("planned_cell_coverage", "pass" if coverage else "unavailable"),
        threshold("units_with_unresolved_cycles", unresolved, 0, "at_most"),
        threshold("alarm_increase_bp", in_basis_points(estimate.delta("alarm")),
                  policy.max_alarm_increase_bp, "at_most"),
        threshold("failure_increase_bp", in_basis_points(estimate.delta("failure")),
                  policy.max_failure_increase_bp, "at_most"),
        threshold("manual_saving_bp", in_basis_points(-manual if manual is not None else None),
                  policy.min_manual_saving_bp, "at_least"),
        energy_gate(estimate, policy),
    ]
    if any(gate.state == "fail" and gate.name != "units_with_unresolved_cycles" for gate in gates):
        recommendation = "hold"
    elif any(gate.state != "pass" for gate in gates):
        recommendation = "insufficient"
    else:
        recommendation = "advance"
    return {
        "recommendation": recommendation,
        "gates": [gate.payload() for gate in gates],
        "blocked_by": [gate.name for gate in gates if gate.state != "pass"],
    }
