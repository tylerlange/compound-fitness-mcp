"""Builders for synthetic source files. All data here is fabricated — the real
exports never live in the repo. Each builder writes a file mimicking one app's
export schema so adapters can be tested against realistic shapes offline.
"""

import csv
import io
import sqlite3
import zipfile


def write_stronglifts_db(path: str) -> None:
    """Minimal StrongLifts-family backup: two workouts, incl. a warmup, an
    AMRAP (reptype=1), an RPE-bearing set, and two workouts colliding on the
    same calendar day + day_name (must NOT merge)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE history (id INTEGER PRIMARY KEY, date INTEGER, day_name TEXT,
                              duration INTEGER);
        CREATE TABLE exercises (id INTEGER PRIMARY KEY, exercise_name TEXT);
        CREATE TABLE history_exercises (
            id INTEGER PRIMARY KEY, history_id INTEGER, exercise_id INTEGER,
            weightlb REAL, weightkg REAL, reps INTEGER, RPE REAL, type INTEGER,
            reptype INTEGER, exercise_number INTEGER, set_number INTEGER);
        """
    )
    # 2020-06-01 in epoch ms
    d1 = 1590969600000
    conn.executemany(
        "INSERT INTO history (id, date, day_name, duration) VALUES (?, ?, ?, ?)",
        [(1, d1, "A", 45), (2, d1, "A", 30)],  # same day + name -> must stay distinct
    )
    conn.executemany(
        "INSERT INTO exercises (id, exercise_name) VALUES (?, ?)",
        [(1, "Squat"), (2, "Bench Press")],
    )
    conn.executemany(
        """INSERT INTO history_exercises
           (history_id, exercise_id, weightlb, weightkg, reps, RPE, type, reptype,
            exercise_number, set_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (1, 1, 45, 20.4, 5, 0, 1, 0, 1, 1),   # warmup (type=1)
            (1, 1, 225, 102, 5, 8, 0, 1, 1, 2),   # AMRAP (reptype=1), RPE 8 -> RIR 2
            (2, 2, 185, 84, 5, 0, 0, 0, 1, 1),    # second same-day workout
        ],
    )
    conn.commit()
    conn.close()


def write_liftin_csv(path: str) -> None:
    """Liftin flat CSV: a warmup row, kg-free lb weights, empty Perception, and
    two same-day sessions distinguished only by timestamp."""
    rows = [
        ["Date", "Duration", "Routine", "Exercise", "Set", "Warmup", "Weight",
         "Reps/Time", "Goal", "Perception"],
        ["2024-03-01 08:00", "1h 05m", "Day 1", "Bench Press", "1", "x", "95 lbs", "5", "", ""],
        ["2024-03-01 08:00", "1h 05m", "Day 1", "Bench Press", "2", "", "135 lbs", "5", "", ""],
        ["2024-03-01 18:00", "0h 40m", "Day 1", "Deadlift", "1", "", "315 lbs", "3", "", ""],
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def write_liftosaur_json(path: str) -> None:
    """Liftosaur JSON export: one history record with a warmup + working set
    (incl. an incomplete/skipped set that must be excluded) plus an AMRAP set."""
    import json

    data = {
        "version": 1,
        "history": [
            {  # started-but-abandoned record: no entries -> must be skipped
                "vtype": "history_record", "id": 999,
                "date": "2025-10-06T18:00:00.000Z",
                "programName": "My Program", "dayName": "Day 2",
                "entries": [],
            },
            {
                "vtype": "history_record",
                "id": 555,
                "date": "2025-10-05T18:00:00.000Z",
                "programName": "My Program",
                "dayName": "Day 1",
                "startTime": 1_000_000,
                "endTime": 4_600_000,  # 3,600,000 ms = 3600s = 1h duration
                "entries": [
                    {
                        "exercise": {"name": "Bench Press"},
                        "warmupSets": [
                            {"isCompleted": True, "completedWeight":
                             {"unit": "lb", "value": 95}, "completedReps": 5},
                        ],
                        "sets": [
                            {"isCompleted": True, "isAmrap": False,
                             "completedWeight": {"unit": "lb", "value": 185},
                             "completedReps": 5},
                            {"isCompleted": True, "isAmrap": True,
                             "completedWeight": {"unit": "lb", "value": 185},
                             "completedReps": 8},
                            {"isCompleted": False, "isAmrap": False,
                             "completedWeight": {"unit": "lb", "value": 185},
                             "completedReps": 5},  # skipped, must be excluded
                        ],
                    },
                    {
                        "exercise": {"name": "Squat"},
                        "warmupSets": [],
                        "sets": [
                            {"isCompleted": True, "isAmrap": False,
                             "completedWeight": {"unit": "kg", "value": 100},
                             "completedReps": 5},
                        ],
                    },
                ],
            }
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def write_macrofactor_xlsx(path: str, with_program: bool = True,
                           weight_header: str = "Weight (lbs)",
                           weights: tuple = (95, 135, 225)) -> None:
    """MacroFactor quick export: a Workout Log (BOM on Date header, standard +
    warmup + failure set types) and optionally an Active Program sheet.
    `weight_header`/`weights` let tests vary the per-set weight column."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Workout Log"
    ws.append(["﻿Date", "Workout Duration", "Workout", "Exercise",
               "Exercise Base Weight (lbs)", "Set Type", weight_header, "Reps",
               "RIR", "Duration", "Distance short (Yd)", "Distance long (Mi)"])
    # 2026-02-01 as Excel serial = 46054
    serial = 46054
    w_warm, w_std, w_fail = weights
    ws.append([serial, 3600, "Workout A", "Close Grip Bench Press", None,
               "Warm-Up Set", w_warm, 8, 4, None, None, None])
    ws.append([serial, 3600, "Workout A", "Close Grip Bench Press", None,
               "Standard Set", w_std, 5, 2, None, None, None])
    ws.append([serial, 3600, "Workout A", "Barbell Back Squat", None,
               "Failure Set", w_fail, 3, 0, None, None, None])

    if with_program:
        ap = wb.create_sheet("Active Program")
        ap.append(["Program: Advanced Full Body", "Cycles: 5", "Deload: Last Cycle"])
        ap.append(["Block 1"])
        ap.append(["Cycle 1", "Exercise", "Skipped", "Notes", "Set 1 Type",
                   "Set 1 Rep Range", "Set 1 RIR"])
        ap.append(["Workout A @ Home Gym", "Close Grip Bench Press", "No", None,
                   "Standard Set", "4 - 6", "1.0"])
        ap.append([None, "Barbell Back Squat", "No", None, "Standard Set", "4 - 6", "1.0"])
    wb.save(path)


def write_alltrails_export(path: str) -> None:
    """Minimal AllTrails account export: a full-export zip whose nested
    "Activities" zip holds one recorded hike (recordings/, 19 points 10s apart,
    steady +3m/point climb) plus one saved route (maps/) that must be ignored.
    """
    hdr = (
        '<?xml version="1.0"?>\n<gpx xmlns='
        '"http://www.topografix.com/GPX/1/1" version="1.1"'
        ' creator="AllTrails.com">\n'
    )
    tpts = []
    for i in range(19):
        lat = 37.0 + i * 0.0001
        ele = 100 + i * 3
        mm, ss = divmod(i * 10, 60)
        tpts.append(
            f'    <trkpt lat="{lat:.4f}" lon="-122.0"><ele>{ele}</ele>'
            f'<time>2021-05-01T10:{mm:02d}:{ss:02d}Z</time></trkpt>\n'
        )
    recording = (
        hdr
        + "  <metadata><name><![CDATA[Test Loop]]></name></metadata>\n"
        + "  <trk><name><![CDATA[Test Loop]]></name><trkseg>\n"
        + "".join(tpts)
        + "  </trkseg></trk>\n</gpx>\n"
    )
    route = (
        hdr
        + "  <metadata><name><![CDATA[Saved Route]]></name></metadata>\n"
        + '  <rte><rtept lat="37.0" lon="-122.0"></rtept></rte>\n</gpx>\n'
    )
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("recordings/test-loop-abc1234.gpx", recording)
        z.writestr("maps/saved-route-def5678.gpx", route)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("report.html", "<html></html>")
        z.writestr("User Profile/User profile.csv", "id\n1\n")
        z.writestr("User Online Activity/Activities 0__test.zip", inner.getvalue())
