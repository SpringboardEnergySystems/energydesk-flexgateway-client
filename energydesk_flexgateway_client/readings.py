"""Synthetic meter-reading generation, for exercising report ingestion.

Not aggregated: one data point per interval, shaped loosely like the
resource's type (ev_charger / solar / battery), with enough day-to-day and
intra-day variation to look like real site data rather than a flat line.

``generate_readings`` yields the physical profile of the resource. ``demand_kw`` turns a value into what is reported:
``DEMAND`` in kW, where **positive is consumption and negative is generation**, so a solar plant's production and a
battery's discharge are negative.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

Point = dict[str, Any]
ProfileFn = Callable[[float, random.Random, float], float]


def resource_type_of(resource: dict[str, Any]) -> str:
    """The resource's type: the ``RESOURCE_TYPE`` attribute of a 3.1.0 resource (an old-style ``resource_type`` field
    is still understood)."""
    for attribute in resource.get("attributes") or []:
        if attribute.get("type") == "RESOURCE_TYPE" and attribute.get("values"):
            return str(attribute["values"][0]).lower()
    return (resource.get("resource_type") or "").lower()


def demand_kw(resource_type: str, value: float) -> float:
    """A profile value as ``DEMAND`` in kW (positive = consumption). Solar produces and the battery profile is
    positive when discharging, so both are generation (negative demand); everything else consumes."""
    return round(-value if resource_type in ("solar", "battery") else value, 3)


def _round_down(moment: datetime, interval: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    seconds = (moment - epoch).total_seconds()
    step = interval.total_seconds()
    return epoch + timedelta(seconds=seconds - (seconds % step))


def _ev_charger_profile(hour: float, rng: random.Random, _day_factor: float) -> float:
    """Charging load: near-zero overnight, bursts around commute/work hours."""
    if 7 <= hour < 9 or 16 <= hour < 19:
        return max(0.0, rng.gauss(7.0, 2.0))
    if 9 <= hour < 16:
        return max(0.0, rng.gauss(2.0, 1.0))
    return max(0.0, rng.gauss(0.2, 0.2))


def _solar_profile(hour: float, rng: random.Random, day_factor: float) -> float:
    """Production: a daylight bell curve, 0 at night, scaled per-day (weather)."""
    if hour < 5 or hour > 21:
        return 0.0
    peak = 6.0 * day_factor
    width = 4.5
    value = peak * math.exp(-((hour - 13.0) ** 2) / (2 * width**2))
    return max(0.0, value + rng.gauss(0, 0.2))


def _battery_profile(hour: float, rng: random.Random, _day_factor: float) -> float:
    """Power flow: negative = charging (cheap night hours), positive = discharging (evening peak)."""
    if 1 <= hour < 5:
        return -max(0.0, rng.gauss(3.0, 1.0))
    if 17 <= hour < 21:
        return max(0.0, rng.gauss(3.0, 1.0))
    return rng.gauss(0, 0.3)


def _default_profile(hour: float, rng: random.Random, _day_factor: float) -> float:
    """Unknown resource_type: gentle smooth noise around a baseline."""
    return max(0.0, 1.0 + math.sin(hour / 24 * 2 * math.pi) + rng.gauss(0, 0.3))


_PROFILES: dict[str, ProfileFn] = {
    "ev_charger": _ev_charger_profile,
    "solar": _solar_profile,
    "battery": _battery_profile,
}


def generate_readings(
    resource: dict[str, Any],
    *,
    days: int = 30,
    interval_minutes: int = 15,
    end: datetime | None = None,
) -> list[Point]:
    """One point per interval over the trailing `days`, ending at `end`
    (default: now, rounded down to an interval boundary)."""
    interval = timedelta(minutes=interval_minutes)
    end = _round_down(end or datetime.now(timezone.utc), interval)
    start = end - timedelta(days=days)

    resource_type = resource_type_of(resource)
    profile = _PROFILES.get(resource_type, _default_profile)
    seed = resource.get("id") or resource.get("resourceName") or resource.get("external_resource_id") or resource_type
    rng = random.Random(seed)

    points: list[Point] = []
    ts = start
    current_day = None
    day_factor = 1.0
    while ts < end:
        if ts.date() != current_day:
            current_day = ts.date()
            day_factor = rng.uniform(0.4, 1.0)
        hour = ts.hour + ts.minute / 60.0
        value = profile(hour, rng, day_factor)
        points.append({"interval_start": ts.isoformat().replace("+00:00", "Z"), "value": round(value, 3)})
        ts += interval
    return points
