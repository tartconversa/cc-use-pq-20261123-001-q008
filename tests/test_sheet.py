from contextlib import redirect_stderr, redirect_stdout
import csv
from io import StringIO
import json
from pathlib import Path

from fieldproof.cli import main
from tests.support import ApiCase, ROOT


HEADER = ["event_id", "revision", "unit_id", "cycle_id", "kind",
          "observed_at", "fold", "void", "meter_id", "meter_wh", "reason"]


def sheet_row(event_id, kind, stamp, *, fold="", meter_id="", meter_wh=""):
    return [event_id, "1", "a-one", "sheet", kind, stamp, fold, "", meter_id, meter_wh, ""]


class FieldSheetTest(ApiCase):
    def setUp(self):
        super().setUp()
        self.setup_trial()
        self.sheet = self.db.parent / "arrival.csv"

    def write(self, rows, header=HEADER):
        with self.sheet.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(header)
            writer.writerows(rows)

    def ingest(self):
        out = StringIO()
        error = StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            result = main([
                "--db", str(self.db), "ingest-sheet", "valley", "site-file", str(self.sheet),
            ])
        return result, json.loads(out.getvalue() if result == 0 else error.getvalue())

    def test_two_sides_of_repeated_local_hour_join_to_one_valid_cycle(self):
        start = sheet_row("sheet-start", "start", "2026-10-25 02:50:00", fold="0")
        end = sheet_row("sheet-stop", "complete", "2026-10-25 02:05:00", fold="1")
        left = sheet_row("sheet-m1", "meter", "2026-10-25 02:50:00", fold="0",
                         meter_id="meter", meter_wh="120")
        right = sheet_row("sheet-m2", "meter", "2026-10-25 02:05:00", fold="1",
                          meter_id="meter", meter_wh="240")
        self.write([right, start, left, end])
        code, result = self.ingest()
        self.assertEqual((code, result["accepted"]), (0, 4))
        row = self.call("GET", ROOT + "/units/a-one/observations")["cycles"][0]
        self.assertEqual(row["elapsed_seconds"], 900)
        self.assertEqual(row["energy_wh"], 120)
        self.assertEqual(row["local_date"], "2026-10-25")
        self.assertTrue(self.ingest()[1]["replayed"])

    def test_fold_and_late_bad_row_reject_entire_file(self):
        first = sheet_row("sheet-start", "start", "2026-10-25 02:45:00", fold="0")
        second = sheet_row("sheet-stop", "complete", "2026-10-25 02:05:00")
        self.write([first, second])
        status, problem = self.ingest()
        self.assertEqual(status, 1)
        self.assertEqual(problem["details"], {"line": 3, "field": "fold"})
        self.assertEqual(self.call("GET", ROOT)["current_cut"], 0)
        second[6] = "1"
        self.write([first, second])
        self.assertEqual(self.ingest()[1]["accepted"], 2)

    def test_source_row_fails_before_partial_write_for_bad_meter_value(self):
        first = sheet_row("sheet-start", "start", "2026-10-25T12:00:00Z")
        bad = sheet_row("sheet-meter", "meter", "2026-10-25T12:00:00Z",
                        meter_id="meter", meter_wh="2.1")
        self.write([first, bad])
        code, problem = self.ingest()
        self.assertEqual(code, 1)
        self.assertEqual(problem["details"], {"line": 3, "field": "meter_wh"})
        self.assertEqual(self.call("GET", ROOT)["current_cut"], 0)
