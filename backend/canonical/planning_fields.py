"""Provider-neutral keys stored in local planning records."""

from typing import Final

PLANNED_LOAD: Final = "planned_load"
PLANNED_INTENSITY: Final = "planned_intensity"
SESSION_FOCUS: Final = "session_focus"
STRENGTH_INTENSITY: Final = "strength_intensity"
STRENGTH_STRUCTURE: Final = "strength_structure"
# A read-only key of a served planned unit, never stored: its earlier strength
# structures with the time each was replaced, oldest first (#3521).
STRENGTH_STRUCTURE_VERSIONS: Final = "strength_structure_versions"
# The heart-rate cap of an easy run unit: percent of the threshold HR plus bpm.
HR_CAP: Final = "hr_cap"
# The undated library template a planned unit was planned from (#2379).
TEMPLATE_ID: Final = "template_id"
# The two classification keys; the structure is a third local key of strength and
# the cap a fourth one of easy runs.
FOCUS_ONLY_KEYS: Final = (SESSION_FOCUS, STRENGTH_INTENSITY)
SESSION_FOCUS_KEYS: Final = (*FOCUS_ONLY_KEYS, STRENGTH_STRUCTURE, HR_CAP)

ENDURANCE_FOCI: Final = (
    "recovery",
    "easy",
    "endurance",
    "long",
    "tempo",
    "threshold",
    "vo2max",
    "anaerobic",
    "race",
    "test",
)
RIDE_FOCI: Final = (*ENDURANCE_FOCI[:6], "sweet_spot", *ENDURANCE_FOCI[6:])
# The German label of each endurance focus, as the calendar shows it (#4292).
FOCUS_LABELS_DE: Final = {
    "recovery": "Regeneration",
    "easy": "Locker",
    "endurance": "Grundlage",
    "long": "Lang",
    "tempo": "Tempo",
    "threshold": "Schwelle",
    "sweet_spot": "Sweet Spot",
    "vo2max": "VO2max",
    "anaerobic": "Anaerob",
    "race": "Wettkampf",
    "test": "Test",
}
STRENGTH_FOCI: Final = (
    "legs",
    "upper",
    "full_body",
    "core",
    "plyometrics",
    "mobility",
)
