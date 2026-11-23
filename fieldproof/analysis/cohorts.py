from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from typing import Iterable

from fieldproof.design.contracts import Enrollment, Policy, enrolled_days
from fieldproof.design.registry import Roster, Trial
from fieldproof.signals.cycles import Cycle

ARMS = ("standard", "assisted")


@dataclass
class UnitSummary:
    unit_id: str
    site_id: str
    market: str
    cell: str
    arm: str
    enrolled_days: int
    cycles: Counter[str] = field(default_factory=Counter)
    energy_issues: Counter[str] = field(default_factory=Counter)
    integrity_issues: Counter[str] = field(default_factory=Counter)
    usable_cycles: int = 0
    completed: int = 0
    manual: int = 0
    alarms: int = 0
    unsafe_unresolved: int = 0
    failed: int = 0
    energy_cycles: int = 0
    total_wh: int = 0
    elapsed_seconds: int = 0

    def record(self, cycle: Cycle):
        self.cycles[cycle.state] += 1
        if not cycle.usable:
            self.integrity_issues.update(cycle.issues)
            self.unsafe_unresolved += int(cycle.alarm)
            return
        self.usable_cycles += 1
        self.completed += int(not cycle.failed)
        self.manual += int(cycle.manual)
        self.alarms += int(cycle.alarm)
        self.failed += int(cycle.failed)
        self.elapsed_seconds += cycle.elapsed_seconds
        if cycle.energy.wh is not None:
            self.energy_cycles += 1
            self.total_wh += cycle.energy.wh
        elif not cycle.failed:
            self.energy_issues[cycle.energy.problem] += 1

    def eligible(self, policy: Policy) -> bool:
        return self.usable_cycles >= policy.required_cycles_per_unit

    def rate(self, numerator: int, denominator: int) -> Fraction | None:
        return Fraction(numerator, denominator) if denominator else None

    def metric(self, name: str) -> Fraction | None:
        if name == "energy_wh":
            return self.rate(self.total_wh, self.energy_cycles)
        if name == "duration_seconds":
            return self.rate(self.elapsed_seconds, self.usable_cycles)
        numerator = {
            "manual": self.manual,
            "alarm": self.alarms,
            "failure": self.failed,
        }[name]
        return self.rate(numerator, self.usable_cycles)

    def payload(self, policy: Policy) -> dict:
        return {
            "unit_id": self.unit_id,
            "site_id": self.site_id,
            "market": self.market,
            "cell": self.cell,
            "arm": self.arm,
            "enrolled_days": self.enrolled_days,
            "cycle_states": dict(sorted(self.cycles.items())),
            "integrity_issues": dict(sorted(self.integrity_issues.items())),
            "energy_issues": dict(sorted(self.energy_issues.items())),
            "eligible": self.eligible(policy),
            "usable_cycles": self.usable_cycles,
            "completed": self.completed,
            "manual_cycles": self.manual,
            "alarm_cycles": self.alarms,
            "unresolved_with_alarm": self.unsafe_unresolved,
            "failed_cycles": self.failed,
            "energy_cycles": self.energy_cycles,
            "total_wh": self.total_wh,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass(frozen=True)
class AnalysisWindow:
    start: date
    end: date

    def contains(self, day: date) -> bool:
        return self.start <= day < self.end

    def payload(self) -> dict:
        return {"starts_on": self.start.isoformat(), "ends_before": self.end.isoformat()}


def summarize(
    trial: Trial, roster: Roster, cycles: Iterable[Cycle], window: AnalysisWindow
) -> dict[str, UnitSummary]:
    summaries = {}
    for unit in roster.units.values():
        days = enrolled_days(unit, window.start, window.end)
        if not days:
            continue
        market, cell = roster.placement(unit)
        summaries[unit.unit_id] = UnitSummary(
            unit.unit_id, unit.site_id, market, cell, unit.arm, days
        )
    for cycle in cycles:
        row = summaries.get(cycle.unit_id)
        if row is not None and (cycle.day is None or window.contains(cycle.day)):
            row.record(cycle)
    return summaries


@dataclass
class ArmSummary:
    recruited: list[UnitSummary]
    eligible: list[UnitSummary]

    @classmethod
    def assess(cls, units: list[UnitSummary], policy: Policy):
        return cls(units, [unit for unit in units if unit.eligible(policy)])

    @property
    def enrollment_coverage(self) -> Fraction:
        return Fraction(len(self.eligible), len(self.recruited)) if self.recruited else Fraction(0)

    @property
    def energy_coverage(self) -> Fraction | None:
        complete = sum(unit.completed for unit in self.eligible)
        measured = sum(unit.energy_cycles for unit in self.eligible)
        return Fraction(measured, complete) if complete else None

    @property
    def measured_units(self) -> int:
        return sum(unit.energy_cycles > 0 for unit in self.eligible)

    def mean(self, name: str) -> Fraction | None:
        values = [value for unit in self.eligible if (value := unit.metric(name)) is not None]
        return sum(values, Fraction(0)) / len(values) if values else None

    def payload(self) -> dict:
        return {
            "recruited_units": len(self.recruited),
            "eligible_units": len(self.eligible),
            "energy_units": self.measured_units,
            "eligible_completed_cycles": sum(unit.completed for unit in self.eligible),
            "eligible_measured_cycles": sum(unit.energy_cycles for unit in self.eligible),
            "eligible_observed_cycles": sum(unit.usable_cycles for unit in self.eligible),
            "unresolved_units": sum(bool(unit.integrity_issues)
                                     for unit in self.recruited),
            "unresolved_alarm_cycles": sum(unit.unsafe_unresolved for unit in self.recruited),
        }


def by_placement(units: Iterable[UnitSummary]) -> dict[tuple[str, str, str], list[UnitSummary]]:
    cells = defaultdict(list)
    for unit in units:
        cells[unit.market, unit.cell, unit.arm].append(unit)
    return cells
