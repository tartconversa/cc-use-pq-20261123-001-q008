from copy import deepcopy

from tests.support import ApiCase, ROOT, enrollment, plan, site


class TrialDesignTest(ApiCase):
    def test_open_rejects_one_empty_arm_and_exposes_actual_gap(self):
        self.create()
        self.register()
        state = self.units([
            enrollment("s-one", "standard"),
            enrollment("s-two", "standard"),
            enrollment("a-one", "assisted"),
        ])
        result = self.call("POST", ROOT + "/open", {
            "expected_version": state["version"],
        }, status=409)
        self.assertEqual(result["code"], "allocation_shortfall")
        self.assertEqual(result["details"], [{
            "market": "alpine", "cell": "cold/light", "arm": "assisted",
            "enrolled": 1, "required": 2,
        }])
        self.assertEqual(self.call("GET", ROOT)["phase"], "draft")

    def test_roster_is_fixed_after_start(self):
        state = self.setup_trial()
        self.assertEqual(state["phase"], "open")
        invalid = self.call("POST", ROOT + "/enrollment", {
            "units": [enrollment("s-new", "standard")],
        }, status=409)
        self.assertEqual(invalid["details"]["phase"], "open")
        self.assertEqual(self.call("GET", ROOT + "/roster")["allocation"], {
            "alpine": {"cold/light": {"standard": 2, "assisted": 2}},
        })
        failure = self.call("PUT", ROOT + "/plan", {
            "expected_version": state["version"], "plan": plan(second_cell=True),
        }, status=409)
        self.assertEqual(failure["code"], "trial_phase")

    def test_plan_change_cannot_orphan_existing_assignment(self):
        self.create(plan(second_cell=True))
        self.register()
        self.units([enrollment("heavy", "standard", load="heavy")])
        baseline = self.call("GET", ROOT)
        failure = self.call("PUT", ROOT + "/plan", {
            "expected_version": baseline["version"], "plan": plan(),
        }, status=422)
        self.assertEqual(failure["code"], "unplanned_cell")
        self.assertEqual(self.call("GET", ROOT), baseline)

    def test_old_client_cannot_close_or_replan_using_stale_version(self):
        self.create()
        before = self.call("GET", ROOT)
        self.register()
        old_edit = self.call("PUT", ROOT + "/plan", {
            "expected_version": before["version"], "plan": plan(),
        }, status=409)
        self.assertEqual(old_edit["details"]["current_version"], 2)
        opened = self.open(self.units()["version"])
        old_close = self.call("POST", ROOT + "/close", {
            "expected_version": opened["version"] - 1, "expected_cut": 0,
        }, status=409)
        self.assertEqual(old_close["code"], "trial_changed")

    def test_conflicting_enrollment_rolls_back_whole_allocation(self):
        self.create()
        self.register()
        self.units([enrollment("z-one", "standard")])
        before = self.call("GET", ROOT)
        rejected = self.call("POST", ROOT + "/enrollment", {
            "units": [enrollment("a-new", "assisted"), enrollment("z-one", "assisted")],
        }, status=409)
        self.assertEqual(rejected["code"], "unit_exists")
        self.assertEqual(self.call("GET", ROOT), before)
        self.assertEqual([item["unit_id"] for item in self.call("GET", ROOT + "/roster")["units"]],
                         ["z-one"])

    def test_plan_validations_and_repeated_site(self):
        invalid = deepcopy(plan())
        invalid["markets"][0]["cells"][0]["weight_bp"] = 8_000
        self.call("POST", "/trials", {"trial_id": "valley", "plan": invalid}, status=422)
        self.create()
        self.call("POST", ROOT + "/sites", site(timezone="Unknown/Clock"), status=422)
        self.register()
        self.register()
        self.assertEqual(self.call("GET", ROOT)["version"], 2)
        other = site(timezone="UTC")
        self.call("POST", ROOT + "/sites", other, status=409)
