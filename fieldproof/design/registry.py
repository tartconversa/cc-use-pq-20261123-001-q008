from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

from fieldproof.common import conflict, encoded, invalid, missing
from fieldproof.design.contracts import Enrollment, Site, TrialPlan


@dataclass(frozen=True)
class Trial:
    trial_id: str
    plan: TrialPlan
    version: int
    phase: str
    close_cut: int | None

    def payload(self) -> dict:
        return {
            "trial_id": self.trial_id,
            "plan": self.plan.model_dump(mode="json"),
            "version": self.version,
            "phase": self.phase,
            "close_cut": self.close_cut,
        }


@dataclass(frozen=True)
class Roster:
    sites: dict[str, Site]
    units: dict[str, Enrollment]

    def placement(self, unit: Enrollment) -> tuple[str, str]:
        site = self.sites[unit.site_id]
        return site.market_id, f"{site.climate}/{unit.load}"

    def split_counts(self) -> dict:
        counts: dict[str, dict] = {}
        for unit in self.units.values():
            market, cell = self.placement(unit)
            arms = counts.setdefault(market, {}).setdefault(
                cell, {"standard": 0, "assisted": 0}
            )
            arms[unit.arm] += 1
        return counts


def get_trial(db: Connection, trial_id: str) -> Trial:
    row = db.execute("SELECT * FROM trial WHERE trial_id=?", (trial_id,)).fetchone()
    if row is None:
        raise missing("trial", trial_id)
    return Trial(
        trial_id=trial_id,
        plan=TrialPlan.model_validate_json(row["design"]),
        version=row["version"],
        phase=row["phase"],
        close_cut=row["close_cut"],
    )


def list_trials(db: Connection, limit: int, after: str) -> list[dict]:
    rows = db.execute(
        """SELECT trial_id, design, version, phase, close_cut FROM trial
           WHERE trial_id>? ORDER BY trial_id LIMIT ?""",
        (after, limit),
    )
    return [
        {
            "trial_id": row["trial_id"],
            "title": TrialPlan.model_validate_json(row["design"]).title,
            "phase": row["phase"],
            "version": row["version"],
            "close_cut": row["close_cut"],
        }
        for row in rows
    ]


def create_trial(db: Connection, trial_id: str, plan: TrialPlan) -> Trial:
    canonical = encoded(plan.model_dump(mode="json"))
    row = db.execute(
        "SELECT design FROM trial WHERE trial_id=?", (trial_id,)
    ).fetchone()
    if row is not None:
        if row["design"] == canonical:
            return get_trial(db, trial_id)
        raise conflict("trial_exists", "This trial code already has a different plan")
    db.execute(
        "INSERT INTO trial VALUES (?,?,1,'draft',NULL)", (trial_id, canonical)
    )
    return get_trial(db, trial_id)


def roster(db: Connection, trial_id: str) -> Roster:
    sites = db.execute(
        "SELECT site_id, data FROM site WHERE trial_id=? ORDER BY site_id",
        (trial_id,),
    )
    units = db.execute(
        "SELECT unit_id, data FROM enrollment WHERE trial_id=? ORDER BY unit_id",
        (trial_id,),
    )
    return Roster(
        {row["site_id"]: Site.model_validate_json(row["data"]) for row in sites},
        {row["unit_id"]: Enrollment.model_validate_json(row["data"]) for row in units},
    )


def check_version(trial: Trial, expected_version: int):
    if trial.version != expected_version:
        raise conflict(
            "trial_changed",
            "Read the current trial before changing it",
            details={"current_version": trial.version},
        )


def require_phase(trial: Trial, phase: str):
    if trial.phase != phase:
        raise conflict(
            "trial_phase",
            f"Operation requires a {phase} trial",
            details={"phase": trial.phase},
        )


def validate_site(plan: TrialPlan, site: Site):
    market = plan.market(site.market_id)
    if market is None:
        raise invalid("unplanned_market", f"Market {site.market_id} is outside the plan")
    if not any(cell.climate == site.climate for cell in market.cells):
        raise invalid("unplanned_climate", f"Site {site.site_id} has no planned cells")


def validate_unit(plan: TrialPlan, sites: dict[str, Site], unit: Enrollment):
    site = sites.get(unit.site_id)
    if site is None:
        raise missing("site", unit.site_id)
    market = plan.market(site.market_id)
    if market is None or not any(
        cell.climate == site.climate and cell.load == unit.load for cell in market.cells
    ):
        raise invalid("unplanned_cell", f"Unit {unit.unit_id} has no planned cell")
    if unit.active_from < plan.starts_on or unit.active_until > plan.ends_before:
        raise invalid("enrollment_window", f"Unit {unit.unit_id} falls outside the trial")


def revise_plan(db: Connection, trial: Trial, plan: TrialPlan, expected: int) -> Trial:
    check_version(trial, expected)
    require_phase(trial, "draft")
    existing = roster(db, trial.trial_id)
    for site in existing.sites.values():
        validate_site(plan, site)
    for unit in existing.units.values():
        validate_unit(plan, existing.sites, unit)
    db.execute(
        "UPDATE trial SET design=?, version=version+1 WHERE trial_id=?",
        (encoded(plan.model_dump(mode="json")), trial.trial_id),
    )
    return get_trial(db, trial.trial_id)


def register_site(db: Connection, trial: Trial, site: Site) -> Site:
    require_phase(trial, "draft")
    validate_site(trial.plan, site)
    row = db.execute(
        "SELECT data FROM site WHERE trial_id=? AND site_id=?",
        (trial.trial_id, site.site_id),
    ).fetchone()
    canonical = encoded(site.model_dump(mode="json"))
    if row is not None:
        if row["data"] != canonical:
            raise conflict("site_exists", "Site code already has a different placement")
        return site
    db.execute(
        "INSERT INTO site VALUES (?,?,?)", (trial.trial_id, site.site_id, canonical)
    )
    db.execute(
        "UPDATE trial SET version=version+1 WHERE trial_id=?", (trial.trial_id,)
    )
    return site


def enroll(db: Connection, trial: Trial, units: list[Enrollment]) -> dict:
    require_phase(trial, "draft")
    current = roster(db, trial.trial_id)
    new = []
    for unit in units:
        validate_unit(trial.plan, current.sites, unit)
        previous = current.units.get(unit.unit_id)
        if previous is None:
            new.append(unit)
        elif previous != unit:
            raise conflict("unit_exists", f"Unit {unit.unit_id} has a different assignment")
    if new:
        db.executemany(
            "INSERT INTO enrollment VALUES (?,?,?,?,?)",
            (
                (
                    trial.trial_id,
                    unit.unit_id,
                    unit.site_id,
                    unit.arm,
                    encoded(unit.model_dump(mode="json")),
                )
                for unit in new
            ),
        )
        db.execute(
            "UPDATE trial SET version=version+1 WHERE trial_id=?", (trial.trial_id,)
        )
    return {
        "added": len(new),
        "already_enrolled": len(units) - len(new),
        "version": get_trial(db, trial.trial_id).version,
    }


def open_trial(db: Connection, trial: Trial, expected: int) -> Trial:
    check_version(trial, expected)
    require_phase(trial, "draft")
    counts = roster(db, trial.trial_id).split_counts()
    gaps = []
    minimum = trial.plan.policy.required_units_per_arm
    for market in trial.plan.markets:
        for cell in market.cells:
            sizes = counts.get(market.market_id, {}).get(cell.key, {})
            for arm in ("standard", "assisted"):
                count = sizes.get(arm, 0)
                if count < minimum:
                    gaps.append(
                        {
                            "market": market.market_id,
                            "cell": cell.key,
                            "arm": arm,
                            "enrolled": count,
                            "required": minimum,
                        }
                    )
    if gaps:
        raise conflict("allocation_shortfall", "Planned arms are understaffed", details=gaps)
    db.execute(
        "UPDATE trial SET phase='open', version=version+1 WHERE trial_id=?",
        (trial.trial_id,),
    )
    return get_trial(db, trial.trial_id)


def close_trial(db: Connection, trial: Trial, expected: int, cut: int) -> Trial:
    check_version(trial, expected)
    require_phase(trial, "open")
    db.execute(
        """UPDATE trial SET phase='closed', close_cut=?, version=version+1
           WHERE trial_id=?""",
        (cut, trial.trial_id),
    )
    return get_trial(db, trial.trial_id)
