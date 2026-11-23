from __future__ import annotations

from collections import Counter
import csv
import io
from sqlite3 import Connection

from fieldproof.analysis.contrast import METRICS
from fieldproof.analysis.evidence import Material, analyze, load
from fieldproof.analysis.cohorts import AnalysisWindow
from fieldproof.common import changed_fields, invalid
from fieldproof.design.registry import Trial


def cycle_index(material: Material) -> dict[tuple[str, str], dict]:
    return {
        (cycle.unit_id, cycle.cycle_id): cycle.payload()
        for cycle in material.cycles
        if cycle.unit_id in material.units
        and (cycle.day is None or material.window.contains(cycle.day))
    }


def changed_cycles(before: Material, after: Material) -> list[dict]:
    old = cycle_index(before)
    new = cycle_index(after)
    changes = []
    for key in sorted(old.keys() | new.keys()):
        previous = old.get(key)
        current = new.get(key)
        if previous == current:
            continue
        unit_id, cycle_id = key
        changes.append(
            {
                "unit_id": unit_id,
                "cycle_id": cycle_id,
                "before_state": previous["state"] if previous else None,
                "after_state": current["state"] if current else None,
                "changed_fields": changed_fields(previous or {}, current or {}),
                "before_revisions": previous["source_revisions"] if previous else {},
                "after_revisions": current["source_revisions"] if current else {},
            }
        )
    return changes


def market_changes(before: dict, after: dict) -> list[dict]:
    results = []
    for old, new in zip(before["market_results"], after["market_results"], strict=True):
        metrics = {
            metric: {"before": old["metrics"][metric], "after": new["metrics"][metric]}
            for metric in METRICS
            if old["metrics"][metric] != new["metrics"][metric]
        }
        old_gates = {gate["gate"]: gate for gate in old["gates"]}
        new_gates = {gate["gate"]: gate for gate in new["gates"]}
        gates = {
            name: {"before": old_gates.get(name), "after": new_gates.get(name)}
            for name in changed_fields(old_gates, new_gates)
        }
        cells = [
            cell["cell"]
            for previous, cell in zip(old["cells"], new["cells"], strict=True)
            if previous != cell
        ]
        if metrics or gates or cells or old["integrity"] != new["integrity"]:
            results.append(
                {
                    "market_id": old["market_id"],
                    "before_recommendation": old["recommendation"],
                    "after_recommendation": new["recommendation"],
                    "metrics": metrics,
                    "gates": gates,
                    "changed_cells": cells,
                }
            )
    return results


def reconcile(
    db: Connection,
    trial: Trial,
    window: AnalysisWindow,
    before_cut: int,
    after_cut: int,
    *,
    limit: int,
    offset: int,
) -> dict:
    if before_cut > after_cut:
        raise invalid("cut_order", "The earlier cut must be no later than the later cut")
    before = load(db, trial, window, before_cut)
    after = load(db, trial, window, after_cut)
    changes = changed_cycles(before, after)
    cells = Counter((after.units[item["unit_id"]].market,
                     after.units[item["unit_id"]].cell) for item in changes)
    return {
        "window": window.payload(),
        "before_cut": before_cut,
        "after_cut": after_cut,
        "markets": market_changes(analyze(trial, before), analyze(trial, after)),
        "affected_cells": [
            {"market_id": market, "cell": cell, "changed_cycles": count}
            for (market, cell), count in sorted(cells.items())
        ],
        "total_changed_cycles": len(changes),
        "cycles": changes[offset:offset + limit],
        "next_offset": offset + limit if offset + limit < len(changes) else None,
    }


def csv_metrics(evidence: dict) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow((
        "evidence_id", "cut", "market_id", "cell", "metric", "unit", "standard",
        "assisted", "assisted_minus_standard", "relative_change_bp",
    ))
    for market in evidence["market_results"]:
        rows = [("", market["metrics"])]
        rows += [(cell["cell"], cell["metrics"]) for cell in market["cells"]]
        for cell, metrics in rows:
            for metric, values in metrics.items():
                writer.writerow((
                    evidence["evidence_id"], evidence["cut"], market["market_id"],
                    cell, metric, values["unit"], values["standard"], values["assisted"],
                    values["assisted_minus_standard"], values.get("relative_change_bp"),
                ))
    return output.getvalue()
