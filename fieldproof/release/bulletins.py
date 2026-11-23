from __future__ import annotations

import json
from sqlite3 import Connection

from fieldproof.analysis import evidence
from fieldproof.common import conflict, encoded, fingerprint, missing, utc_now
from fieldproof.design.registry import Trial, require_phase
from fieldproof.release.contracts import IssueBulletin, Review


def list_reviews(db: Connection, trial_id: str, evidence_id: str, market: str) -> list[dict]:
    rows = db.execute(
        """SELECT desk,assessment,reviewer,note,recorded_at FROM review
           WHERE trial_id=? AND evidence_id=? AND market=? ORDER BY desk""",
        (trial_id, evidence_id, market),
    )
    return [dict(row) for row in rows]


def record_review(
    db: Connection, trial: Trial, evidence_id: str, market: str, submitted: Review
) -> dict:
    require_phase(trial, "closed")
    current = evidence.get(db, trial.trial_id, evidence_id)
    evidence.get_market(current, market)
    existing = list_reviews(db, trial.trial_id, evidence_id, market)
    document = submitted.model_dump()
    for old in existing:
        if old["desk"] == submitted.desk:
            if all(old[key] == value for key, value in document.items()):
                return old
            raise conflict("desk_already_recorded", f"{submitted.desk} has already recorded a decision")
        if old["reviewer"] == submitted.reviewer:
            raise conflict("reviewer_not_independent", "Each desk requires a different reviewer")
    if by_evidence(db, trial.trial_id, market, evidence_id) is not None:
        raise conflict("review_closed", "The market bulletin already sealed its reviews")
    entry = {**document, "recorded_at": utc_now()}
    db.execute(
        "INSERT INTO review VALUES (?,?,?,?,?,?,?,?)",
        (
            trial.trial_id, evidence_id, market, entry["desk"], entry["assessment"],
            entry["reviewer"], entry["note"], entry["recorded_at"],
        ),
    )
    return entry


def by_evidence(db: Connection, trial_id: str, market: str, evidence_id: str):
    return db.execute(
        """SELECT digest,document FROM bulletin
           WHERE trial_id=? AND market=? AND evidence_id=?""",
        (trial_id, market, evidence_id),
    ).fetchone()


def current_edition(db: Connection, trial_id: str, market: str) -> int:
    return db.execute(
        "SELECT COALESCE(MAX(edition),0) FROM bulletin WHERE trial_id=? AND market=?",
        (trial_id, market),
    ).fetchone()[0]


def permitted_outcome(market_result: dict, reviews: list[dict]) -> tuple[str, str]:
    if any(item["assessment"] == "object" for item in reviews):
        return "hold", "review_objection"
    if market_result["recommendation"] != "advance":
        return "hold", market_result["recommendation"]
    endorsers = {item["desk"] for item in reviews if item["assessment"] == "endorse"}
    if endorsers != {"field", "methods"}:
        raise conflict("endorsements_missing", "Field and methods desks must independently endorse")
    return "advance", "thresholds_and_reviews"


def issue(
    db: Connection,
    trial: Trial,
    market: str,
    request: IssueBulletin,
) -> dict:
    require_phase(trial, "closed")
    request_digest = fingerprint(request.model_dump())
    existing = by_evidence(db, trial.trial_id, market, request.evidence_id)
    if existing is not None:
        if existing["digest"] != request_digest:
            raise conflict("bulletin_exists", "The evidence already has a market bulletin")
        return json.loads(existing["document"])
    edition = current_edition(db, trial.trial_id, market)
    if edition != request.expected_edition:
        raise conflict(
            "edition_changed",
            "Read the current market bulletin first",
            details={"current_edition": edition},
        )
    frozen = evidence.get(db, trial.trial_id, request.evidence_id)
    result = evidence.get_market(frozen, market)
    if frozen["cut"] != trial.close_cut:
        raise conflict("stale_evidence", "Only the closed trial cut can be issued")
    reviews = list_reviews(db, trial.trial_id, request.evidence_id, market)
    outcome, reason = permitted_outcome(result, reviews)
    if request.outcome != outcome:
        raise conflict("outcome_mismatch", "Requested outcome conflicts with evidence or reviews",
                       details={"permitted_outcome": outcome, "reason": reason})
    document = {
        "trial_id": trial.trial_id,
        "market_id": market,
        "edition": edition + 1,
        "outcome": outcome,
        "decision_reason": reason,
        "evidence_id": request.evidence_id,
        "cut": frozen["cut"],
        "window": frozen["window"],
        "result": result,
        "reviews": reviews,
        "note": request.note,
        "issued_at": utc_now(),
    }
    document["bulletin_digest"] = fingerprint(document)
    db.execute(
        "INSERT INTO bulletin VALUES (?,?,?,?,?,?,?)",
        (
            trial.trial_id, market, edition + 1, request.evidence_id, outcome,
            request_digest, encoded(document),
        ),
    )
    return document


def read(
    db: Connection, trial: Trial, market: str, edition: int | None = None
) -> dict:
    if trial.plan.market(market) is None:
        raise missing("market", market)
    if edition is None:
        edition = current_edition(db, trial.trial_id, market)
    row = db.execute(
        "SELECT document FROM bulletin WHERE trial_id=? AND market=? AND edition=?",
        (trial.trial_id, market, edition),
    ).fetchone()
    if row is None:
        raise missing("bulletin", f"{market}/{edition}")
    return json.loads(row["document"])
