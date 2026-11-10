"""Receive a dispatch for a group, split it across the group's resources, and report what they did.

Springboard does not dispatch your individual resources: it dispatches your **group** (one site), and **one event arrives for the
group**, targeted `GROUP:<venName>/<groupName>`. The event carries one setpoint for the whole group; dividing it over the
resources is yours to do. This example shows the loop:

    python examples/handle_group_dispatch.py --group fjellkraft-bergen            # show the dispatches addressed to the group
    python examples/handle_group_dispatch.py --group fjellkraft-bergen --watch    # keep polling (every 30 s)
    python examples/handle_group_dispatch.py --group fjellkraft-bergen --report   # also report (simulated) delivery

The setpoint is **relative to your baseline, in kW; negative means up-regulation** (less consumption or more export), positive means
down-regulation (more consumption). A resource that is told nothing keeps to its own plan.

In a real integration replace `apply_setpoint` (tell the battery, the charger, the heat pump) and `read_baseline_and_usage`
(what the site actually did, from your meters). Without a public address, polling is all you need; `python -m flexclient listen`
shows the same events as they arrive, and a webhook subscription gets them without polling (see the README).
"""
import argparse
import re
import time
from datetime import datetime, timedelta

from _common import partner_client

from flexclient import FlexgatewayClient, groups, payloads

POLL_SECONDS = 30


def apply_setpoint(resource_name: str, kw: float, start: str, duration: str) -> None:
    """REPLACE with a command to the real device. Here we only print what would be asked of it."""
    print(f"    -> {resource_name}: {kw:+g} kW from {start} for {duration}")


def read_baseline_and_usage(resource: dict, requested_kw: float) -> tuple[float, float]:
    """REPLACE with your real baseline and metered consumption for the interval (both kW; consumption positive).

    The example invents a baseline of 30 % of the resource's rated power and assumes the resource delivered exactly what was asked."""
    baseline = round(0.3 * groups.resource_power_kw(resource), 3)
    return baseline, round(baseline + requested_kw, 3)


def seconds(duration: str) -> int:
    """An ISO 8601 time duration (PT15M, PT1H, PT90M, PT2H30M) in seconds."""
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not match or not any(match.groups()):
        raise ValueError(f"unsupported duration {duration!r}")
    hours, minutes, secs = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + secs


def report_delivery(client: FlexgatewayClient, event: dict, resource: dict, part_kw: float, setpoint: dict) -> None:
    """Report USAGE (kWh) and BASELINE (kW) per 15 minutes of the dispatch period for one resource, against the dispatch event.
    Named so that running it again replaces the report instead of adding a copy."""
    start = datetime.fromisoformat(setpoint["start"].replace("Z", "+00:00"))
    steps = max(1, seconds(setpoint["duration"]) // 900)
    baseline, usage_kw = read_baseline_and_usage(resource, part_kw)
    intervals = [payloads.interval(i, start + timedelta(minutes=15 * i), "PT15M", {"USAGE": round(usage_kw * 0.25, 4), "BASELINE": baseline})
                 for i in range(steps)]
    name = f"dispatch-{event['id']}-{resource['resourceName']}"
    body = payloads.report_request(event["id"], client.ven_name or "flexclient", resource["resourceName"], intervals, report_name=name)
    existing = {r["reportName"]: r["id"] for r in client.list_reports(event_id=event["id"]) if r.get("reportName")}
    sent = client.update_report(existing[name], body) if name in existing else client.create_report(body)
    print(f"    reported {len(intervals)} interval(s) for {resource['resourceName']} (report {sent['id']})")


def handle(client: FlexgatewayClient, group: dict, event: dict, resources_by_id: dict, report: bool) -> None:
    members = {resources_by_id[i]["resourceName"]: resources_by_id[i] for i in group["resource_ids"] if i in resources_by_id}
    weights = {name: groups.resource_power_kw(r) for name, r in members.items()}
    print(f"\n{event.get('eventName')}  (event {event['id']})")
    for setpoint in payloads.setpoints(event):
        meaning = "up-regulation" if setpoint["kw"] < 0 else "down-regulation"
        print(f"  {setpoint['start']} +{setpoint['duration']}: group setpoint {setpoint['kw']:+g} kW ({meaning})")
        try:
            parts = groups.split_setpoint(setpoint["kw"], weights)     # by rated power; use your own rule if you know better
        except ValueError as exc:
            print(f"    cannot split: {exc} (give the resources a MAX_POWER_EXPORT / MAX_POWER_CONSUMPTION)")
            continue
        for name, kw in sorted(parts.items()):
            apply_setpoint(name, kw, setpoint["start"], setpoint["duration"])
            if report:
                report_delivery(client, event, members[name], kw, setpoint)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", required=True, help="the groupName")
    parser.add_argument("--watch", action="store_true", help=f"keep polling every {POLL_SECONDS} s (default: look once)")
    parser.add_argument("--report", action="store_true", help="also report simulated delivery (USAGE and BASELINE) per resource")
    args = parser.parse_args()

    handled: set[tuple[str, str]] = set()
    with partner_client() as client:
        group = client.get_group(args.group)                          # carries the exact `target` events are addressed with
        resources_by_id = {r["id"]: r for r in client.list_resources()}
        print(f"watching {group['target']} ({len(group['resource_ids'])} resources); setpoints are relative to baseline, negative = up")
        while True:
            events = client.group_events(group)
            fresh = [e for e in events if (e["id"], str(e.get("modificationDateTime"))) not in handled]
            for event in fresh:
                handle(client, group, event, resources_by_id, args.report)
                handled.add((event["id"], str(event.get("modificationDateTime"))))
            if not events and not args.watch:
                print("no dispatch events for this group right now")
            if not args.watch:
                return 0
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
