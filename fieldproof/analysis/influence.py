from __future__ import annotations

from dataclasses import dataclass

from fieldproof.analysis.cohorts import UnitSummary, by_placement
from fieldproof.analysis.contrast import METRICS, MarketEstimate, assess_cells, display
from fieldproof.analysis.evidence import Material
from fieldproof.common import missing
from fieldproof.design.contracts import Market, Policy
from fieldproof.design.registry import Trial
from fieldproof.release.gates import assess


@dataclass(frozen=True)
class Removal:
    site_id: str
    units: list[UnitSummary]
    remaining: list[UnitSummary]


def removals(units: list[UnitSummary]):
    for site in sorted({unit.site_id for unit in units}):
        site_units = []
        remaining = []
        for unit in units:
            if unit.site_id == site:
                site_units.append(unit)
            else:
                remaining.append(unit)
        yield Removal(site, site_units, remaining)


def estimate_remaining(market: Market, policy: Policy, removal: Removal) -> MarketEstimate:
    cells = assess_cells(market, by_placement(removal.remaining), policy)
    return MarketEstimate.build(market, cells)


def describe_effect(full: MarketEstimate, removed: MarketEstimate, metric: str) -> dict:
    baseline = full.delta(metric)
    other = removed.delta(metric)
    scale = 10_000 if metric in {"manual", "alarm", "failure"} else 1
    return {
        "full_effect": display(baseline, scale),
        "remaining_effect": display(other, scale),
        "shift": display(other - baseline, scale)
        if baseline is not None and other is not None else None,
        "unit": {"energy_wh": "Wh", "duration_seconds": "seconds"}.get(metric, "basis_points"),
    }


def compare_gates(full: dict, remaining: dict) -> list[dict]:
    before = {item["gate"]: item["state"] for item in full["gates"]}
    after = {item["gate"]: item["state"] for item in remaining["gates"]}
    return [
        {"gate": key, "before": before.get(key), "after": after.get(key)}
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    ]


def site_readout(trial: Trial, material: Material, market_id: str) -> dict:
    market = trial.plan.market(market_id)
    if market is None:
        raise missing("market", market_id)
    units = [unit for unit in material.units.values() if unit.market == market_id]
    policy = trial.plan.policy
    full = MarketEstimate.build(market, assess_cells(market, by_placement(units), policy))
    full_gates = assess(full, policy)
    rows = [
        omission_row(market, policy, full, full_gates, removal)
        for removal in removals(units)
    ]
    return {
        "market_id": market_id,
        "cut": material.cut,
        "window": material.window.payload(),
        "full_recommendation": full_gates["recommendation"],
        "sites_with_recommendation_change": [
            row["site_id"] for row in rows
            if row["remaining_recommendation"] != full_gates["recommendation"]
        ],
        "site_omissions": rows,
    }


def omission_row(market, policy, full, full_gates, removal: Removal):
    remaining = estimate_remaining(market, policy, removal)
    remaining_gates = assess(remaining, policy)
    cells = []
    for cell in remaining.cells:
        gaps = cell.gaps
        affected = sum(unit.cell == cell.key for unit in removal.units)
        if affected or gaps:
            cells.append({
                "cell": cell.key,
                "removed_units": affected,
                "remaining_gaps": gaps,
            })
    return {
        "site_id": removal.site_id,
        "removed_units": len(removal.units),
        "remaining_units": len(removal.remaining),
        "remaining_recommendation": remaining_gates["recommendation"],
        "cells": cells,
        "metrics": {
            metric: describe_effect(full, remaining, metric)
            for metric in METRICS
        },
        "gate_changes": compare_gates(full_gates, remaining_gates),
    }
