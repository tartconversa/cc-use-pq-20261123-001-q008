from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)
]
Arm = Literal["standard", "assisted"]
Climate = Literal["cold", "temperate", "warm"]
Load = Literal["light", "heavy"]
Rate = Annotated[StrictInt, Field(ge=0, le=10_000)]
Weight = Annotated[StrictInt, Field(gt=0, le=10_000)]
WholeSecond = Annotated[AwareDatetime, Field()]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def whole_seconds(cls, value):
        if isinstance(value, datetime) and value.microsecond:
            raise ValueError("Only whole-second timestamps are accepted")
        return value


class Cell(Input):
    climate: Climate
    load: Load
    weight_bp: Weight

    @property
    def key(self) -> str:
        return f"{self.climate}/{self.load}"


class Market(Input):
    market_id: Key
    label: ShortText
    cells: list[Cell] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def composition(self):
        if len({cell.key for cell in self.cells}) != len(self.cells):
            raise ValueError("A market cannot have duplicate comparison cells")
        if sum(cell.weight_bp for cell in self.cells) != 10_000:
            raise ValueError("Market weights must add up to 10,000 basis points")
        return self


class Policy(Input):
    required_units_per_arm: Annotated[StrictInt, Field(ge=2, le=100)]
    required_cycles_per_unit: Annotated[StrictInt, Field(ge=1, le=100)]
    minimum_unit_coverage_bp: Rate
    max_alarm_increase_bp: Rate
    max_failure_increase_bp: Rate
    min_manual_saving_bp: Rate
    max_energy_increase_bp: Rate
    max_missing_energy_bp: Rate
    max_cycle_seconds: Annotated[StrictInt, Field(ge=60, le=172_800)]
    max_cycle_wh: Annotated[StrictInt, Field(ge=1, le=50_000)]


class TrialPlan(Input):
    title: ShortText
    program: Key
    starts_on: date
    ends_before: date
    policy: Policy
    markets: list[Market] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def trial_window(self):
        days = (self.ends_before - self.starts_on).days
        if not 1 <= days <= 180:
            raise ValueError("Trial must span between 1 and 180 local dates")
        if len({market.market_id for market in self.markets}) != len(self.markets):
            raise ValueError("Market identifiers must be unique")
        return self

    def market(self, market_id: str) -> Market | None:
        return next((item for item in self.markets if item.market_id == market_id), None)


class NewTrial(Input):
    trial_id: Key
    plan: TrialPlan


class ReviseTrial(Input):
    expected_version: Annotated[StrictInt, Field(ge=1)]
    plan: TrialPlan


class AdvancePhase(Input):
    expected_version: Annotated[StrictInt, Field(ge=1)]


class Site(Input):
    site_id: Key
    market_id: Key
    climate: Climate
    timezone: ShortText

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Expected an IANA time zone") from exc
        return value


class Enrollment(Input):
    unit_id: Key
    site_id: Key
    arm: Arm
    load: Load
    active_from: date
    active_until: date

    @model_validator(mode="after")
    def interval(self):
        if self.active_from >= self.active_until:
            raise ValueError("Enrollment end must follow enrollment start")
        return self


class Enrollments(Input):
    units: list[Enrollment] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_units(self):
        if len({unit.unit_id for unit in self.units}) != len(self.units):
            raise ValueError("Duplicate units within a single enrollment request")
        return self


def local_day(moment: datetime, site: Site) -> date:
    return moment.astimezone(ZoneInfo(site.timezone)).date()


def enrolled_at(unit: Enrollment, day: date) -> bool:
    return unit.active_from <= day < unit.active_until


def enrolled_days(unit: Enrollment, start: date, stop: date) -> int:
    overlap = min(unit.active_until, stop) - max(unit.active_from, start)
    return max(overlap, timedelta()).days
