from concurrent.futures import ThreadPoolExecutor
import csv
from hashlib import sha256
from io import StringIO

from tests.support import ApiCase, MAIN, ROOT
from fieldproof.common import encoded


class ReleaseFlowTest(ApiCase):
    def test_meter_correction_changes_gates_closure_and_published_decision(self):
        opened = self.setup_trial()
        signal = self.green_events()
        first = self.deliver("baseline", signal)["current_cut"]
        original = self.draft()
        self.assertEqual(original["market_results"][0]["recommendation"], "advance")
        meter = next(item for item in signal if item["event_id"] == "a-one-a-m1")
        correction = {**meter, "revision": 2, "meter_wh": 180}
        later = self.deliver("meter-correction", [correction])["current_cut"]
        rejects_old_cut = self.call("POST", ROOT + "/close", {
            "expected_version": opened["version"], "expected_cut": first,
        }, status=409)
        self.assertEqual(rejects_old_cut["details"]["current_cut"], later)
        self.assertEqual(self.draft(cut=first), original)
        changed = self.call("GET", ROOT + "/reconciliation",
                            params={"before_cut": first, "after_cut": later})
        result = changed["markets"][0]
        self.assertEqual(result["before_recommendation"], "advance")
        self.assertEqual(result["after_recommendation"], "hold")
        self.assertEqual(result["gates"]["energy_change_bp"]["after"]["observed"], "2500.000")
        self.assertEqual(changed["cycles"][0]["unit_id"], "a-one")
        self.assertEqual(changed["cycles"][0]["after_revisions"][meter["event_id"]], 2)
        self.assertEqual(changed["affected_cells"], [{
            "market_id": "alpine", "cell": "cold/light", "changed_cycles": 1,
        }])
        closed = self.close(opened["version"], later)
        self.assertEqual(closed["close_cut"], later)
        self.call("POST", ROOT + "/evidence", {"expected_cut": first}, status=409)
        frozen = self.freeze(later)
        evidence_id = frozen["evidence_id"]
        self.review(evidence_id, "field", "ada")
        self.review(evidence_id, "methods", "bea")
        failure = self.issue(evidence_id, status=409)
        self.assertEqual(failure["details"], {"permitted_outcome": "hold", "reason": "hold"})
        bulletin = self.issue(evidence_id, outcome="hold")
        self.assertEqual(bulletin["result"], frozen["market_results"][0])
        self.assertEqual(self.call("GET", MAIN + "/bulletins/latest"), bulletin)
        self.assertEqual(self.call("GET", MAIN + "/bulletins/1"), bulletin)
        self.assertEqual(bulletin, self.issue(evidence_id, outcome="hold"))
        self.assertEqual(self.deliver("baseline", signal)["current_cut"], later)
        self.deliver("fresh-after-close", [{**correction, "revision": 3}], status=409)

    def test_second_review_seals_exact_evidence_under_concurrent_retry(self):
        opened = self.setup_trial()
        cut = self.deliver("good", self.green_events())["current_cut"]
        self.close(opened["version"], cut)
        frozen = self.freeze(cut)
        evidence_id = frozen["evidence_id"]
        canonical = {key: value for key, value in frozen.items() if key != "evidence_id"}
        self.assertEqual(evidence_id, sha256(encoded(canonical).encode()).hexdigest())
        self.review(evidence_id, "field", "ida")
        duplicate_person = self.review(evidence_id, "methods", "ida", status=409)
        self.assertEqual(duplicate_person["code"], "reviewer_not_independent")
        missing = self.issue(evidence_id, status=409)
        self.assertEqual(missing["code"], "endorsements_missing")
        self.review(evidence_id, "methods", "marc")
        with ThreadPoolExecutor(max_workers=2) as pool:
            candidates = list(pool.map(lambda _: self.issue(evidence_id), range(2)))
        self.assertEqual(candidates[0], candidates[1])
        self.assertEqual(candidates[0]["edition"], 1)
        values = {key: value for key, value in candidates[0].items() if key != "bulletin_digest"}
        self.assertEqual(candidates[0]["bulletin_digest"], sha256(encoded(values).encode()).hexdigest())
        self.assertEqual(self.issue(evidence_id, note="Different memo", status=409)["code"],
                         "bulletin_exists")
        self.assertEqual(self.freeze(cut), frozen)

    def test_objection_can_hold_a_market_with_passing_numbers(self):
        opened = self.setup_trial()
        cut = self.deliver("good", self.green_events())["current_cut"]
        self.close(opened["version"], cut)
        frozen = self.freeze(cut)
        self.assertEqual(frozen["market_results"][0]["recommendation"], "advance")
        evidence_id = frozen["evidence_id"]
        self.review(evidence_id, "field", "ida", assessment="object")
        bulletin = self.issue(evidence_id, outcome="hold")
        self.assertEqual(bulletin["decision_reason"], "review_objection")
        failure = self.review(evidence_id, "methods", "marc", status=409)
        self.assertEqual(failure["code"], "review_closed")

    def test_frozen_csv_keeps_missing_measurements_empty_and_links_cut(self):
        opened = self.setup_trial()
        self.close(opened["version"], 0)
        frozen = self.freeze(0)
        evidence_id = frozen["evidence_id"]
        response = self.client.get(ROOT + f"/evidence/{evidence_id}/metrics.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn(evidence_id, response.headers["content-disposition"])
        rows = list(csv.DictReader(StringIO(response.text)))
        self.assertEqual(len(rows), 10)
        manual = next(row for row in rows if row["metric"] == "manual" and not row["cell"])
        self.assertEqual(manual["assisted"], "")
        self.assertEqual(manual["evidence_id"], evidence_id)
        self.assertEqual(manual["cut"], "0")
