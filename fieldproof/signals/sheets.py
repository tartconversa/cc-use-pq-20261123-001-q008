from __future__ import annotations

import csv
from datetime import UTC, datetime
import re
from typing import TextIO
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from fieldproof.common import invalid
from fieldproof.design.registry import Roster
from fieldproof.signals.contracts import Delivery, Event

REQUIRED = ("event_id", "revision", "unit_id", "cycle_id", "kind", "observed_at")
OPTIONAL = ("fold", "void", "meter_id", "meter_wh", "reason")
LOCAL_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
WHOLE = re.compile(r"[0-9]{1,12}")
MAX_ROWS = 2000


def row_error(line: int, field: str, message: str):
    return invalid("signal_sheet", message, details={"line": line, "field": field})


def integer(text: str, line: int, field: str) -> int:
    if not WHOLE.fullmatch(text):
        raise row_error(line, field, "Expected a nonnegative whole number")
    return int(text)


def boolean(text: str, line: int) -> bool:
    value = {"": False, "false": False, "true": True}.get(text)
    if value is None:
        raise row_error(line, "void", "Expected true or false")
    return value


def local_to_utc(timestamp: datetime, zone: ZoneInfo, fold: str, line: int) -> datetime:
    candidates = {}
    for option in (0, 1):
        instant = timestamp.replace(tzinfo=zone, fold=option).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == timestamp:
            candidates[str(option)] = instant
    if not candidates:
        raise row_error(line, "observed_at", "Local clock time does not exist in the site zone")
    if len(set(candidates.values())) == 1:
        if fold:
            raise row_error(line, "fold", "Fold is only permitted for a repeated local time")
        return next(iter(candidates.values()))
    if fold not in candidates:
        raise row_error(line, "fold", "Repeated local time requires fold 0 or 1")
    return candidates[fold]


def observed_at(text: str, fold: str, zone: ZoneInfo, line: int) -> datetime:
    try:
        timestamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise row_error(line, "observed_at", "Expected an ISO date and time") from exc
    if timestamp.tzinfo is not None:
        if fold:
            raise row_error(line, "fold", "Offset timestamps already disambiguate repeated times")
        return timestamp.astimezone(UTC)
    if not LOCAL_TIMESTAMP.fullmatch(text):
        raise row_error(line, "observed_at", "Use a local date and time with seconds")
    return local_to_utc(timestamp, zone, fold, line)


def row_event(values: dict[str, str], roster: Roster, line: int) -> Event:
    unit = roster.units.get(values["unit_id"])
    if unit is None:
        raise row_error(line, "unit_id", "Device is absent from the trial roster")
    zone = ZoneInfo(roster.sites[unit.site_id].timezone)
    reading = values.get("meter_wh", "")
    entry = {
        "event_id": values["event_id"],
        "revision": integer(values["revision"], line, "revision"),
        "unit_id": values["unit_id"],
        "cycle_id": values["cycle_id"],
        "kind": values["kind"],
        "occurred_at": observed_at(values["observed_at"], values.get("fold", ""), zone, line),
        "void": boolean(values.get("void", ""), line),
        "meter_id": values.get("meter_id") or None,
        "meter_wh": integer(reading, line, "meter_wh") if reading else None,
        "reason": values.get("reason") or None,
    }
    try:
        return Event.model_validate(entry)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        field = ".".join(map(str, first["loc"])) or "event"
        raise row_error(line, field, first["msg"]) from exc


def parse_sheet(source: TextIO, delivery_id: str, roster: Roster) -> Delivery:
    reader = csv.reader(source, strict=True)
    events = []
    seen = set()
    try:
        headings = [entry.strip() for entry in next(reader, [])]
        if (
            len(headings) != len(set(headings))
            or not set(REQUIRED).issubset(headings)
            or not set(headings).issubset(REQUIRED + OPTIONAL)
        ):
            raise row_error(1, "header", "Expected unique required and optional signal columns")
        for row in reader:
            if not row:
                continue
            line = reader.line_num
            if len(events) >= MAX_ROWS:
                raise row_error(line, "event", f"Delivery exceeds {MAX_ROWS} rows")
            if len(row) != len(headings):
                raise row_error(line, "event", "Row width differs from the header")
            values = dict(zip(headings, (value.strip() for value in row), strict=True))
            event = row_event(values, roster, line)
            key = event.event_id, event.revision
            if key in seen:
                raise row_error(line, "revision", "Event revision repeats within the delivery")
            events.append(event)
            seen.add(key)
    except csv.Error as exc:
        raise row_error(reader.line_num, "event", str(exc)) from exc
    if not events:
        raise row_error(1, "event", "Delivery requires at least one row")
    return Delivery(delivery_id=delivery_id, events=events)
