from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction

from fieldproof.analysis.cohorts import ARMS, UnitSummary, by_placement
from fieldproof.analysis.contrast import display
from fieldproof.analysis.evidence import Material
from fieldproof.design.contracts import Policy
from fieldproof.design.registry import Roster, Trial
from fieldproof.signals.cycles import resolve_cycles
from fieldproof.signals.ledger import events_at
from fieldproof.signals.monitoring import merged_online_windows, segments_at

RESCUE = "rescue"
COMPLETE = "completed"
ABORT = "aborted"
CAUSES = (RESCUE, COMPLETE, ABORT)
CAUSE_LABELS = {
    RESCUE: "first_manual_rescue",
    COMPLETE: "normal_completion",
    ABORT: "fault_termination",
}
TELEMETRY_GAP = "telemetry_gap"
PENDING_OPEN = "cycle_still_open"

# 首次救场的可定位程度
PRECISE = "precise"
INTERVAL = "interval_censored"
NONE_YET = "no_event"

# 极值情景：无法精确定位的首次救场分别取最早/最晚可能位置
LOWER = "lower"
UPPER = "upper"


@dataclass(frozen=True)
class RescueRecord:
    unit_id: str
    cycle_id: str
    day: object
    origin: datetime
    terminal: datetime | None
    terminal_kind: str | None                  # complete / aborted / None（未闭合）
    terminal_in_gap: bool
    first_manual_at: datetime | None
    first_manual_tie: bool                     # 救场与终止同一时刻，先后不可判定
    status: str                                # precise / interval_censored / no_event
    uncertain_from: datetime | None            # 无法精确定位区间的两端（绝对时刻）
    uncertain_until: datetime | None
    partial_reason: str | None                 # telemetry_gap / cycle_still_open
    last_confirmed: datetime                   # 最后可确认时刻
    observed_until: datetime | None            # 起点所在连续观察段的右端
    origin_observed: bool
    repeated_presses: int
    sources: dict[str, int]

    @property
    def closed(self) -> bool:
        return self.terminal is not None

    @property
    def ambiguous(self) -> bool:
        return self.status == INTERVAL or self.first_manual_tie


def _seconds(origin: datetime, moment: datetime) -> int:
    return int((moment - origin).total_seconds())


def _connected_cover(union: list[tuple], origin: datetime) -> tuple[datetime | None, bool]:
    """包含周期起点的连续观察段的右端，以及起点是否落在在线窗口内。"""
    for start, end in union:
        if start <= origin < end:
            return end, True
        if start > origin:
            break
    return None, False


def _gap_reaching(union: list[tuple], moment: datetime, origin: datetime) -> tuple | None:
    """找出使 moment 无法被从 origin 起的连续在线覆盖的最近缺口 (gap_start, gap_end)。

    端点相接不算缺口；缺口右端取 min(实际缺口右端, moment)。
    """
    cursor = origin
    for start, end in union:
        if end <= origin:
            continue
        if start > cursor:
            return cursor, min(start, moment)
        if start >= moment:
            break
        cursor = max(cursor, end)
        if cursor >= moment:
            return None
    if cursor < moment:
        return cursor, moment
    return None


def build_records(
    db,
    trial: Trial,
    roster: Roster,
    cut: int,
    *,
    window,
) -> list[RescueRecord]:
    """从事件与监测时段重建每个周期的首次救场时间线。

    合格周期包含已闭合（complete/aborted）与未闭合（pending）周期；
    没有提交监测时段的设备按连续观察处理，保持旧试点的读取不变。
    """
    events = list(events_at(db, trial.trial_id, cut))
    windows = merged_online_windows(list(segments_at(db, trial.trial_id, cut)))
    resolved = {(cycle.unit_id, cycle.cycle_id): cycle
                for cycle in resolve_cycles(events, roster, trial.plan.policy)}
    grouped: dict[tuple[str, str], list] = defaultdict(list)
    for event in events:
        grouped[event.unit_id, event.cycle_id].append(event)

    records: list[RescueRecord] = []
    for (unit_id, cycle_id), cycle_events in sorted(grouped.items()):
        cycle = resolved.get((unit_id, cycle_id))
        if cycle is None or cycle.start is None or cycle.state == "orphan":
            continue
        # 未闭合（pending）周期同样进入首次救场时间线；无效周期排除
        if cycle.state == "invalid":
            continue
        if cycle.day is not None and not window.contains(cycle.day):
            continue
        union = windows.get(unit_id, [])
        origin = cycle.start.occurred_at
        terminal = cycle.close
        end_moment = terminal.occurred_at if terminal is not None else None
        # 同刻多笔按键：以事件标识次序兜底，保证“第一项有效救场”可稳定复现
        order_index = {item.event_id: index for index, item in enumerate(cycle_events)}
        manuals = sorted(
            (item for item in cycle_events if item.kind == "manual"),
            key=lambda item: (item.occurred_at, order_index[item.event_id]),
        )
        first_manual = manuals[0] if manuals else None
        repeated = len(manuals) - 1 if manuals else 0
        sources = {event.event_id: event.revision for event in cycle_events}
        event_times = [event.occurred_at for event in cycle_events]

        # ---- 无监测提交：默认全程在线（旧试点行为） ----
        if not union:
            last_confirmed = end_moment or max(event_times)
            if first_manual is not None:
                tie = terminal is not None and first_manual.occurred_at == end_moment
                records.append(RescueRecord(
                    unit_id, cycle_id, cycle.day, origin, end_moment,
                    terminal.kind if terminal else None, False,
                    first_manual.occurred_at, tie,
                    INTERVAL if tie else PRECISE,
                    origin if tie else None, end_moment if tie else None,
                    None if terminal else PENDING_OPEN,
                    last_confirmed, end_moment, True, repeated, sources,
                ))
            else:
                records.append(RescueRecord(
                    unit_id, cycle_id, cycle.day, origin, end_moment,
                    terminal.kind if terminal else None, False,
                    None, False, NONE_YET, None, None,
                    None if terminal else PENDING_OPEN,
                    last_confirmed, None, True, repeated, sources,
                ))
            continue

        cover_end, covered = _connected_cover(union, origin)

        # ---- 起点本身不可观察：整周期只有部分观察，不进入风险判断 ----
        if not covered:
            frontier = max(event_times + [end for _, end in union])
            records.append(RescueRecord(
                unit_id, cycle_id, cycle.day, origin, end_moment,
                terminal.kind if terminal else None, terminal is not None,
                first_manual.occurred_at if first_manual else None, False,
                INTERVAL if first_manual else NONE_YET,
                origin, first_manual.occurred_at if first_manual else None,
                TELEMETRY_GAP, frontier, None, False, repeated, sources,
            ))
            continue

        if terminal is not None:
            last_confirmed = end_moment
            terminal_gap = _gap_reaching(union, end_moment, origin)
            terminal_in_gap = terminal_gap is not None
            partial_reason = TELEMETRY_GAP if terminal_in_gap else None
        else:
            # 未闭合：观察前沿即连续观察段右端——在线本身证明此前没有结局
            last_confirmed = cover_end
            terminal_gap = None
            terminal_in_gap = False
            partial_reason = PENDING_OPEN

        if first_manual is not None:
            manual_moment = first_manual.occurred_at
            tie = terminal is not None and manual_moment == end_moment
            manual_gap = None if tie else _gap_reaching(union, manual_moment, origin)
            if tie:
                # 发生在最后可确认时刻的救场：事实先后不可追，纳入不确定性
                records.append(RescueRecord(
                    unit_id, cycle_id, cycle.day, origin, end_moment,
                    terminal.kind, terminal_in_gap, manual_moment, True,
                    INTERVAL, origin, end_moment,
                    partial_reason or TELEMETRY_GAP,
                    last_confirmed, cover_end, True, repeated, sources,
                ))
            elif manual_gap is not None:
                # 中途掉线又恢复：后来收到的救场不能当作全周期首次救场
                records.append(RescueRecord(
                    unit_id, cycle_id, cycle.day, origin, end_moment,
                    terminal.kind if terminal else None, terminal_in_gap,
                    manual_moment, False, INTERVAL,
                    manual_gap[0], manual_moment,
                    partial_reason, last_confirmed, cover_end, True,
                    repeated, sources,
                ))
            else:
                records.append(RescueRecord(
                    unit_id, cycle_id, cycle.day, origin, end_moment,
                    terminal.kind if terminal else None, terminal_in_gap,
                    manual_moment, False, PRECISE, None, None,
                    partial_reason, last_confirmed, cover_end, True,
                    repeated, sources,
                ))
        else:
            uncertain = terminal_gap if terminal is not None and terminal_in_gap else None
            records.append(RescueRecord(
                unit_id, cycle_id, cycle.day, origin, end_moment,
                terminal.kind if terminal else None, terminal_in_gap,
                None, False,
                INTERVAL if uncertain else NONE_YET,
                uncertain[0] if uncertain else None,
                uncertain[1] if uncertain else None,
                partial_reason, last_confirmed, cover_end, True,
                repeated, sources,
            ))
    return records


def _extremal_history(record: RescueRecord, scenario: str) -> tuple[int, str | None]:
    """转成单条 (时间秒, 结局cause或右删失None)。同一周期只允许一个终点。"""
    if record.status == PRECISE and record.first_manual_at is not None:
        return _seconds(record.origin, record.first_manual_at), RESCUE
    if record.status == NONE_YET and record.closed and not record.terminal_in_gap:
        cause = COMPLETE if record.terminal_kind == "complete" else ABORT
        return _seconds(record.origin, record.terminal), cause
    if record.ambiguous or record.status == INTERVAL:
        if scenario == LOWER:
            # 最早可能：救场落在未知区间左端
            return _seconds(record.origin, record.uncertain_from), RESCUE
        # 最晚可能：已闭合则闭合结局先发生；未闭合则在最后确认处右删失
        if record.closed:
            cause = COMPLETE if record.terminal_kind == "complete" else ABORT
            return _seconds(record.origin, record.terminal), cause
        return _seconds(record.origin, record.last_confirmed), None
    return _seconds(record.origin, record.last_confirmed), None


def aalen_johansen(
    histories: list[tuple[int, str | None]], times: list[int]
) -> dict[str, list[Fraction]]:
    """竞争风险累积发生率曲线，在各固定运行时点取值。"""
    event_clock: dict[int, Counter] = defaultdict(Counter)
    censor_clock: Counter = Counter()
    for moment, cause in histories:
        if cause is None:
            censor_clock[moment] += 1
        else:
            event_clock[moment][cause] += 1
    grid = sorted(set(event_clock) | set(censor_clock))
    at_risk = len(histories)
    survival = Fraction(1)
    cif = {cause: Fraction(0) for cause in CAUSES}
    curve: dict[int, dict[str, Fraction]] = {0: dict(cif)}
    for moment in grid:
        deaths = event_clock[moment]
        d_total = sum(deaths.values())
        if at_risk > 0 and d_total:
            for cause in CAUSES:
                count = deaths.get(cause, 0)
                if count:
                    cif[cause] += survival * Fraction(count, at_risk)
            survival *= Fraction(1) - Fraction(d_total, at_risk)
        at_risk -= d_total + censor_clock.get(moment, 0)
        curve[moment] = dict(cif)
    ordered = sorted(curve)
    result = {cause: [] for cause in CAUSES}
    for checkpoint in times:
        chosen = max(moment for moment in ordered if moment <= checkpoint)
        for cause in CAUSES:
            result[cause].append(curve[chosen][cause])
    return result


def unit_curves(
    records: list[RescueRecord], checkpoints: list[int]
) -> dict[str, dict[str, dict[str, list[Fraction]]]]:
    by_unit = defaultdict(list)
    for record in records:
        by_unit[record.unit_id].append(record)
    curves = {}
    for unit_id, unit_records in by_unit.items():
        curves[unit_id] = {
            scenario: aalen_johansen(
                [_extremal_history(record, scenario) for record in unit_records],
                checkpoints,
            )
            for scenario in (LOWER, UPPER)
        }
    return curves


@dataclass
class CheckpointCounts:
    entered: int = 0
    at_risk_units: set = field(default_factory=set)
    precise_rescue: int = 0
    possible_rescue: int = 0
    completed: int = 0
    aborted: int = 0
    possible_completed: int = 0
    possible_aborted: int = 0
    partial_gap: int = 0
    partial_open: int = 0
    unobserved_origin: int = 0

    def payload(self) -> dict:
        return {
            "entered_cycles": self.entered,
            "at_risk_units": len(self.at_risk_units),
            "first_rescue": {
                "precise_cycles": self.precise_rescue,
                "possible_cycles": self.possible_rescue,
            },
            "normal_completion": {
                "definite_cycles": self.completed,
                "possible_cycles": self.possible_completed,
            },
            "fault_termination": {
                "definite_cycles": self.aborted,
                "possible_cycles": self.possible_aborted,
            },
            "partially_observed": {
                "telemetry_gap_cycles": self.partial_gap,
                "still_open_cycles": self.partial_open,
                "unobserved_origin_cycles": self.unobserved_origin,
            },
        }


def count_at_checkpoint(record: RescueRecord, checkpoint: int, counts: CheckpointCounts):
    """按事实先后累计周期；无法判断的周期只进可能计数与部分观察，绝不补全。"""
    # observed_until 为 None 且起点可观察：设备未提交监测，沿用旧默认连续观察
    if not record.origin_observed:
        counts.unobserved_origin += 1
        return
    age_confirmed = _seconds(record.origin, record.last_confirmed)
    if age_confirmed <= 0 and record.observed_until is not None:
        counts.unobserved_origin += 1
        return
    counts.entered += 1

    if record.partial_reason == TELEMETRY_GAP:
        counts.partial_gap += 1
    elif record.partial_reason == PENDING_OPEN:
        counts.partial_open += 1

    # 明确知道此前没有事件的运行才进入风险判断
    clean_through = False
    if record.status == PRECISE or record.status == NONE_YET:
        clean_through = age_confirmed >= checkpoint
    elif record.uncertain_from is not None:
        clean_through = _seconds(record.origin, record.uncertain_from) >= checkpoint
    if clean_through:
        counts.at_risk_units.add(record.unit_id)

    age_manual = (_seconds(record.origin, record.first_manual_at)
                  if record.first_manual_at is not None else None)
    if record.status == PRECISE and age_manual is not None and age_manual <= checkpoint:
        counts.precise_rescue += 1
    elif record.ambiguous and age_manual is not None and age_manual <= checkpoint:
        counts.possible_rescue += 1
    elif record.status == INTERVAL and record.first_manual_at is None \
            and record.uncertain_from is not None \
            and _seconds(record.origin, record.uncertain_from) <= checkpoint:
        counts.possible_rescue += 1

    if record.closed:
        age_terminal = _seconds(record.origin, record.terminal)
        if age_terminal <= checkpoint:
            if record.terminal_kind == "complete":
                if record.ambiguous:
                    counts.possible_completed += 1
                else:
                    counts.completed += 1
            elif record.terminal_kind == "aborted":
                if record.ambiguous:
                    counts.possible_aborted += 1
                else:
                    counts.aborted += 1


def cell_checkpoint(
    cell_key: str,
    weight_bp: int,
    arm: str,
    eligible_units: list[UnitSummary],
    records: list[RescueRecord],
    checkpoints: list[int],
    policy: Policy,
) -> dict:
    eligible_ids = {unit.unit_id for unit in eligible_units}
    arm_records = [record for record in records if record.unit_id in eligible_ids]
    curves = unit_curves(arm_records, checkpoints)
    participating = sorted(curves)
    points = []
    supported = True
    for index, checkpoint in enumerate(checkpoints):
        counts = CheckpointCounts()
        for record in arm_records:
            count_at_checkpoint(record, checkpoint, counts)
        if participating:
            cumulative = {}
            for cause in CAUSES:
                values = []
                for scenario in (LOWER, UPPER):
                    values.append(
                        sum(curves[unit_id][scenario][cause][index]
                            for unit_id in participating)
                        / len(participating)
                    )
                low, high = min(values), max(values)
                cumulative[CAUSE_LABELS[cause]] = {
                    "low_bp": display(low, 10_000),
                    "high_bp": display(high, 10_000),
                }
        else:
            cumulative = {label: {"low_bp": None, "high_bp": None}
                          for label in CAUSE_LABELS.values()}
        enough = (len(eligible_units) >= policy.required_units_per_arm
                  and len(participating) >= policy.required_units_per_arm)
        supported = supported and enough
        if not enough:
            # 证据不足的时点不能用最后已知比例补全
            cumulative = {label: {"low_bp": None, "high_bp": None}
                          for label in CAUSE_LABELS.values()}
        points.append({
            "checkpoint_seconds": checkpoint,
            "supported": enough,
            "participating_units": len(participating),
            "eligible_units": len(eligible_units),
            "coverage": counts.payload(),
            "cumulative_incidence_bp": cumulative,
        })
    repeated_press_cycles = sum(
        1 for record in arm_records if record.repeated_presses
    )
    return {
        "cell": cell_key,
        "arm": arm,
        "weight_bp": weight_bp,
        "supported": supported,
        "repeated_press_cycles": repeated_press_cycles,
        "checkpoints": points,
    }


def _weighted_bounds(cells: list[dict], cause_label: str, index: int,
                     sign: int) -> tuple[Fraction, Fraction] | None:
    """固定市场权重汇总界限；任一单元不支持则该时点不可用，不用最后已知比例补全。"""
    low_total = Fraction(0)
    high_total = Fraction(0)
    for cell in cells:
        point = cell["checkpoints"][index]
        if not point["supported"]:
            return None
        bounds = point["cumulative_incidence_bp"][cause_label]
        if bounds["low_bp"] is None:
            return None
        weight = Fraction(cell["weight_bp"], 10_000)
        low = Fraction(bounds["low_bp"]) / 10_000 * weight
        high = Fraction(bounds["high_bp"]) / 10_000 * weight
        low_total += sign * low
        high_total += sign * high
    return low_total, high_total


def rescue_decision(
    cell_rows: list[dict],
    policy: Policy,
    checkpoints: list[int],
) -> dict:
    """区分“争取到时间”与“更早终止”：救场界限与中止界限分别评估，证据不足即拦截。"""
    standard_cells = [row for row in cell_rows if row["arm"] == "standard"]
    assisted_cells = [row for row in cell_rows if row["arm"] == "assisted"]
    verdicts = []
    abort_blocked = rescue_blocked = partial_blocked = False
    saving_boundary = Fraction(policy.min_manual_saving_bp, 10_000)
    failure_boundary = Fraction(policy.max_failure_increase_bp, 10_000)
    partial_boundary = Fraction(policy.max_missing_energy_bp, 10_000)

    for index, checkpoint in enumerate(checkpoints):
        entry = {"checkpoint_seconds": checkpoint}
        if not all(row["checkpoints"][index]["supported"] for row in cell_rows):
            entry["supported"] = False
            entry["verdict"] = "insufficient_evidence"
            verdicts.append(entry)
            continue
        entry["supported"] = True
        rescue_standard = _weighted_bounds(standard_cells, CAUSE_LABELS[RESCUE], index, 1)
        rescue_assisted = _weighted_bounds(assisted_cells, CAUSE_LABELS[RESCUE], index, 1)
        abort_standard = _weighted_bounds(standard_cells, CAUSE_LABELS[ABORT], index, 1)
        abort_assisted = _weighted_bounds(assisted_cells, CAUSE_LABELS[ABORT], index, 1)
        # 救场节省 = 标准 - 辅助；界限做区间减法
        rescue_low = rescue_standard[0] - rescue_assisted[1]
        rescue_high = rescue_standard[1] - rescue_assisted[0]
        abort_low = abort_assisted[0] - abort_standard[1]
        abort_high = abort_assisted[1] - abort_standard[0]

        partial_shares = []
        for row in cell_rows:
            coverage = row["checkpoints"][index]["coverage"]
            partial = coverage["partially_observed"]
            observed_partial = (partial["telemetry_gap_cycles"]
                                + partial["still_open_cycles"])
            entered = coverage["entered_cycles"]
            if entered:
                partial_shares.append(Fraction(observed_partial, entered))
        partial_share = max(partial_shares, default=Fraction(0))

        entry["rescue_rate_difference_bp"] = {
            "definition": "standard_minus_assisted",
            "low_bp": display(rescue_low, 10_000),
            "high_bp": display(rescue_high, 10_000),
        }
        entry["abort_rate_difference_bp"] = {
            "definition": "assisted_minus_standard",
            "low_bp": display(abort_low, 10_000),
            "high_bp": display(abort_high, 10_000),
        }
        entry["max_partial_observation_share_bp"] = display(partial_share, 10_000)
        reasons = []
        if abort_high > failure_boundary:
            abort_blocked = True
            reasons.append("earlier_termination_increase")
        if rescue_high < saving_boundary:
            rescue_blocked = True
            reasons.append("rescue_saving_not_demonstrated")
        if partial_share > partial_boundary:
            partial_blocked = True
            reasons.append("too_much_partial_observation")
        entry["blocking_reasons"] = reasons
        entry["verdict"] = "hold" if reasons else "pass"
        verdicts.append(entry)

    if not checkpoints:
        overall = "not_configured"
    elif any(item["verdict"] == "insufficient_evidence" for item in verdicts):
        overall = "insufficient"
    elif abort_blocked or rescue_blocked or partial_blocked:
        overall = "hold"
    else:
        overall = "advance"
    return {
        "recommendation": overall,
        "checkpoints": verdicts,
        "guardrails": {
            "min_manual_saving_bp": policy.min_manual_saving_bp,
            "max_failure_increase_bp": policy.max_failure_increase_bp,
            "max_partial_observation_bp": policy.max_missing_energy_bp,
        },
    }


def market_readout(
    trial: Trial,
    material: Material,
    records: list[RescueRecord],
    market_id: str,
) -> dict:
    market = trial.plan.market(market_id)
    if market is None:
        from fieldproof.common import missing
        raise missing("market", market_id)
    policy = trial.plan.policy
    checkpoints = list(policy.rescue_checkpoints_seconds)
    placed = by_placement(material.units.values())
    cell_rows = []
    for cell in market.cells:
        for arm in ARMS:
            summaries = placed.get((market_id, cell.key, arm), [])
            eligible = [unit for unit in summaries if unit.eligible(policy)]
            cell_rows.append(cell_checkpoint(
                cell.key, cell.weight_bp, arm, eligible, records, checkpoints, policy,
            ))
    return {
        "market_id": market_id,
        "label": market.label,
        "cut": material.cut,
        "window": material.window.payload(),
        "checkpoints_seconds": checkpoints,
        "interpretation": (
            "First manual rescue per cycle; repeated presses explain process but count once. "
            "Bounds span telemetry gaps and unresolvable event ordering."
        ),
        "cells": cell_rows,
        "rescue_decision": rescue_decision(cell_rows, policy, checkpoints),
    }
