from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from sqlite3 import Connection

from fieldproof.analysis.cohorts import AnalysisWindow, UnitSummary, summarize
from fieldproof.analysis.contrast import estimate
from fieldproof.common import encoded, fingerprint, invalid, missing
from fieldproof.design.registry import Trial, require_phase, roster
from fieldproof.release.gates import assess
from fieldproof.signals.cycles import Cycle, resolve_cycles
from fieldproof.signals.ledger import current_cut, events_at, require_cut

EVIDENCE_FORMAT = 1


@dataclass(frozen=True)
class Material:
    window: AnalysisWindow
    cut: int
    cycles: list[Cycle]
    units: dict[str, UnitSummary]


def select_window(trial: Trial, start: date | None, end: date | None) -> AnalysisWindow:
    starts = trial.plan.starts_on if start is None else start
    ends = trial.plan.ends_before if end is None else end
    if not trial.plan.starts_on <= starts < ends <= trial.plan.ends_before:
        raise invalid("analysis_window", "Select a non-empty interval inside the trial dates")
    return AnalysisWindow(starts, ends)


def load(
    db: Connection,
    trial: Trial,
    window: AnalysisWindow,
    cut: int | None,
) -> Material:
    if trial.phase == "draft":
        require_phase(trial, "open")
    selected = current_cut(db, trial.trial_id) if cut is None else cut
    require_cut(db, trial.trial_id, selected)
    assigned = roster(db, trial.trial_id)
    cycles = resolve_cycles(
        events_at(db, trial.trial_id, selected),
        assigned,
        trial.plan.policy,
    )
    return Material(window, selected, cycles, summarize(trial, assigned, cycles, window))


def analyze(trial: Trial, material: Material) -> dict:
    policy = trial.plan.policy
    markets = []
    for market in estimate(trial, material.units):
        markets.append({**market.payload(), **assess(market, policy)})
    relevant = [
        cycle.payload()
        for cycle in material.cycles
        if cycle.unit_id in material.units
        and (cycle.day is None or material.window.contains(cycle.day))
    ]
    units = [row.payload(policy) for row in material.units.values()]
    return {
        "format": EVIDENCE_FORMAT,
        "trial_id": trial.trial_id,
        "program": trial.plan.program,
        "cut": material.cut,
        "window": material.window.payload(),
        "policy": policy.model_dump(),
        "roster_digest": fingerprint([
            (row.unit_id, row.site_id, row.market, row.cell, row.arm, row.enrolled_days)
            for row in material.units.values()
        ]),
        "facts_digest": fingerprint(relevant),
        "unit_digest": fingerprint(units),
        "market_results": markets,
    }


def build(
    db: Connection,
    trial: Trial,
    window: AnalysisWindow,
    cut: int | None,
) -> dict:
    material = load(db, trial, window, cut)
    document = analyze(trial, material)
    return {"evidence_id": fingerprint(document), **document}


def freeze(db: Connection, trial: Trial, expected_cut: int) -> dict:
    require_phase(trial, "closed")
    if trial.close_cut != expected_cut:
        from fieldproof.common import conflict

        raise conflict(
            "cut_changed",
            "The submitted cut does not match the closed trial",
            details={"close_cut": trial.close_cut},
        )
    document = build(db, trial, select_window(trial, None, None), expected_cut)
    db.execute(
        """INSERT INTO evidence VALUES (?,?,?,?)
           ON CONFLICT(trial_id,evidence_id) DO NOTHING""",
        (trial.trial_id, document["evidence_id"], expected_cut, encoded(document)),
    )
    return document


def get(db: Connection, trial_id: str, evidence_id: str) -> dict:
    import json

    row = db.execute(
        "SELECT document FROM evidence WHERE trial_id=? AND evidence_id=?",
        (trial_id, evidence_id),
    ).fetchone()
    if row is None:
        raise missing("evidence", evidence_id)
    return json.loads(row["document"])


def get_market(document: dict, market_id: str) -> dict:
    for item in document["market_results"]:
        if item["market_id"] == market_id:
            return item
    raise missing("market", market_id)


def unit_record(material: Material, trial: Trial, unit_id: str) -> dict:
    item = material.units.get(unit_id)
    if item is None:
        raise missing("unit_in_window", unit_id)
    cycles = [
        cycle.payload()
        for cycle in material.cycles
        if cycle.unit_id == unit_id
        and (cycle.day is None or material.window.contains(cycle.day))
    ]
    return {
        "cut": material.cut,
        "window": material.window.payload(),
        "unit": item.payload(trial.plan.policy),
        "cycles": cycles,
    }
