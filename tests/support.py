from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from fieldproof.api import create_app

ROOT = "/trials/valley"
MAIN = ROOT + "/markets/alpine"


def plan(*, second_cell=False, required_cycles=1):
    cells = [{"climate": "cold", "load": "light", "weight_bp": 7_000 if second_cell else 10_000}]
    if second_cell:
        cells.append({"climate": "cold", "load": "heavy", "weight_bp": 3_000})
    return {
        "title": "Laundry assist pilot",
        "program": "laundry",
        "starts_on": "2026-10-24",
        "ends_before": "2026-10-29",
        "markets": [{"market_id": "alpine", "label": "Alpine", "cells": cells}],
        "policy": {
            "required_units_per_arm": 2,
            "required_cycles_per_unit": required_cycles,
            "minimum_unit_coverage_bp": 8_000,
            "max_alarm_increase_bp": 0,
            "max_failure_increase_bp": 0,
            "min_manual_saving_bp": 1_000,
            "max_energy_increase_bp": 1_000,
            "max_missing_energy_bp": 0,
            "max_cycle_seconds": 7_200,
            "max_cycle_wh": 2_000,
        },
    }


def site(site_id="north", timezone="Europe/Berlin"):
    return {"site_id": site_id, "market_id": "alpine", "climate": "cold", "timezone": timezone}


def enrollment(unit, arm, *, load="light", at="north", start="2026-10-24", end="2026-10-29"):
    return {
        "unit_id": unit, "site_id": at, "arm": arm, "load": load,
        "active_from": start, "active_until": end,
    }


def event(unit, cycle, label, kind, at, **fields):
    return {
        "event_id": f"{unit}-{cycle}-{label}", "revision": 1,
        "unit_id": unit, "cycle_id": cycle, "kind": kind, "occurred_at": at, **fields,
    }


def observations(
    unit: str, cycle: str = "a", *,
    start: str = "2026-10-25T09:00:00+00:00",
    manual=False,
    alarm=False,
    abort=False,
    wh: int | None = 100,
    seconds: int = 600,
):
    moment = datetime.fromisoformat(start)
    middle = (moment + timedelta(seconds=seconds // 2)).isoformat()
    close = (moment + timedelta(seconds=seconds)).isoformat()
    result = [event(unit, cycle, "start", "start", start)]
    if manual:
        result.append(event(unit, cycle, "manual", "manual", middle))
    if alarm:
        result.append(event(unit, cycle, "alarm", "alarm", middle, reason="overheat"))
    if wh is not None:
        result.extend([
            event(unit, cycle, "m0", "meter", start, meter_id="main", meter_wh=20),
            event(unit, cycle, "m1", "meter", close, meter_id="main", meter_wh=20 + wh),
        ])
    if abort:
        result.append(event(unit, cycle, "end", "abort", close, reason="jam"))
    else:
        result.append(event(unit, cycle, "end", "complete", close))
    return result


class ApiCase(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "pilot.sqlite3"
        self.client = TestClient(create_app(self.db))
        self.addCleanup(self.client.close)

    def call(self, method, path, data=None, *, status=200, params=None):
        response = self.client.request(method, path, json=data, params=params)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def create(self, trial_plan=None):
        return self.call("POST", "/trials", {"trial_id": "valley", "plan": trial_plan or plan()})

    def register(self, details=None):
        return self.call("POST", ROOT + "/sites", details or site())

    def units(self, units=None):
        if units is None:
            units = [
                enrollment("s-one", "standard"), enrollment("s-two", "standard"),
                enrollment("a-one", "assisted"), enrollment("a-two", "assisted"),
            ]
        return self.call("POST", ROOT + "/enrollment", {"units": units})

    def setup_trial(self, trial_plan=None, *, units=None):
        self.create(trial_plan)
        self.register()
        return self.open(self.units(units)["version"])

    def open(self, version):
        return self.call("POST", ROOT + "/open", {"expected_version": version})

    def close(self, version, cut):
        return self.call("POST", ROOT + "/close", {
            "expected_version": version, "expected_cut": cut,
        })

    def deliver(self, delivery_id, events, *, status=200):
        return self.call("POST", ROOT + "/deliveries", {
            "delivery_id": delivery_id, "events": events,
        }, status=status)

    def draft(self, **params):
        return self.call("GET", ROOT + "/preview", params=params)

    def green_events(self, *, assisted_wh=90):
        return (
            observations("s-one", manual=True)
            + observations("s-two", manual=True)
            + observations("a-one", wh=assisted_wh)
            + observations("a-two", wh=assisted_wh)
        )

    def freeze(self, cut):
        return self.call("POST", ROOT + "/evidence", {"expected_cut": cut})

    def review(self, evidence_id, desk, reviewer, *, assessment="endorse", status=200):
        return self.call("POST", ROOT + f"/evidence/{evidence_id}/markets/alpine/reviews", {
            "desk": desk, "assessment": assessment, "reviewer": reviewer, "note": "Field review",
        }, status=status)

    def issue(self, evidence_id, *, outcome="advance", edition=0, status=200, note="Pilot disposition"):
        return self.call("POST", MAIN + "/bulletins", {
            "evidence_id": evidence_id, "expected_edition": edition,
            "outcome": outcome, "note": note,
        }, status=status)
