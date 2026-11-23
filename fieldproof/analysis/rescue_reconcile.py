from __future__ import annotations

from collections import Counter
from sqlite3 import Connection

from fieldproof.analysis.evidence import analyze, load
from fieldproof.analysis.rescue import (
    INTERVAL,
    NONE_YET,
    PRECISE,
    RescueRecord,
    build_records,
    market_readout,
)
from fieldproof.common import invalid
from fieldproof.design.registry import Trial, roster as load_roster

# 周期在首次救场视角下的五种互斥分类
CLASS_PENDING = "partially_observed_open"
CLASS_GAP = "partially_observed_gap"
CLASS_RESCUE = "first_rescue_observed"
CLASS_COMPLETE = "closed_without_rescue_completed"
CLASS_ABORT = "closed_without_rescue_aborted"


def classify(record: RescueRecord) -> str:
    if not record.origin_observed:
        return CLASS_GAP
    if record.ambiguous or record.status == INTERVAL:
        # 区间删失一律视为部分观察：可能已救场，也可能按原结局闭合
        return CLASS_PENDING if record.partial_reason == "cycle_still_open" else CLASS_GAP
    if record.status == PRECISE:
        return CLASS_RESCUE
    if record.closed:
        return CLASS_ABORT if record.terminal_kind == "aborted" else CLASS_COMPLETE
    return CLASS_PENDING


def _index(records: list[RescueRecord]) -> dict[tuple[str, str], RescueRecord]:
    return {(record.unit_id, record.cycle_id): record for record in records}


def record_diff(before: RescueRecord | None, after: RescueRecord | None) -> dict | None:
    if before is not None and after is not None:
        same = (
            before.status == after.status
            and before.first_manual_at == after.first_manual_at
            and before.terminal == after.terminal
            and before.terminal_kind == after.terminal_kind
            and before.partial_reason == after.partial_reason
            and before.last_confirmed == after.last_confirmed
            and before.sources == after.sources
        )
        if same:
            return None
    return {
        "unit_id": (after or before).unit_id,
        "cycle_id": (after or before).cycle_id,
        "before_classification": classify(before) if before else None,
        "after_classification": classify(after) if after else None,
        "before_first_manual_at": _stamp(before),
        "after_first_manual_at": _stamp(after),
        "before_revisions": before.sources if before else {},
        "after_revisions": after.sources if after else {},
    }


def _stamp(record: RescueRecord | None):
    from fieldproof.common import utc_text

    if record is None or record.first_manual_at is None:
        return None
    return utc_text(record.first_manual_at)


def reconcile_rescue(
    db: Connection,
    trial: Trial,
    window,
    before_cut: int,
    after_cut: int,
    market_id: str,
    *,
    limit: int,
    offset: int,
) -> dict:
    """同一次更正如何同时改变：仍有机会的设备、首次结局分类、可签发的市场判断。"""
    if before_cut > after_cut:
        raise invalid("cut_order", "The earlier cut must be no later than the later cut")
    assigned = load_roster(db, trial.trial_id)
    before_material = load(db, trial, window, before_cut)
    after_material = load(db, trial, window, after_cut)
    before_records = build_records(db, trial, assigned, before_cut, window=window)
    after_records = build_records(db, trial, assigned, after_cut, window=window)
    old_index, new_index = _index(before_records), _index(after_records)

    changes = []
    for key in sorted(old_index.keys() | new_index.keys()):
        diff = record_diff(old_index.get(key), new_index.get(key))
        if diff is not None:
            changes.append(diff)

    def at_risk_units(records: list[RescueRecord]) -> dict[str, list[str]]:
        grouped: dict[str, set] = {}
        for record in records:
            grouped.setdefault(record.status, set()).add(record.unit_id)
        return {
            "clean_no_event_units": sorted(grouped.get(NONE_YET, set())),
            "precise_rescue_units": sorted(grouped.get(PRECISE, set())),
            "interval_censored_units": sorted(grouped.get(INTERVAL, set())),
        }

    before_readout = market_readout(trial, before_material, before_records, market_id)
    after_readout = market_readout(trial, after_material, after_records, market_id)
    before_legacy = _legacy_market(analyze(trial, before_material), market_id)
    after_legacy = _legacy_market(analyze(trial, after_material), market_id)

    transition: dict[str, Counter] = {}
    for diff in changes:
        source = diff["before_classification"] or "newly_observed"
        transition.setdefault(source, Counter())[diff["after_classification"]] += 1
    transitions = [
        {"from_classification": source, "to_classification": target, "cycles": count}
        for source, targets in sorted(transition.items())
        for target, count in sorted(targets.items())
    ]
    return {
        "market_id": market_id,
        "window": window.payload(),
        "before_cut": before_cut,
        "after_cut": after_cut,
        "still_at_risk": {
            "before": at_risk_units(before_records),
            "after": at_risk_units(after_records),
        },
        "classification_transitions": transitions,
        "rescue_decision": {
            "before": before_readout["rescue_decision"],
            "after": after_readout["rescue_decision"],
        },
        "legacy_manual_rate": {
            "before": before_legacy["metrics"]["manual"],
            "after": after_legacy["metrics"]["manual"],
        },
        "total_changed_cycles": len(changes),
        "cycles": changes[offset:offset + limit],
        "next_offset": offset + limit if offset + limit < len(changes) else None,
    }


def _legacy_market(document: dict, market_id: str) -> dict:
    for result in document["market_results"]:
        if result["market_id"] == market_id:
            return result
    from fieldproof.common import missing
    raise missing("market", market_id)
