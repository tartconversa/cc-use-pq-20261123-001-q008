from __future__ import annotations

from datetime import date
import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from fieldproof.analysis import evidence, influence, reconcile, sensitivity
from fieldproof.analysis.checks import inspection_queue, readiness_calendar
from fieldproof.analysis.contrast import estimate
from fieldproof.common import Problem, conflict, missing
from fieldproof.design import registry
from fieldproof.design.contracts import (
    AdvancePhase,
    Enrollment,
    Enrollments,
    Input,
    Key,
    NewTrial,
    ReviseTrial,
    Site,
)
from fieldproof.release import bulletins
from fieldproof.release.contracts import Cut, EvidenceId, FreezeEvidence, IssueBulletin, Review
from fieldproof.signals import ledger
from fieldproof.signals.contracts import Delivery
from fieldproof.storage import Store

QueryCut = Annotated[int, Query(ge=0, le=2**63 - 1)]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=1_000_000)]


class CloseTrial(AdvancePhase):
    expected_cut: Cut


class Selection:
    def __init__(
        self,
        cut: QueryCut | None = None,
        starts_on: date | None = None,
        ends_before: date | None = None,
    ):
        self.cut = cut
        self.starts_on = starts_on
        self.ends_before = ends_before

    def window(self, trial: registry.Trial):
        return evidence.select_window(trial, self.starts_on, self.ends_before)


Selected = Annotated[Selection, Depends(Selection)]


def create_app(database: str | Path) -> FastAPI:
    store = Store(database)
    app = FastAPI(title="FieldProof", version="1.0.0")

    @app.exception_handler(Problem)
    async def business_problem(request: Request, exc: Problem):
        return JSONResponse(status_code=exc.status, content=exc.payload())

    @app.get("/health")
    def health():
        with store.transaction() as db:
            db.execute("SELECT 1 FROM trial LIMIT 1").fetchone()
        return {"status": "ready"}

    @app.post("/trials")
    def create(body: NewTrial):
        with store.transaction(write=True) as db:
            return registry.create_trial(db, body.trial_id, body.plan).payload()

    @app.get("/trials")
    def list_trials(limit: Limit = 50, after: Key | None = None):
        with store.transaction() as db:
            items = registry.list_trials(db, limit + 1, after or "")
        return {
            "items": items[:limit],
            "next_after": items[limit - 1]["trial_id"] if len(items) > limit else None,
        }

    @app.get("/trials/{trial_id}")
    def read_trial(trial_id: Key):
        with store.transaction() as db:
            trial = registry.get_trial(db, trial_id)
            return {**trial.payload(), "current_cut": ledger.current_cut(db, trial_id)}

    @app.put("/trials/{trial_id}/plan")
    def revise(trial_id: Key, body: ReviseTrial):
        with store.transaction(write=True) as db:
            trial = registry.get_trial(db, trial_id)
            return registry.revise_plan(db, trial, body.plan, body.expected_version).payload()

    @app.post("/trials/{trial_id}/sites")
    def register(trial_id: Key, body: Site):
        with store.transaction(write=True) as db:
            trial = registry.get_trial(db, trial_id)
            return registry.register_site(db, trial, body)

    @app.post("/trials/{trial_id}/enrollment")
    def enroll(trial_id: Key, body: Enrollments):
        with store.transaction(write=True) as db:
            trial = registry.get_trial(db, trial_id)
            return registry.enroll(db, trial, body.units)

    @app.get("/trials/{trial_id}/roster")
    def read_roster(trial_id: Key):
        with store.transaction() as db:
            registry.get_trial(db, trial_id)
            assigned = registry.roster(db, trial_id)
            return {
                "sites": list(assigned.sites.values()),
                "units": list(assigned.units.values()),
                "allocation": assigned.split_counts(),
            }

    @app.post("/trials/{trial_id}/open")
    def start(trial_id: Key, body: AdvancePhase):
        with store.transaction(write=True) as db:
            trial = registry.get_trial(db, trial_id)
            return registry.open_trial(db, trial, body.expected_version).payload()

    @app.post("/trials/{trial_id}/close")
    def close(trial_id: Key, body: CloseTrial):
        with store.transaction(write=True) as db:
            trial = registry.get_trial(db, trial_id)
            cut = ledger.current_cut(db, trial_id)
            if cut != body.expected_cut:
                raise conflict("cut_changed", "New evidence arrived before closing",
                               details={"current_cut": cut})
            return registry.close_trial(db, trial, body.expected_version, cut).payload()

    @app.post("/trials/{trial_id}/deliveries")
    def deliver(trial_id: Key, body: Delivery):
        with store.transaction(write=True) as db:
            trial = registry.get_trial(db, trial_id)
            assigned = registry.roster(db, trial_id)
            return ledger.append(db, trial, assigned, body)

    @app.get("/trials/{trial_id}/events/{event_id}")
    def event_history(trial_id: Key, event_id: Key):
        with store.transaction() as db:
            registry.get_trial(db, trial_id)
            return {"revisions": ledger.history(db, trial_id, event_id)}

    @app.get("/trials/{trial_id}/preview")
    def preview(trial_id: Key, pick: Selected):
        with store.transaction() as db:
            trial = registry.get_trial(db, trial_id)
            return evidence.build(db, trial, pick.window(trial), pick.cut)

    @app.get("/trials/{trial_id}/units/{unit_id}/observations")
    def unit_observations(trial_id: Key, unit_id: Key, pick: Selected):
        with store.transaction() as db:
            trial = registry.get_trial(db, trial_id)
            material = evidence.load(db, trial, pick.window(trial), pick.cut)
            return evidence.unit_record(material, trial, unit_id)

    @app.get("/trials/{trial_id}/reconciliation")
    def changes(
        trial_id: Key,
        before_cut: QueryCut,
        after_cut: QueryCut,
        starts_on: date | None = None,
        ends_before: date | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ):
        with store.transaction() as db:
            trial = registry.get_trial(db, trial_id)
            window = evidence.select_window(trial, starts_on, ends_before)
            return reconcile.reconcile(db, trial, window, before_cut, after_cut,
                                       limit=limit, offset=offset)

    @app.get("/trials/{trial_id}/markets/{market}/inspection")
    def inspect(
        trial_id: Key,
        market: Key,
        pick: Selected,
        limit: Limit = 50,
        offset: Offset = 0,
        site: Key | None = None,
    ):
        with store.transaction() as db:
            trial = registry.get_trial(db, trial_id)
            material = evidence.load(db, trial, pick.window(trial), pick.cut)
            return inspection_queue(trial, material, market, site, offset, limit)

    @app.get("/trials/{trial_id}/markets/{market}/coverage-calendar")
    def calendar(trial_id: Key, market: Key, pick: Selected):
        with store.transaction() as db:
            trial = registry.get_trial(db, trial_id)
            material = evidence.load(db, trial, pick.window(trial), pick.cut)
            return readiness_calendar(db, trial, material, market)

    @app.get("/trials/{trial_id}/markets/{market}/sensitivity")
    def sensitivity_readout(trial_id: Key, market: Key, pick: Selected):
        with store.transaction() as db:
            trial = registry.get_trial(db, trial_id)
            material = evidence.load(db, trial, pick.window(trial), pick.cut)
            for result in estimate(trial, material.units):
                if result.market.market_id == market:
                    return {
                        "cut": material.cut,
                        "window": material.window.payload(),
                        **sensitivity.range_readout(result, trial.plan.policy),
                    }
            raise missing("market", market)

    @app.get("/trials/{trial_id}/markets/{market}/site-influence")
    def site_influence(trial_id: Key, market: Key, pick: Selected):
        with store.transaction() as db:
            trial = registry.get_trial(db, trial_id)
            material = evidence.load(db, trial, pick.window(trial), pick.cut)
            return influence.site_readout(trial, material, market)

    @app.post("/trials/{trial_id}/evidence")
    def freeze(trial_id: Key, body: FreezeEvidence):
        with store.transaction(write=True) as db:
            trial = registry.get_trial(db, trial_id)
            return evidence.freeze(db, trial, body.expected_cut)

    @app.get("/trials/{trial_id}/evidence/{evidence_id}")
    def read_evidence(trial_id: Key, evidence_id: EvidenceId):
        with store.transaction() as db:
            registry.get_trial(db, trial_id)
            return evidence.get(db, trial_id, evidence_id)

    @app.get("/trials/{trial_id}/evidence/{evidence_id}/metrics.csv")
    def export_metrics(trial_id: Key, evidence_id: EvidenceId):
        with store.transaction() as db:
            document = evidence.get(db, trial_id, evidence_id)
        return Response(
            content=reconcile.csv_metrics(document),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{evidence_id}.csv"'},
        )

    @app.post("/trials/{trial_id}/evidence/{evidence_id}/markets/{market}/reviews")
    def review(trial_id: Key, evidence_id: EvidenceId, market: Key, body: Review):
        with store.transaction(write=True) as db:
            trial = registry.get_trial(db, trial_id)
            return bulletins.record_review(db, trial, evidence_id, market, body)

    @app.get("/trials/{trial_id}/evidence/{evidence_id}/markets/{market}/reviews")
    def reviews(trial_id: Key, evidence_id: EvidenceId, market: Key):
        with store.transaction() as db:
            document = evidence.get(db, trial_id, evidence_id)
            evidence.get_market(document, market)
            return {"reviews": bulletins.list_reviews(db, trial_id, evidence_id, market)}

    @app.post("/trials/{trial_id}/markets/{market}/bulletins")
    def issue(trial_id: Key, market: Key, body: IssueBulletin):
        with store.transaction(write=True) as db:
            trial = registry.get_trial(db, trial_id)
            return bulletins.issue(db, trial, market, body)

    @app.get("/trials/{trial_id}/markets/{market}/bulletins/latest")
    def latest_bulletin(trial_id: Key, market: Key):
        with store.transaction() as db:
            trial = registry.get_trial(db, trial_id)
            return bulletins.read(db, trial, market)

    @app.get("/trials/{trial_id}/markets/{market}/bulletins/{edition}")
    def historical_bulletin(trial_id: Key, market: Key, edition: int):
        if not 1 <= edition < 2**63:
            raise missing("bulletin", f"{market}/{edition}")
        with store.transaction() as db:
            trial = registry.get_trial(db, trial_id)
            return bulletins.read(db, trial, market, edition)

    return app


def from_environment() -> FastAPI:
    return create_app(os.environ.get("FIELDPROOF_DB", "fieldproof.sqlite3"))
