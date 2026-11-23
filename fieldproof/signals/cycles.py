from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

from fieldproof.common import utc_text
from fieldproof.design.contracts import Policy, local_day
from fieldproof.design.registry import Roster
from fieldproof.signals.contracts import Event
from fieldproof.signals.ledger import cycle_inputs


@dataclass(frozen=True)
class Energy:
    wh: int | None
    problem: str | None = None
    meter_id: str | None = None
    event_ids: tuple[str, ...] = ()


@dataclass
class Cycle:
    unit_id: str
    cycle_id: str
    state: str
    issues: list[str]
    day: date | None = None
    start: Event | None = None
    close: Event | None = None
    manual: bool = False
    alarm: bool = False
    elapsed_seconds: int | None = None
    energy: Energy = field(default_factory=lambda: Energy(None, "cycle_unresolved"))
    sources: dict[str, int] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.state in {"complete", "aborted"}

    @property
    def failed(self) -> bool:
        return self.state == "aborted"

    def payload(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "cycle_id": self.cycle_id,
            "state": self.state,
            "issues": self.issues,
            "local_date": self.day.isoformat() if self.day else None,
            "started_at": utc_text(self.start.occurred_at) if self.start else None,
            "closed_at": utc_text(self.close.occurred_at) if self.close else None,
            "manual": self.manual,
            "alarm": self.alarm,
            "elapsed_seconds": self.elapsed_seconds,
            "energy_wh": self.energy.wh,
            "energy_issue": self.energy.problem,
            "meter_id": self.energy.meter_id,
            "meter_events": list(self.energy.event_ids),
            "source_revisions": self.sources,
        }


def energy_used(meters: list[Event], start: datetime, close: datetime, cap: int) -> Energy:
    by_time: dict[datetime, list[Event]] = defaultdict(list)
    for meter in meters:
        if start <= meter.occurred_at <= close:
            by_time[meter.occurred_at].append(meter)
    if start not in by_time or close not in by_time:
        return Energy(None, "missing_endpoint")
    ordered = []
    for moment in sorted(by_time):
        candidates = by_time[moment]
        signatures = {(item.meter_id, item.meter_wh) for item in candidates}
        if len(signatures) != 1:
            return Energy(None, "conflicting_readings")
        ordered.append(min(candidates, key=lambda item: item.event_id))
    meters_used = {item.meter_id for item in ordered}
    if len(meters_used) != 1:
        return Energy(None, "meter_changed")
    readings = [item.meter_wh for item in ordered]
    if any(after < before for before, after in zip(readings, readings[1:])):
        return Energy(None, "counter_decreased")
    energy = readings[-1] - readings[0]
    if energy > cap:
        return Energy(None, "implausible_consumption")
    return Energy(energy, meter_id=ordered[0].meter_id,
                  event_ids=tuple(item.event_id for item in ordered))


def resolve(unit_id: str, cycle_id: str, events: list[Event], roster: Roster, policy: Policy) -> Cycle:
    groups = defaultdict(list)
    for event in events:
        groups[event.kind].append(event)
    starts = groups["start"]
    ends = groups["complete"] + groups["abort"]
    cycle = Cycle(
        unit_id,
        cycle_id,
        "invalid",
        [],
        alarm=bool(groups["alarm"]),
        sources={event.event_id: event.revision for event in events},
    )
    if len(starts) != 1:
        if not starts:
            cycle.state = "orphan"
            cycle.issues.append("missing_start")
        else:
            cycle.issues.append("duplicate_start")
        return cycle
    cycle.start = start = starts[0]
    unit = roster.units[unit_id]
    cycle.day = local_day(start.occurred_at, roster.sites[unit.site_id])
    if not ends:
        cycle.state = "pending"
        cycle.issues.append("missing_terminal")
        return cycle
    if len(ends) != 1:
        cycle.issues.append("multiple_terminals")
        return cycle
    cycle.close = close = ends[0]
    elapsed = close.occurred_at - start.occurred_at
    if elapsed <= timedelta() or elapsed > timedelta(seconds=policy.max_cycle_seconds):
        cycle.issues.append("invalid_duration")
        return cycle
    actions = groups["manual"] + groups["alarm"]
    if any(not start.occurred_at <= event.occurred_at <= close.occurred_at for event in actions):
        cycle.issues.append("action_outside_cycle")
        return cycle
    cycle.elapsed_seconds = int(elapsed.total_seconds())
    cycle.manual = bool(groups["manual"])
    cycle.state = "aborted" if close.kind == "abort" else "complete"
    if cycle.state == "aborted":
        cycle.energy = Energy(None, "cycle_aborted")
    else:
        cycle.energy = energy_used(groups["meter"], start.occurred_at, close.occurred_at,
                                   policy.max_cycle_wh)
    return cycle


def resolve_cycles(events: Iterable[Event], roster: Roster, policy: Policy) -> list[Cycle]:
    cycles = [
        resolve(unit, cycle, group, roster, policy)
        for (unit, cycle), group in cycle_inputs(events)
    ]
    by_unit: dict[str, list[Cycle]] = defaultdict(list)
    for cycle in cycles:
        if cycle.usable:
            by_unit[cycle.unit_id].append(cycle)
    for observed in by_unit.values():
        ordered = sorted(observed, key=lambda cycle: (cycle.start.occurred_at, cycle.cycle_id))
        if not ordered:
            continue
        last_ending = ordered[0]
        for candidate in ordered[1:]:
            if candidate.start.occurred_at < last_ending.close.occurred_at:
                for item in (last_ending, candidate):
                    if item.state != "invalid":
                        item.state = "invalid"
                        item.issues.append("overlapping_cycle")
                        item.energy = Energy(None, "cycle_unresolved")
            if candidate.close.occurred_at > last_ending.close.occurred_at:
                last_ending = candidate
    return cycles
