"""Resource groups, available flex and group dispatch: the pieces a partner writes around the three calls.

A **group** is your reporting and dispatch unit (usually one site). You **push its available flex** every 15 minutes as a
forecast, and when Springboard dispatches, **one event arrives for the group**, targeted ``GROUP:<venName>/<groupName>``; you
split its setpoint across the group's resources yourself (``split_setpoint``) and report what they did.

Plain functions over plain dicts, no network: ``FlexgatewayClient`` does the calls (``put_group``, ``push_availability``,
``group_events``).
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

INTERVAL = "PT15M"
SLICE = timedelta(minutes=15)
FORECAST, DOWN, UP = "USAGE_FORECAST", "DOWN_REGULATION_AVAILABLE", "UP_REGULATION_AVAILABLE"
PAYLOAD_TYPES = (FORECAST, DOWN, UP)
DEFAULT_HORIZON_HOURS = 36          # the rolling push: from now to now + 36 h
MAX_PUSH_INTERVALS = 4 * 24 * 7     # the server takes at most a week in one push


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{moment!r} has no timezone: use an aware datetime (UTC offsets matter, the server works in UTC)")
    return moment.astimezone(timezone.utc)


def quarter_floor(moment: datetime) -> datetime:
    """The start of the 15-minute interval ``moment`` falls in, in UTC."""
    moment = _utc(moment)
    return moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)


def rolling_window(now: datetime | None = None, hours: float = DEFAULT_HORIZON_HOURS) -> list[datetime]:
    """The interval starts a rolling push covers: the current quarter hour up to ``hours`` ahead (default 36 h = 144 intervals).
    Push this every 15 minutes, with your latest plan, and straight away when the plan changes."""
    first = quarter_floor(now or datetime.now(timezone.utc))
    return [first + SLICE * i for i in range(int(hours * 4))]


# ── the push ─────────────────────────────────────────────────────────────────

def availability_interval(start: datetime, *, forecast_kw: float, down_kw: float, up_kw: float) -> dict[str, Any]:
    """One 15-minute interval of the push. All three are **kW, the average over the interval, not negative**.

    * ``forecast_kw`` (``USAGE_FORECAST``): what the group will use according to your own plan; the availability only makes sense
      next to it, and it is the cheapest check on your forecasting.
    * ``down_kw`` (``DOWN_REGULATION_AVAILABLE``): how much the group can **increase** consumption (or reduce generation) on request.
    * ``up_kw`` (``UP_REGULATION_AVAILABLE``): how much it can **decrease** consumption (or increase generation).

    ``start`` must be on a UTC quarter hour (``quarter_floor`` gets you there).
    """
    start = _utc(start)
    if start != quarter_floor(start):
        raise ValueError(f"{start:%Y-%m-%dT%H:%M:%S}Z is not on a UTC quarter hour")
    out: dict[str, Any] = {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ")}
    for name, value in ((FORECAST, forecast_kw), (DOWN, down_kw), (UP, up_kw)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a number of kW, not negative: got {value!r}")
        out[name] = float(value)
    return out


def availability_body(intervals: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The request body of a push. Checks what the server would refuse, so a mistake shows before the call."""
    rows = [dict(i) for i in intervals]
    if not rows:
        raise ValueError("a push needs at least one interval")
    if len(rows) > MAX_PUSH_INTERVALS:
        raise ValueError(f"{len(rows)} intervals in one push; at most {MAX_PUSH_INTERVALS} (a week). A rolling push is 36 hours.")
    for row in rows:
        availability_interval(datetime.fromisoformat(str(row.get("start", "")).replace("Z", "+00:00")),
                              forecast_kw=row.get(FORECAST), down_kw=row.get(DOWN), up_kw=row.get(UP))
    return {"interval": INTERVAL, "intervals": rows}


def synthetic_availability(
    now: datetime | None = None, hours: float = DEFAULT_HORIZON_HOURS, *, down_kw: float = 600.0, up_kw: float = 300.0,
    forecast_kw: float = 800.0, seed: int | None = None,
) -> list[dict[str, Any]]:
    """An invented but plausible rolling plan for demos: the forecast follows a day curve (low at night, an evening peak) and the
    availability is a share of the configured capacity that shrinks at the peak. Replace with your own planning."""
    rng = random.Random(seed)
    rows = []
    for start in rolling_window(now, hours):
        hour = start.hour + start.minute / 60
        shape = 0.55 + 0.45 * math.sin((hour - 6) / 24 * 2 * math.pi) ** 2 + rng.uniform(-0.03, 0.03)
        rows.append(availability_interval(
            start, forecast_kw=round(forecast_kw * shape, 1), down_kw=round(down_kw * (1.25 - shape * 0.5), 1),
            up_kw=round(up_kw * (1.1 - shape * 0.4), 1)))
    return rows


# ── receiving a group dispatch ───────────────────────────────────────────────

def events_for_group(events: Iterable[Mapping[str, Any]], group: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The events addressed to ``group`` (a group as returned by ``get_group``/``put_group``): those whose targets hold the
    group's ``target``. Use the ``target`` the server gives you; never build the string yourself."""
    wanted = group.get("target")
    if not wanted:
        raise ValueError("the group has no target: pass a group as the server returned it")
    return [dict(e) for e in events if wanted in (e.get("targets") or [])]


def resource_power_kw(resource: Mapping[str, Any]) -> float:
    """The larger of a resource's ``MAX_POWER_EXPORT`` and ``MAX_POWER_CONSUMPTION`` (kW): a natural weight to split by.
    0.0 when it declares neither."""
    best = 0.0
    for attribute in resource.get("attributes") or []:
        if attribute.get("type") in ("MAX_POWER_EXPORT", "MAX_POWER_CONSUMPTION") and attribute.get("values"):
            try:
                best = max(best, float(attribute["values"][0]))
            except (TypeError, ValueError):
                pass
    return best


def split_setpoint(setpoint_kw: float, weights: Mapping[str, float]) -> dict[str, float]:
    """Split a group's dispatch setpoint over its resources **in proportion to ``weights``** (resource name -> weight, e.g.
    ``resource_power_kw``). The parts keep the sign of the setpoint (negative = up-regulation) and add up to it exactly
    (millis of a kW; the remainder goes to the heaviest resource). Weights of zero get nothing; all zero is an error.

    Springboard dispatches the group as a whole and does not care how you divide it; this is one reasonable way. You may
    know better (a battery at 20 % cannot discharge, a charger is idle): use your own rule, as long as the group delivers the sum.
    """
    positive = {name: float(w) for name, w in weights.items() if w and w > 0}
    if not positive:
        raise ValueError("no resource has a positive weight to split by")
    total = sum(positive.values())
    parts = {name: round(setpoint_kw * w / total, 3) for name, w in positive.items()}
    heaviest = max(positive, key=positive.get)
    parts[heaviest] = round(parts[heaviest] + (round(setpoint_kw, 3) - round(sum(parts.values()), 3)), 3)
    return parts


def describe_dispatch(event: Mapping[str, Any], weights: Mapping[str, float] | None = None) -> list[str]:
    """Readable lines for a group dispatch event: the period, the setpoint (and what it means), and with ``weights`` (resource
    name -> weight) how ``split_setpoint`` would divide it."""
    from . import payloads
    lines = [f"{event.get('eventName', event.get('id'))}  targets={','.join(event.get('targets') or [])}"]
    for item in payloads.setpoints(dict(event)):
        meaning = "up-regulation (less consumption / more export)" if item["kw"] < 0 else "down-regulation (more consumption)"
        lines.append(f"  {item['start']} +{item['duration']}  setpoint {item['kw']:g} kW  {meaning}")
        if weights:
            lines.extend(f"      {name}: {kw:g} kW" for name, kw in sorted(split_setpoint(item["kw"], weights).items()))
    return lines
