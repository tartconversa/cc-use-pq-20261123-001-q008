from __future__ import annotations

from sqlite3 import Connection
from typing import Iterator

from fieldproof.common import conflict, encoded, fingerprint, invalid, missing
from fieldproof.design.contracts import local_day
from fieldproof.design.registry import Roster, Trial, require_phase
from fieldproof.signals.contracts import MonitorSegment, MonitoringDelivery
from fieldproof.signals.ledger import current_cut, receipt


def validate_placement(segment: MonitorSegment, trial: Trial, roster: Roster):
    unit = roster.units.get(segment.unit_id)
    if unit is None:
        raise missing("unit", segment.unit_id)
    site = roster.sites[unit.site_id]
    span = (
        local_day(segment.online_from, site),
        local_day(segment.online_until, site),
    )
    if not (unit.active_from <= span[0] and span[1] < unit.active_until) and not segment.void:
        raise invalid(
            "monitor_outside_enrollment",
            f"{segment.segment_id} cannot be placed outside this unit's active dates",
            details={"from_local_date": span[0].isoformat(),
                     "until_local_date": span[1].isoformat()},
        )


def append(
    db: Connection,
    trial: Trial,
    roster: Roster,
    delivery: MonitoringDelivery,
) -> dict:
    """监测时段与事件共用事实截点序列，修订与撤回规则与事件台账一致。"""
    trial_id = trial.trial_id
    segments = sorted(delivery.segments, key=lambda item: (item.segment_id, item.revision))
    documents = [item.model_dump(mode="json") for item in segments]
    digest = fingerprint(documents)
    previous = db.execute(
        "SELECT * FROM delivery WHERE trial_id=? AND delivery_id=?",
        (trial_id, delivery.delivery_id),
    ).fetchone()
    if previous is not None:
        if digest != previous["digest"]:
            raise conflict("delivery_changed", "Delivery identifier was reused with other content")
        return receipt(db, trial_id, previous, replayed=True)
    require_phase(trial, "open")
    result = db.execute(
        "INSERT INTO delivery(trial_id, delivery_id, digest) VALUES (?,?,?)",
        (trial_id, delivery.delivery_id, digest),
    )
    seq = result.lastrowid
    accepted = 0
    for segment, document in zip(segments, documents, strict=True):
        segment_digest = fingerprint(document)
        old = db.execute(
            """SELECT digest FROM monitor
               WHERE trial_id=? AND segment_id=? AND revision=?""",
            (trial_id, segment.segment_id, segment.revision),
        ).fetchone()
        if old is not None:
            if old["digest"] != segment_digest:
                raise conflict(
                    "revision_changed",
                    "Monitor revision has different content",
                    details={"segment_id": segment.segment_id, "revision": segment.revision},
                )
            continue
        predecessor_row = db.execute(
            """SELECT revision,segment FROM monitor WHERE trial_id=? AND segment_id=?
               ORDER BY revision DESC LIMIT 1""",
            (trial_id, segment.segment_id),
        ).fetchone()
        expected = 1 if predecessor_row is None else predecessor_row["revision"] + 1
        if segment.revision != expected:
            raise conflict(
                "revision_gap",
                f"Missing predecessor for {segment.segment_id}",
                details={"expected_revision": expected},
            )
        if predecessor_row is not None:
            predecessor = MonitorSegment.model_validate_json(predecessor_row["segment"])
            if predecessor.identity() != segment.identity():
                raise conflict("revision_reassigned",
                               f"Segment {segment.segment_id} changed identity")
        validate_placement(segment, trial, roster)
        db.execute(
            """INSERT INTO monitor(trial_id,segment_id,revision,unit_id,delivery_seq,
                                   segment,digest) VALUES (?,?,?,?,?,?,?)""",
            (
                trial_id,
                segment.segment_id,
                segment.revision,
                segment.unit_id,
                seq,
                encoded(document),
                segment_digest,
            ),
        )
        accepted += 1
    db.execute(
        "UPDATE delivery SET accepted=?, repeated=? WHERE seq=?",
        (accepted, len(segments) - accepted, seq),
    )
    row = db.execute("SELECT * FROM delivery WHERE seq=?", (seq,)).fetchone()
    return receipt(db, trial_id, row, replayed=False)


def segments_at(db: Connection, trial_id: str, cut: int) -> Iterator[MonitorSegment]:
    rows = db.execute(
        """WITH latest AS (
                SELECT segment_id, MAX(revision) AS revision
                FROM monitor
                WHERE trial_id=? AND delivery_seq<=?
                GROUP BY segment_id
            )
            SELECT monitor.segment FROM monitor JOIN latest USING(segment_id,revision)
            WHERE monitor.trial_id=? ORDER BY unit_id,segment_id""",
        (trial_id, cut, trial_id),
    )
    for row in rows:
        segment = MonitorSegment.model_validate_json(row["segment"])
        if not segment.void:
            yield segment


def merged_online_windows(
    segments: list[MonitorSegment],
) -> dict[str, list[tuple]]:
    """每台设备的在线时段求并集，得到互不相交、按时间排序的连续观察窗口。"""
    by_unit: dict[str, list[tuple]] = {}
    for segment in segments:
        by_unit.setdefault(segment.unit_id, []).append(
            (segment.online_from, segment.online_until)
        )
    merged: dict[str, list[tuple]] = {}
    for unit_id, windows in by_unit.items():
        windows.sort()
        union = []
        for start, end in windows:
            if union and start <= union[-1][1]:
                if end > union[-1][1]:
                    union[-1] = (union[-1][0], end)
            else:
                union.append((start, end))
        merged[unit_id] = union
    return merged
