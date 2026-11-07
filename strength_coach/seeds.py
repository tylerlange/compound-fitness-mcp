"""Default exercise-name equivalences, seeded before every import.

Left = a name a source app might use; right = the canonical name the store
keeps. These cover only naming that is the same lift by any reasonable
reading: an implement prefix on the main barbell lifts, plural and spelling
variants, and two common abbreviations. Whether two genuinely different
variations (barbell vs dumbbell incline, rope vs bar pushdown) count as one
lift is a training decision, so none are merged here.

Per-user mappings go in `$COACH_DATA_DIR/aliases.yaml` and win over these
(see `strength_coach.aliases`). No key may also be a value: that would make
seeding order matter (tests/test_seeds.py enforces it).
"""

DEFAULT_ALIASES = {
    # Main barbell lifts: some apps prefix the implement.
    "Barbell Bench Press": "Bench Press",
    "Barbell Back Squat": "Squat",
    "Barbell Overhead Press": "Overhead Press",
    "Barbell Deadlift": "Deadlift",
    # Plural and spelling variants.
    "Lat Pulldowns": "Lat Pulldown",
    "Pull Ups": "Pull-Up",
    "Pullups": "Pull-Up",
    "Chinups": "Chin-Up",
    "Chin Up": "Chin-Up",
    "Good Mornings": "Good Morning",
    "Tricep Pushdown": "Triceps Pushdown",
    # Word order: same movement, grip as a suffix.
    "Bench Press (Close Grip)": "Close Grip Bench Press",
    # Common abbreviations, so a bare "rdl"/"ohp" resolves to the right lift
    # instead of substring-matching a different one (e.g. "Single Leg RDL").
    "RDL": "Romanian Deadlift",
    "OHP": "Overhead Press",
}
