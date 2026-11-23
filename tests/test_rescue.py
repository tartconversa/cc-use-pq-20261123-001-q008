from __future__ import annotations

from tests.support import (
    ApiCase,
    MAIN,
    ROOT,
    enrollment,
    event,
    observations,
    plan,
)

CHECKPOINTS = [300, 600]


def rescue_plan(**overrides):
    trial_plan = plan()
    trial_plan["policy"]["rescue_checkpoints_seconds"] = CHECKPOINTS
    trial_plan["policy"].update(overrides)
    return trial_plan


def segment(segment_id, unit, online_from, online_until, *, revision=1, void=False):
    return {
        "segment_id": segment_id,
        "revision": revision,
        "unit_id": unit,
        "online_from": online_from,
        "online_until": online_until,
        "void": void,
    }


class RescueTimelineTest(ApiCase):
    def setUp(self):
        super().setUp()
        self.setup_trial(rescue_plan())

    def timeline(self, **params):
        return self.call("GET", MAIN + "/rescue-timeline", params=params)

    def arm_cell(self, readout, arm):
        return next(cell for cell in readout["cells"] if cell["arm"] == arm)

    def deliver_monitoring(self, delivery_id, segments, **kwargs):
        return self.call("POST", ROOT + "/monitoring",
                         {"delivery_id": delivery_id, "segments": segments}, **kwargs)

    def test_first_rescue_counts_once_while_repeated_presses_remain_visible(self):
        rows = observations("s-one", manual=True)
        rows.append(event("s-one", "a", "again", "manual", "2026-10-25T09:08:00Z"))
        rows += observations("s-two", manual=True) + observations("a-one") + observations("a-two")
        self.deliver("repeats", rows)
        readout = self.timeline()
        standard = self.arm_cell(readout, "standard")
        coverage = standard["checkpoints"][1]["coverage"]
        # 两台标准设备各发生一次首次救场；s-one 的重复按键不重复计数
        self.assertEqual(coverage["first_rescue"]["precise_cycles"], 2)
        self.assertEqual(coverage["first_rescue"]["possible_cycles"], 0)
        self.assertEqual(standard["repeated_press_cycles"], 1)
        # 旧二元手动率仍然把该周期记为手动周期
        self.assertEqual(readout["legacy_manual_rate"]["manual"]["standard"], "10000.000")

    def test_assisted_rescue_comes_later_and_earlier_abort_blocks_advance(self):
        rows = (
            observations("s-one", manual=True) + observations("s-two", manual=True)
            + observations("a-one")
            + observations("a-two", "fails", abort=True, wh=None)
        )
        self.deliver("abort", rows)
        readout = self.timeline()
        decision = readout["rescue_decision"]
        self.assertEqual(decision["recommendation"], "hold")
        late = decision["checkpoints"][1]
        self.assertIn("earlier_termination_increase", late["blocking_reasons"])
        assisted = self.arm_cell(readout, "assisted")
        self.assertEqual(
            assisted["checkpoints"][1]["cumulative_incidence_bp"]["fault_termination"],
            {"low_bp": "5000.000", "high_bp": "5000.000"},
        )
        # 较早时点中止尚未发生，不会仅凭该时点更低的救场率给出推进
        self.assertEqual(decision["checkpoints"][0]["verdict"], "pass")

    def test_telemetry_gap_turns_a_later_rescue_into_bounded_uncertainty(self):
        rows = (
            observations("s-one", manual=True) + observations("s-two", manual=True)
            + observations("a-one") + observations("a-two")
        )
        first_cut = self.deliver("signals", rows)["current_cut"]
        segments = [
            segment("s2-a", "s-two", "2026-10-25T09:00:00Z", "2026-10-25T09:02:00Z"),
            segment("s2-b", "s-two", "2026-10-25T09:04:00Z", "2026-10-25T09:11:00Z"),
            segment("s1", "s-one", "2026-10-25T08:00:00Z", "2026-10-25T10:00:00Z"),
            segment("a1", "a-one", "2026-10-25T08:00:00Z", "2026-10-25T10:00:00Z"),
            segment("a2", "a-two", "2026-10-25T08:00:00Z", "2026-10-25T10:00:00Z"),
        ]
        cut = self.deliver_monitoring("monitoring", segments)["current_cut"]
        self.assertGreater(cut, first_cut)
        readout = self.timeline()
        standard = self.arm_cell(readout, "standard")
        point = standard["checkpoints"][1]
        self.assertEqual(point["coverage"]["first_rescue"],
                         {"precise_cycles": 1, "possible_cycles": 1})
        self.assertEqual(point["coverage"]["partially_observed"]["telemetry_gap_cycles"], 1)
        self.assertEqual(
            point["cumulative_incidence_bp"]["first_manual_rescue"],
            {"low_bp": "5000.000", "high_bp": "10000.000"},
        )
        # 旧截点读取不受后到监测影响
        old = self.timeline(cut=first_cut)
        old_point = self.arm_cell(old, "standard")["checkpoints"][1]
        self.assertEqual(old_point["coverage"]["first_rescue"]["precise_cycles"], 2)

    def test_unobserved_origin_never_enters_risk_and_is_reported_separately(self):
        rows = (
            observations("s-one", manual=True) + observations("s-two")
            + observations("a-one") + observations("a-two")
        )
        self.deliver("signals", rows)
        segments = [
            segment("s1-late", "s-one", "2026-10-25T09:01:00Z", "2026-10-25T10:00:00Z"),
            segment("s2", "s-two", "2026-10-25T08:00:00Z", "2026-10-25T10:00:00Z"),
            segment("a1", "a-one", "2026-10-25T08:00:00Z", "2026-10-25T10:00:00Z"),
            segment("a2", "a-two", "2026-10-25T08:00:00Z", "2026-10-25T10:00:00Z"),
        ]
        self.deliver_monitoring("monitoring", segments)
        point = self.arm_cell(self.timeline(), "standard")["checkpoints"][1]
        partial = point["coverage"]["partially_observed"]
        self.assertEqual(partial["unobserved_origin_cycles"], 1)
        self.assertEqual(point["coverage"]["entered_cycles"], 1)
        self.assertEqual(
            point["cumulative_incidence_bp"]["first_manual_rescue"],
            {"low_bp": "0.000", "high_bp": "5000.000"},
        )

    def test_tie_at_last_confirmed_moment_is_uncertain_not_precise(self):
        tie = [
            event("a-one", "tie", "start", "start", "2026-10-26T09:00:00Z"),
            event("a-one", "tie", "man", "manual", "2026-10-26T09:05:00Z"),
            event("a-one", "tie", "end", "complete", "2026-10-26T09:05:00Z"),
            event("a-one", "tie", "m0", "meter", "2026-10-26T09:00:00Z",
                  meter_id="main", meter_wh=10),
            event("a-one", "tie", "m1", "meter", "2026-10-26T09:05:00Z",
                  meter_id="main", meter_wh=20),
        ]
        rows = (tie + observations("a-two")
                + observations("s-one", manual=True) + observations("s-two", manual=True))
        self.deliver("tie", rows)
        point = self.arm_cell(self.timeline(), "assisted")["checkpoints"][1]
        self.assertEqual(point["coverage"]["first_rescue"]["possible_cycles"], 1)
        self.assertEqual(point["coverage"]["first_rescue"]["precise_cycles"], 0)
        self.assertEqual(
            point["cumulative_incidence_bp"]["first_manual_rescue"],
            {"low_bp": "0.000", "high_bp": "5000.000"},
        )

    def test_open_cycle_is_right_censored_and_listed_as_partial_observation(self):
        rows = self.green_events()
        self.deliver("closed", rows)
        self.deliver("open", [
            event("a-one", "pending", "start", "start", "2026-10-26T09:00:00Z"),
        ])
        point = self.arm_cell(self.timeline(), "assisted")["checkpoints"][0]
        partial = point["coverage"]["partially_observed"]
        self.assertEqual(partial["still_open_cycles"], 1)
        self.assertEqual(partial["telemetry_gap_cycles"], 0)
        # 右删失周期不制造任何结局
        self.assertEqual(point["cumulative_incidence_bp"]["first_manual_rescue"],
                         {"low_bp": "0.000", "high_bp": "0.000"})

    def test_unsupported_checkpoint_is_never_filled_from_the_last_known_rate(self):
        # 只有 a-one 有观测；按单元最低数要求，两个组在该时点都不支持
        self.deliver("thin", observations("a-one"))
        readout = self.timeline()
        for cell in readout["cells"]:
            for point in cell["checkpoints"]:
                self.assertFalse(point["supported"])
                self.assertIsNone(
                    point["cumulative_incidence_bp"]["first_manual_rescue"]["low_bp"]
                )
        self.assertEqual(readout["rescue_decision"]["recommendation"], "insufficient")
        for verdict in readout["rescue_decision"]["checkpoints"]:
            self.assertEqual(verdict["verdict"], "insufficient_evidence")

    def test_monitoring_uses_the_shared_cut_and_supports_retraction_and_replay(self):
        segments = [
            segment("only", "a-one", "2026-10-25T08:00:00Z", "2026-10-25T10:00:00Z"),
        ]
        first = self.deliver_monitoring("mon", segments)
        self.assertEqual(first["current_cut"], 1)
        replay = self.deliver_monitoring("mon", segments)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["current_cut"], 1)
        retraction = [segment(
            "only", "a-one", "2026-10-25T08:00:00Z", "2026-10-25T10:00:00Z",
            revision=2, void=True,
        )]
        second = self.deliver_monitoring("retract", retraction)
        self.assertEqual(second["current_cut"], 2)
        visible = self.call("GET", ROOT + "/monitoring", params={"cut": 1})
        self.assertEqual(len(visible["segments"]), 1)
        withdrawn = self.call("GET", ROOT + "/monitoring")
        self.assertEqual(withdrawn["segments"], [])
        # 修订必须顺序递增
        bad = self.deliver_monitoring(
            "gap", [segment("x", "a-one", "2026-10-25T08:00:00Z",
                            "2026-10-25T09:00:00Z", revision=3)],
            status=409,
        )
        self.assertEqual(bad["code"], "revision_gap")

    def test_monitoring_outside_enrollment_and_bad_windows_are_rejected(self):
        bad = self.deliver_monitoring("outside", [
            segment("x", "a-one", "2026-10-20T08:00:00Z", "2026-10-20T09:00:00Z"),
        ], status=422)
        self.assertEqual(bad["code"], "monitor_outside_enrollment")
        inverted = self.deliver_monitoring("inverted", [
            segment("y", "a-one", "2026-10-25T10:00:00Z", "2026-10-25T09:00:00Z"),
        ], status=422)
        self.assertIn("must end after it begins", str(inverted))

    def test_rescue_reconciliation_tracks_classification_risk_and_legacy_rate(self):
        rows = (
            observations("s-one", manual=True) + observations("s-two", manual=True)
            + observations("a-one") + observations("a-two")
        )
        before = self.deliver("baseline", rows)["current_cut"]
        manual = next(item for item in rows
                      if item["unit_id"] == "s-one" and item["kind"] == "manual")
        after = self.deliver("retract-manual",
                             [{**manual, "revision": 2, "void": True}])["current_cut"]
        report = self.call(
            "GET", MAIN + "/rescue-reconciliation",
            params={"before_cut": before, "after_cut": after},
        )
        self.assertEqual(report["classification_transitions"], [{
            "from_classification": "first_rescue_observed",
            "to_classification": "closed_without_rescue_completed",
            "cycles": 1,
        }])
        self.assertEqual(report["legacy_manual_rate"]["before"]["standard"], "10000.000")
        self.assertEqual(report["legacy_manual_rate"]["after"]["standard"], "5000.000")
        self.assertIn("s-one", report["still_at_risk"]["after"]["clean_no_event_units"])
        self.assertEqual(report["total_changed_cycles"], 1)
        self.assertEqual(report["cycles"][0]["after_first_manual_at"], None)

    def test_market_mix_reuses_planned_weights_without_renormalization(self):
        # 双单元试点需要独立的应用实例，避免与 setUp 的单单元试点冲突
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from fastapi.testclient import TestClient
        from fieldproof.api import create_app

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = TestClient(create_app(Path(directory.name) / "mix.sqlite3"))
        self.addCleanup(self.client.close)
        trial_plan = plan(second_cell=True)
        trial_plan["policy"]["rescue_checkpoints_seconds"] = CHECKPOINTS
        self.create(trial_plan)
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
            rows += observations(item["unit_id"],
                                 manual=(light and standard) or (not light and not standard))
        self.deliver("weighted", rows)
        readout = self.timeline()
        weights = sorted(cell["weight_bp"] for cell in readout["cells"])
        self.assertEqual(weights, [3000, 3000, 7000, 7000])
        decision = readout["rescue_decision"]
        self.assertTrue(all(point["supported"]
                            for cell in readout["cells"]
                            for point in cell["checkpoints"]))
        # 固定市场权重（70/30）下节省为 4000bp，超过 1000bp 护栏且无中止增加
        self.assertEqual(decision["recommendation"], "advance")
        point = decision["checkpoints"][1]
        self.assertEqual(point["rescue_rate_difference_bp"],
                         {"definition": "standard_minus_assisted",
                          "low_bp": "4000.000", "high_bp": "4000.000"})

    def test_legacy_endpoints_keep_their_original_payloads(self):
        self.deliver("baseline", self.green_events())
        preview = self.draft()["market_results"][0]
        self.assertEqual(preview["metrics"]["manual"]["assisted"], "0.000")
        self.assertNotIn("rescue_decision", preview)
        timeline = self.timeline()
        self.assertEqual(timeline["legacy_manual_rate"]["manual"]["assisted"], "0.000")
        self.assertEqual(timeline["checkpoints_seconds"], CHECKPOINTS)

    def test_policy_checkpoints_must_be_ordered_and_within_cycle_limit(self):
        bad_plan = rescue_plan()
        bad_plan["policy"]["rescue_checkpoints_seconds"] = [600, 300]
        self.call("POST", "/trials", {"trial_id": "valley", "plan": bad_plan}, status=422)
        beyond = rescue_plan()
        beyond["policy"]["rescue_checkpoints_seconds"] = [9_999]
        self.call("POST", "/trials", {"trial_id": "valley", "plan": beyond}, status=422)
