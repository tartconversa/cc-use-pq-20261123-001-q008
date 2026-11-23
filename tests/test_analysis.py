from tests.support import ApiCase, MAIN, ROOT, enrollment, observations, plan, site


class ComparisonTest(ApiCase):
    def test_device_rates_are_equally_weighted_after_cycle_counts_differ(self):
        self.setup_trial()
        events = observations("s-one", manual=True) + observations("s-two")
        for hour in (10, 11, 12):
            events += observations("s-one", f"later-{hour}",
                                   start=f"2026-10-25T{hour}:00:00Z", manual=True)
        events += observations("a-one", wh=80) + observations("a-two", wh=120)
        self.deliver("weighted", events)
        result = self.draft()["market_results"][0]
        self.assertEqual(result["metrics"]["manual"]["standard"], "5000.000")
        self.assertEqual(result["metrics"]["manual"]["assisted"], "0.000")
        self.assertEqual(result["metrics"]["energy_wh"]["relative_change_bp"], "0.000")
        self.assertEqual(result["cells"][0]["arms"]["standard"]["eligible_observed_cycles"], 5)

    def test_fixed_mix_can_differ_from_the_pooled_cycle_direction(self):
        self.create(plan(second_cell=True))
        self.register()
        participants = [
            enrollment(f"{prefix}-{cell}-{number}", arm, load=cell)
            for prefix, arm in (("s", "standard"), ("a", "assisted"))
            for cell in ("light", "heavy")
            for number in range(2)
        ]
        self.open(self.units(participants)["version"])
        rows = []
        for item in participants:
            light = item["load"] == "light"
            standard = item["arm"] == "standard"
            rows += observations(item["unit_id"], manual=(light and standard) or (not light and not standard))
        self.deliver("two-cells", rows)
        report = self.draft()["market_results"][0]
        self.assertEqual(report["metrics"]["manual"]["assisted_minus_standard"], "-4000.000")
        self.assertEqual(report["cells"][1]["metrics"]["manual"]["assisted_minus_standard"], "10000.000")

    def test_missing_cell_is_not_replaced_by_supported_cell(self):
        self.create(plan(second_cell=True))
        self.register()
        participants = [
            enrollment(f"{prefix}-{cell}-{number}", arm, load=cell)
            for prefix, arm in (("s", "standard"), ("a", "assisted"))
            for cell in ("light", "heavy")
            for number in range(2)
        ]
        self.open(self.units(participants)["version"])
        rows = [
            event
            for item in participants if item["load"] == "light"
            for event in observations(item["unit_id"])
        ]
        self.deliver("light-only", rows)
        report = self.draft()["market_results"][0]
        self.assertIsNone(report["metrics"]["manual"]["assisted"])
        self.assertEqual(report["cells"][0]["metrics"]["manual"]["assisted"], "0.000")
        self.assertEqual(report["recommendation"], "insufficient")

    def test_aborted_cycles_enter_failure_but_not_missing_energy(self):
        self.setup_trial()
        events = self.green_events()
        events += observations("a-one", "failed", abort=True, wh=None,
                               start="2026-10-25T12:00:00Z")
        self.deliver("abort", events)
        estimate = self.draft()["market_results"][0]
        self.assertEqual(estimate["metrics"]["failure"]["assisted"], "2500.000")
        assisted = estimate["cells"][0]["arms"]["assisted"]
        self.assertEqual(assisted["energy_coverage_bp"], "10000.000")
        self.assertEqual(assisted["eligible_measured_cycles"], 2)
        self.assertEqual(estimate["recommendation"], "hold")

    def test_zero_denominator_energy_distinguishes_zero_from_missing(self):
        self.setup_trial()
        rows = observations("s-one", manual=True, wh=0) + observations("s-two", manual=True, wh=0)
        rows += observations("a-one", wh=0) + observations("a-two", wh=0)
        self.deliver("zero", rows)
        report = self.draft()["market_results"][0]
        self.assertEqual(report["metrics"]["energy_wh"]["standard"], "0.000")
        self.assertEqual(report["gates"][-1]["gate"], "zero_baseline_energy")
        self.assertEqual(report["gates"][-1]["state"], "pass")
        edit = {**rows[-2], "revision": 2, "meter_wh": 21}
        self.deliver("nonzero", [edit])
        changed = self.draft()["market_results"][0]
        self.assertEqual(changed["gates"][-1]["state"], "fail")
        self.assertEqual(changed["recommendation"], "hold")

    def test_missing_enrollment_coverage_produces_sensitivity_interval_and_calendar(self):
        units = [
            enrollment("s-one", "standard"), enrollment("s-two", "standard"),
            enrollment("a-one", "assisted"), enrollment("a-two", "assisted"),
            enrollment("a-extra", "assisted", start="2026-10-27"),
        ]
        self.setup_trial(units=units)
        cut = self.deliver("normal", self.green_events())["current_cut"]
        report = self.draft()["market_results"][0]
        self.assertEqual(report["recommendation"], "insufficient")
        readout = self.call("GET", MAIN + "/sensitivity", params={"cut": cut})
        self.assertEqual(readout["metrics"]["alarm"]["effect"]["high_bp"], "3333.333")
        self.assertEqual(readout["metrics"]["alarm"]["effect"]["verdict"],
                         "depends_on_unqualified_units")
        result = self.call("GET", MAIN + "/coverage-calendar", params={"cut": cut})
        arms = [item["cells"][0]["arms"]["assisted"] for item in result["days"]]
        self.assertEqual([row["active_on_date"] for row in arms], [2, 2, 2, 3, 3])
        self.assertEqual([row["qualified_to_date"] for row in arms], [0, 2, 2, 2, 2])
        self.assertEqual(arms[-1]["short_of_coverage_target"], 1)

    def test_site_omission_represents_lost_support_without_renormalizing(self):
        self.create()
        self.register()
        self.register(site("south"))
        units = [
            enrollment("s-one", "standard"),
            enrollment("s-two", "standard", at="south"),
            enrollment("a-one", "assisted"),
            enrollment("a-two", "assisted", at="south"),
        ]
        self.open(self.units(units)["version"])
        self.deliver("full", self.green_events())
        report = self.call("GET", MAIN + "/site-influence")
        self.assertEqual(report["full_recommendation"], "advance")
        self.assertEqual(report["sites_with_recommendation_change"], ["north", "south"])
        omitted = report["site_omissions"][0]
        self.assertEqual(omitted["removed_units"], 2)
        self.assertIsNone(omitted["metrics"]["manual"]["remaining_effect"])
        self.assertEqual(omitted["remaining_recommendation"], "insufficient")
