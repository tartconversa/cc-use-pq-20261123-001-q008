from __future__ import annotations

from sqlite3 import Connection
from typing import Iterable, Iterator

from fieldproof.common import conflict, encoded, fingerprint, invalid, missing
from fieldproof.design.contracts import enrolled_at, local_day
from fieldproof.design.registry import Roster, Trial, require_phase
from fieldproof.signals.contracts import Delivery, Event


def current_cut(db: Connection, trial_id: str) -> int:
    row = db.execute(
        "SELECT MAX(delivery_seq) FROM signal WHERE trial_id=?", (trial_id,)
    ).fetchone()
    return row[0] or 0


def require_cut(db: Connection, trial_id: str, cut: int):
    if cut == 0:
        return
    row = db.execute(
        "SELECT 1 FROM signal WHERE trial_id=? AND delivery_seq=? LIMIT 1",
        (trial_id, cut),
    ).fetchone()
    if row is None:
        raise missing("cut", str(cut))


def receipt(db: Connection, trial_id: str, row, replayed: bool) -> dict:
    return {
        "delivery_id": row["delivery_id"],
        "delivery_sequence": row["seq"],
        "accepted": row["accepted"],
        "repeated": row["repeated"],
        "current_cut": current_cut(db, trial_id),
        "replayed": replayed,
    }


def validate_placement(event: Event, trial: Trial, roster: Roster):
    unit = roster.units.get(event.unit_id)
    if unit is None:
        raise missing("unit", event.unit_id)
    day = local_day(event.occurred_at, roster.sites[unit.site_id])
    if event.kind == "start":
        valid_day = enrolled_at(unit, day)
    else:
        valid_day = unit.active_from <= day and (day - unit.active_until).days < 2
    if not valid_day and not event.void:
        raise invalid(
            "signal_outside_enrollment",
            f"{event.event_id} cannot be placed in this unit's active dates",
            details={"local_date": day.isoformat()},
        )


def append(
    db: Connection,
    trial: Trial,
    roster: Roster,
    delivery: Delivery,
) -> dict:
    trial_id = trial.trial_id
    events = sorted(delivery.events, key=lambda event: (event.event_id, event.revision))
    documents = [event.model_dump(mode="json") for event in events]
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
    for event, document in zip(events, documents, strict=True):
        event_digest = fingerprint(document)
        old = db.execute(
            """SELECT digest FROM signal
               WHERE trial_id=? AND event_id=? AND revision=?""",
            (trial_id, event.event_id, event.revision),
        ).fetchone()
        if old is not None:
            if old["digest"] != event_digest:
                raise conflict(
                    "revision_changed",
                    "Event revision has different content",
                    details={"event_id": event.event_id, "revision": event.revision},
                )
            continue
        previous = db.execute(
            """SELECT revision,event FROM signal WHERE trial_id=? AND event_id=?
               ORDER BY revision DESC LIMIT 1""",
            (trial_id, event.event_id),
        ).fetchone()
        expected = 1 if previous is None else previous["revision"] + 1
        if event.revision != expected:
            raise conflict(
                "revision_gap",
                f"Missing predecessor for {event.event_id}",
                details={"expected_revision": expected},
            )
        if previous is not None:
            predecessor = Event.model_validate_json(previous["event"])
            if event.identity() != predecessor.identity():
                raise conflict("revision_reassigned", f"Event {event.event_id} changed identity")
        validate_placement(event, trial, roster)
        db.execute(
            """INSERT INTO signal(trial_id,event_id,revision,unit_id,cycle_id,
                                  delivery_seq,event,digest) VALUES (?,?,?,?,?,?,?,?)""",
            (
                trial_id,
                event.event_id,
                event.revision,
                event.unit_id,
                event.cycle_id,
                seq,
                encoded(document),
                event_digest,
            ),
        )
        accepted += 1
    db.execute(
        "UPDATE delivery SET accepted=?, repeated=? WHERE seq=?",
        (accepted, len(events) - accepted, seq),
    )
    row = db.execute("SELECT * FROM delivery WHERE seq=?", (seq,)).fetchone()
    return receipt(db, trial_id, row, replayed=False)


def history(db: Connection, trial_id: str, event_id: str) -> list[dict]:
    rows = db.execute(
        """SELECT event, delivery_seq FROM signal WHERE trial_id=? AND event_id=?
           ORDER BY revision""",
        (trial_id, event_id),
    )
    items = [
        {
            "event": Event.model_validate_json(row["event"]).model_dump(mode="json"),
            "delivery_sequence": row["delivery_seq"],
        }
        for row in rows
    ]
    if not items:
        raise missing("event", event_id)
    return items


def events_at(
    db: Connection,
    trial_id: str,
    cut: int,
    *,
    unit_id: str | None = None,
    cycle_id: str | None = None,
) -> Iterator[Event]:
    conditions = ["trial_id=?", "delivery_seq<=?"]
    parameters: list[str | int] = [trial_id, cut]
    for column, value in (("unit_id", unit_id), ("cycle_id", cycle_id)):
        if value is not None:
            conditions.append(f"{column}=?")
            parameters.append(value)
    selector = " AND ".join(conditions)
    rows = db.execute(
        f"""WITH latest AS (
                SELECT event_id, MAX(revision) AS revision
                FROM signal WHERE {selector} GROUP BY event_id
            )
            SELECT signal.event FROM signal JOIN latest USING(event_id,revision)
            WHERE signal.trial_id=? ORDER BY unit_id,cycle_id,event_id""",
        (*parameters, trial_id),
    )
    for row in rows:
        event = Event.model_validate_json(row["event"])
        if not event.void:
            yield event


def cycle_inputs(events: Iterable[Event]) -> Iterator[tuple[tuple[str, str], list[Event]]]:
    from itertools import groupby

    for key, group in groupby(events, key=lambda event: (event.unit_id, event.cycle_id)):
        yield key, list(group)
