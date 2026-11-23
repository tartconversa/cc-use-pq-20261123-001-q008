from __future__ import annotations

from datetime import UTC
from typing import Annotated, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from fieldproof.design.contracts import Input, Key, WholeSecond

SignalKind = Literal["start", "complete", "abort", "manual", "alarm", "meter"]
MeterValue = Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
Revision = Annotated[StrictInt, Field(ge=1, le=1_000_000)]


class Event(Input):
    event_id: Key
    revision: Revision
    unit_id: Key
    cycle_id: Key
    occurred_at: WholeSecond
    kind: SignalKind
    void: bool = False
    meter_wh: MeterValue | None = None
    meter_id: Key | None = None
    reason: Key | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value):
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def signal_fields(self):
        if self.void:
            if self.revision == 1:
                raise ValueError("A retraction must revise an existing event")
            if any(v is not None for v in (self.meter_wh, self.meter_id, self.reason)):
                raise ValueError("A retraction cannot contain a measurement or reason")
            return self
        meter = self.kind == "meter"
        if meter != (self.meter_wh is not None) or meter != (self.meter_id is not None):
            raise ValueError("Only meter events carry both a meter ID and a Wh reading")
        if (self.kind in {"abort", "alarm"}) != (self.reason is not None):
            raise ValueError("Only abort and alarm events require reason codes")
        return self

    def identity(self) -> tuple[str, str, str]:
        return self.unit_id, self.cycle_id, self.kind


class Delivery(Input):
    delivery_id: Key
    events: list[Event] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_revisions(self):
        keys = {(event.event_id, event.revision) for event in self.events}
        if len(keys) != len(self.events):
            raise ValueError("A delivery cannot contain an event revision twice")
        return self
