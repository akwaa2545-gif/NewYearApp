"""Convert both event workbook sheets into the staff application's JSON source.

``DATA`` is the employee master and ``Lucky Wheel`` is the draw-participant list.
They are reconciled by employee ID, never by row number. Emergency-contact details
from DATA are deliberately excluded from the operational JSON.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets

import openpyxl


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "Emp. Data for Registration - IT.xlsx"
DEFAULT_OUTPUT = ROOT / "data" / "employees.json"
EMPLOYEE_ID_WIDTH = 7


def text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def employee_id_text(value):
    value = text(value)
    return value.zfill(EMPLOYEE_ID_WIDTH) if value.isdigit() and len(value) < EMPLOYEE_ID_WIDTH else value


def existing_tokens(path):
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            employee_id_text(item["employee_id"]): str(item["qr_token"])
            for item in payload.get("employees", [])
            if item.get("employee_id") and item.get("qr_token")
        }
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def convert(source, output):
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    required = {"DATA", "Lucky Wheel"}
    missing = required.difference(workbook.sheetnames)
    if missing:
        workbook.close()
        raise ValueError("Workbook is missing worksheet: " + ", ".join(sorted(missing)))
    sheet = workbook["DATA"]
    lucky_sheet = workbook["Lucky Wheel"]
    reused = existing_tokens(output)
    seen = set()
    employees = []
    skipped = []

    lucky_ids = []
    lucky_seen = set()
    lucky_skipped = []
    for row_number, row in enumerate(lucky_sheet.iter_rows(min_row=2, values_only=True), 2):
        values = list(row) + [None, None]
        employee_id = employee_id_text(values[1])
        if not employee_id:
            if any(text(value) for value in values):
                lucky_skipped.append({"row": row_number, "reason": "missing employee ID"})
            continue
        if employee_id in lucky_seen:
            lucky_skipped.append({"row": row_number, "employee_id": employee_id,
                                  "reason": "duplicate employee ID"})
            continue
        lucky_seen.add(employee_id)
        lucky_ids.append(employee_id)

    for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2):
        values = list(row) + [None] * max(0, 9 - len(row))
        employee_id = employee_id_text(values[1])
        full_name = text(values[5]) or " ".join(filter(None, (
            text(values[2]), text(values[3]), text(values[4])
        )))
        if not employee_id and not full_name:
            continue
        if not employee_id or not full_name:
            skipped.append({"row": row_number, "reason": "missing employee ID or name"})
            continue
        if employee_id in seen:
            skipped.append({"row": row_number, "employee_id": employee_id,
                            "reason": "duplicate employee ID"})
            continue
        seen.add(employee_id)
        token = reused.get(employee_id) or secrets.token_urlsafe(24)
        employees.append({
            "employee_id": employee_id,
            "name": full_name,
            "first_name": text(values[3]),
            "last_name": text(values[4]),
            "department": text(values[7]),
            "position": text(values[6]),
            "route": text(values[8]),
            "eligible_for_draw": employee_id in lucky_seen,
            "qr_token": token,
            "qr_code": "PARTY:" + token,
        })

    workbook.close()

    if not employees:
        raise ValueError("No valid employee rows were found.")

    payload = {
        "schema_version": 1,
        "source_file": source.name,
        "source_sheets": {"employees": "DATA", "draw_participants": "Lucky Wheel"},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "employee_count": len(employees),
        "draw_participant_count": sum(1 for item in employees if item["eligible_for_draw"]),
        "skipped_rows": skipped,
        "reconciliation": {
            "matched": len(seen.intersection(lucky_seen)),
            "data_only": len(seen.difference(lucky_seen)),
            "lucky_only": len(lucky_seen.difference(seen)),
            "lucky_skipped_rows": lucky_skipped,
        },
        "employees": employees,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = convert(args.source.resolve(), args.output.resolve())
    print(f"Created {args.output}: {payload['employee_count']} employees, "
          f"{payload['draw_participant_count']} lucky-wheel participants, "
          f"{len(payload['skipped_rows'])} skipped DATA rows")


if __name__ == "__main__":
    main()
