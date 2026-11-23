from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import date
from pathlib import Path
import sys

from pydantic import ValidationError

from fieldproof.analysis import evidence, reconcile
from fieldproof.common import encoded, Problem, invalid
from fieldproof.design.contracts import Key, Input
from fieldproof.design.registry import get_trial, roster
from fieldproof.signals import ledger
from fieldproof.signals.sheets import parse_sheet
from fieldproof.storage import Store


class IngestSelection(Input):
    trial_id: Key
    delivery_id: Key


def import_sheet(db, arguments):
    select = IngestSelection(trial_id=arguments.trial, delivery_id=arguments.delivery)
    trial = get_trial(db, select.trial_id)
    assigned = roster(db, trial.trial_id)
    handle = (
        nullcontext(sys.stdin)
        if arguments.sheet == "-"
        else Path(arguments.sheet).open("r", encoding="utf-8-sig", newline="")
    )
    with handle as source:
        delivery = parse_sheet(source, select.delivery_id, assigned)
    return encoded(ledger.append(db, trial, assigned, delivery))


def preview(db, arguments):
    trial = get_trial(db, arguments.trial)
    window = evidence.select_window(trial, arguments.start, arguments.end)
    if arguments.cut is not None and not 0 <= arguments.cut < 2**63:
        raise invalid("cut_range", "Cut must be a nonnegative 64-bit integer")
    document = evidence.build(db, trial, window, arguments.cut)
    return reconcile.csv_metrics(document) if arguments.csv else encoded(document)


def export(db, arguments):
    document = evidence.get(db, arguments.trial, arguments.evidence)
    return reconcile.csv_metrics(document)


def iso_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected an ISO calendar date") from exc


def make_parser():
    parser = argparse.ArgumentParser(prog="python -m fieldproof.cli")
    parser.add_argument("--db", type=Path, required=True, help="FieldProof SQLite database")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest-sheet", help="Record a field operator's CSV delivery")
    ingest.add_argument("trial", help="Trial identifier")
    ingest.add_argument("delivery", help="Stable delivery identifier for retries")
    ingest.add_argument("sheet", help="UTF-8 CSV file, or - for standard input")
    draft = commands.add_parser("preview", help="Inspect an as-of comparison")
    draft.add_argument("trial")
    draft.add_argument("--start", type=iso_date)
    draft.add_argument("--end", type=iso_date)
    draft.add_argument("--cut", type=int)
    draft.add_argument("--csv", action="store_true")
    frozen = commands.add_parser("export", help="Export the numbers in frozen evidence")
    frozen.add_argument("trial")
    frozen.add_argument("evidence")
    return parser


def main(argv=None):
    args = make_parser().parse_args(argv)
    actions = {"ingest-sheet": import_sheet, "preview": preview, "export": export}
    try:
        store = Store(args.db)
        with store.transaction(write=args.command == "ingest-sheet") as db:
            output = actions[args.command](db, args)
    except Problem as exc:
        print(encoded(exc.payload()), file=sys.stderr)
        return 1
    except (OSError, UnicodeError, ValidationError) as exc:
        print(encoded({"code": "input_error", "message": str(exc)}), file=sys.stderr)
        return 1
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
