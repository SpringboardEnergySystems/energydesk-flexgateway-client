"""Builders for the OpenADR 3.1.0 request bodies a VEN sends. Plain dicts in the specification's camelCase."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


def iso(moment: datetime) -> str:
    """UTC, with a ``Z``: the form the specification's examples use."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resource_request(
    resource_name: str,
    *,
    resource_type: str | None = None,
    meter_point_id: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    max_power_consumption_kw: float | None = None,
    max_power_export_kw: float | None = None,
    description: str | None = None,
    extra_attributes: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """A ``VEN_RESOURCE_REQUEST`` (without ``venID``: the client adds it).

    Attributes the VTN acts on:

    * ``METER_POINT_ID``: the meter point (GSRN) the resource sits on; the VTN looks up its balance responsible party;
    * ``RESOURCE_TYPE``: ``battery``, ``solar``, ``ev_charger`` and so on.

    The standard 3.1.0 attributes ``LOCATION`` (longitude first), ``MAX_POWER_CONSUMPTION`` and ``MAX_POWER_EXPORT`` (kW)
    and ``DESCRIPTION`` are stored as sent. ``BRP_ID`` and ``GRID_NODE_ID`` are added by the VTN when you read the
    resource back; do not send them.
    """
    attributes: list[dict[str, Any]] = []
    if meter_point_id:
        attributes.append({"type": "METER_POINT_ID", "values": [meter_point_id]})
    if resource_type:
        attributes.append({"type": "RESOURCE_TYPE", "values": [resource_type]})
    if latitude is not None and longitude is not None:
        attributes.append({"type": "LOCATION", "values": [longitude, latitude]})
    if max_power_consumption_kw is not None:
        attributes.append({"type": "MAX_POWER_CONSUMPTION", "values": [max_power_consumption_kw]})
    if max_power_export_kw is not None:
        attributes.append({"type": "MAX_POWER_EXPORT", "values": [max_power_export_kw]})
    if description:
        attributes.append({"type": "DESCRIPTION", "values": [description[:128]]})
    attributes.extend(dict(a) for a in extra_attributes)
    body: dict[str, Any] = {"objectType": "VEN_RESOURCE_REQUEST", "resourceName": resource_name}
    if attributes:
        body["attributes"] = attributes
    return body


def interval(interval_id: int, start: datetime, duration: str, payloads: Mapping[str, float | Sequence[float]]) -> dict[str, Any]:
    """One report interval. ``payloads`` maps a payload type to its value(s), e.g. ``{"USAGE": 1.5}``."""
    return {
        "id": interval_id,
        "intervalPeriod": {"start": iso(start), "duration": duration},
        "payloads": [
            {"type": payload_type, "values": [values] if isinstance(values, (int, float)) else list(values)}
            for payload_type, values in payloads.items()
        ],
    }


def report_request(
    event_id: str,
    client_name: str,
    resource_name: str,
    intervals: Iterable[Mapping[str, Any]],
    *,
    report_name: str | None = None,
) -> dict[str, Any]:
    """A ``reportRequest`` for one resource. Put several resources in ``resources`` yourself if you prefer one report."""
    body: dict[str, Any] = {
        "eventID": event_id,
        "clientName": client_name,
        "resources": [{"resourceName": resource_name, "intervals": [dict(i) for i in intervals]}],
    }
    if report_name:
        body["reportName"] = report_name
    return body


def metering_event(events: Iterable[Mapping[str, Any]], program_id: str) -> Mapping[str, Any] | None:
    """The program's long-running ``metering`` event: the one to report continuous and historical meter data against."""
    for event in events:
        if event.get("programID") == program_id and event.get("eventName") == "metering":
            return event
    return None


def setpoints(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``DISPATCH_SETPOINT_RELATIVE`` values of an activation event, one per interval.

    The value is the change of consumption against your baseline, in KW: **negative means less consumption or more
    export, which is up-regulation**. Returns ``[{"id", "start", "duration", "kw"}]`` with the event's interval period
    inherited where an interval gives none.
    """
    default = event.get("intervalPeriod") or {}
    found = []
    for item in event.get("intervals") or []:
        period = {**default, **(item.get("intervalPeriod") or {})}
        for payload in item.get("payloads", []):
            if payload.get("type") == "DISPATCH_SETPOINT_RELATIVE" and payload.get("values"):
                found.append({"id": item["id"], "start": period.get("start"), "duration": period.get("duration"),
                              "kw": float(payload["values"][0])})
    return found


def reservations(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``RESERVATION_UP`` / ``RESERVATION_DOWN`` capacity (KW) you must keep available, per interval."""
    default = event.get("intervalPeriod") or {}
    found = []
    for item in event.get("intervals") or []:
        period = {**default, **(item.get("intervalPeriod") or {})}
        values = {p["type"]: float(p["values"][0]) for p in item.get("payloads", [])
                  if p.get("type") in ("RESERVATION_UP", "RESERVATION_DOWN") and p.get("values")}
        if values:
            found.append({"id": item["id"], "start": period.get("start"), "duration": period.get("duration"),
                          "up_kw": values.get("RESERVATION_UP"), "down_kw": values.get("RESERVATION_DOWN")})
    return found
