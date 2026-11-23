from concurrent.futures import ThreadPoolExecutor

from tests.support import ApiCase, ROOT, MAIN, event, observations


class SignalHistoryTest(ApiCase):
    def setUp(self):
        super().setUp()
        self.setup_trial()

    def unit(self, unit="a-one", **params):
        return self.call("GET", ROOT + f"/units/{unit}/observations", params=params)

    def test_repeated_shipment_is_order_and_zone_independent(self):
        rows = observations("a-one")
        first = self.deliver("batch", rows)
        local_start = {**rows[0], "occurred_at": "2026-10-25T10:00:00+01:00"}
        again = self.deliver("batch", [*reversed(rows[1:]), local_start])
        repack = self.deliver("repack", rows)
        self.assertTrue(again["replayed"])
        self.assertEqual((repack["accepted"], repack["repeated"]), (0, len(rows)))
        self.assertEqual(again["current_cut"], first["current_cut"])
        self.assertEqual(repack["current_cut"], first["current_cut"])
        self.call("GET", ROOT + "/preview", params={"cut": repack["delivery_sequence"]}, status=404)

    def test_conflicting_revision_rolls_back_even_preceding_new_rows(self):
        rows = observations("s-one")
        first = self.deliver("original", rows)
        broken = {**rows[0], "occurred_at": "2026-10-26T10:00:00+00:00"}
        rejected = self.deliver("correction", [*observations("a-one"), broken], status=409)
        self.assertEqual(rejected["code"], "revision_changed")
        self.assertEqual(self.unit()["cycles"], [])
        self.assertEqual(self.call("GET", ROOT)["current_cut"], first["current_cut"])
        second = self.deliver("correction", [{**broken, "revision": 2}])
        self.assertGreater(second["current_cut"], first["current_cut"])

    def test_revisions_in_same_delivery_and_retractions_are_as_of(self):
        rows = observations("a-one", alarm=True)
        first = self.deliver("original", rows)
        alarm = rows[1]
        withdrawn = {**alarm, "revision": 2, "void": True, "reason": None}
        third = {**alarm, "revision": 3, "reason": "sensor-fault"}
        second = self.deliver("retraction", [withdrawn])
        later = self.deliver("replacement", [third])
        self.assertTrue(self.unit(cut=first["current_cut"])["cycles"][0]["alarm"])
        self.assertFalse(self.unit(cut=second["current_cut"])["cycles"][0]["alarm"])
        self.assertTrue(self.unit(cut=later["current_cut"])["cycles"][0]["alarm"])
        revisions = self.call("GET", ROOT + f"/events/{alarm['event_id']}")["revisions"]
        self.assertEqual([row["event"]["revision"] for row in revisions], [1, 2, 3])
        tail = [{**third, "revision": 5}, {**third, "revision": 4}]
        self.assertEqual(self.deliver("multiple-revisions", tail)["accepted"], 2)

    def test_missing_predecessor_and_reassignment_are_distinct(self):
        row = observations("a-one")[0]
        failure = self.deliver("first", [{**row, "revision": 2}], status=409)
        self.assertEqual(failure["details"]["expected_revision"], 1)
        self.deliver("first", [row])
        failure = self.deliver("second", [{**row, "revision": 2, "cycle_id": "other"}], status=409)
        self.assertEqual(failure["code"], "revision_reassigned")

    def test_boundary_uses_site_date_and_credits_cross_midnight_to_start(self):
        too_early = observations("a-one", start="2026-10-23T21:59:59Z")
        failure = self.deliver("early", too_early, status=422)
        self.assertEqual(failure["details"]["local_date"], "2026-10-23")
        accepted = observations("a-one", start="2026-10-28T22:59:00Z")
        self.deliver("last", accepted)
        included = self.unit(starts_on="2026-10-28", ends_before="2026-10-29")
        omitted = self.unit(starts_on="2026-10-27", ends_before="2026-10-28")
        self.assertEqual(included["cycles"][0]["local_date"], "2026-10-28")
        self.assertEqual(included["cycles"][0]["state"], "complete")
        self.assertEqual(omitted["cycles"], [])

    def test_overlap_marks_both_and_does_not_count_a_contiguous_third(self):
        rows = observations("a-one", "one")
        rows += observations("a-one", "two", start="2026-10-25T09:04:00Z")
        rows += observations("a-one", "three", start="2026-10-25T09:14:00Z")
        self.deliver("overlap", rows)
        cycles = {row["cycle_id"]: row for row in self.unit()["cycles"]}
        for key in ("one", "two"):
            self.assertEqual(cycles[key]["issues"], ["overlapping_cycle"])
            self.assertIsNone(cycles[key]["energy_wh"])
        self.assertEqual(cycles["three"]["state"], "complete")
        self.assertEqual(self.unit()["unit"]["usable_cycles"], 1)

    def test_conflicting_meter_and_backward_counter_are_separate(self):
        first = observations("a-one", "first")
        first.append(event("a-one", "first", "m2", "meter", first[-1]["occurred_at"],
                           meter_wh=201, meter_id="main"))
        second = observations("a-two", "second")
        second.append(event("a-two", "second", "m2", "meter", "2026-10-25T09:05:00Z",
                            meter_wh=300, meter_id="main"))
        self.deliver("meters", first + second)
        self.assertEqual(self.unit()["cycles"][0]["energy_issue"], "conflicting_readings")
        self.assertEqual(self.unit("a-two")["cycles"][0]["energy_issue"], "counter_decreased")
        queue = self.call("GET", MAIN + "/inspection", params={"limit": 1})
        self.assertEqual(queue["counts"]["energy"], 2)
        self.assertEqual(queue["next_offset"], 1)
        self.assertEqual(queue["items"][0]["category"], "energy")

    def test_missing_start_alarm_is_prioritized_without_manufacturing_a_rate(self):
        row = observations("a-one", alarm=True)[1]
        self.deliver("partial", [row, observations("a-two")[0]])
        found = self.call("GET", MAIN + "/inspection")
        self.assertEqual([item["category"] for item in found["items"]],
                         ["unresolved_alarm", "pending"])
        result = self.draft()["market_results"][0]
        self.assertEqual(result["integrity"]["unresolved_with_alarm"], 1)
        self.assertEqual(result["recommendation"], "insufficient")
        self.assertIsNone(result["metrics"]["alarm"]["assisted"])

    def test_concurrent_delivery_retry_commits_once(self):
        rows = observations("a-one")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.deliver("same-delivery", rows), range(2)))
        self.assertEqual(sorted(item["replayed"] for item in results), [False, True])
        self.assertEqual(results[0]["current_cut"], results[1]["current_cut"])
        self.assertEqual(len(self.call("GET", ROOT + f"/events/{rows[0]['event_id']}")["revisions"]), 1)
