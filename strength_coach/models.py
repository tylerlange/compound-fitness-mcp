"""Normalized data model that every source adapter maps into.

Adapters are responsible for producing these types with units already
normalized to pounds and dates as ISO ``YYYY-MM-DD`` strings, so downstream
code never has to know which app the data came from.
"""

from dataclasses import dataclass, field
from typing import Optional

# Normalized set-type vocabulary. Every source's own labels collapse to one of
# these. ``warmup`` is the one that matters for analytics (excluded from
# working-max math); ``failure`` marks AMRAP / to-failure / top sets.
SET_TYPE_STANDARD = "standard"
SET_TYPE_WARMUP = "warmup"
SET_TYPE_FAILURE = "failure"


@dataclass
class NormalizedSet:
    exercise_raw: str  # source-specific exercise name, pre-canonicalization
    seq: int  # 0-based order within the session (stable across re-exports)
    set_type: str = SET_TYPE_STANDARD
    weight_lb: Optional[float] = None
    reps: Optional[int] = None
    rir: Optional[float] = None  # reps-in-reserve; None when the source lacks it

    @property
    def is_warmup(self) -> bool:
        return self.set_type == SET_TYPE_WARMUP


@dataclass
class NormalizedSession:
    date: str  # ISO YYYY-MM-DD (display/query granularity)
    source: str  # macrofactor | stronglifts | liftin
    activity_type: str = "strength"
    workout: str = ""  # routine/day name (user-facing label)
    # Stable per-source session identity, used for dedup. Sources whose
    # (date, workout) can collide within a day (StrongLifts history id, Liftin
    # full timestamp) populate this; sources that can't tell same-day sessions
    # apart anyway (MacroFactor, date-only) leave it "".
    source_session_key: str = ""
    duration_s: Optional[int] = None
    notes: Optional[str] = None
    sets: list = field(default_factory=list)  # list[NormalizedSet]
    metrics: list = field(default_factory=list)  # list[tuple(metric, value, unit)]
    # Per-sample time series for sources that expose one (a connected bike's
    # performance graph). list[tuple(metric, unit, sample_seconds, offsets, values)] where
    # offsets are seconds since pedaling start and values may contain None for
    # samples the bike/strap dropped. Empty for every file-based source.
    streams: list = field(default_factory=list)


@dataclass
class ParsedImport:
    source: str
    filename: str
    sessions: list = field(default_factory=list)  # list[NormalizedSession]
    program_snapshot: Optional[dict] = None  # MacroFactor Active Program, else None


# Unit helper shared by adapters.
LB_PER_KG = 2.2046226218


def kg_to_lb(kg: float) -> float:
    return round(kg * LB_PER_KG, 2)
