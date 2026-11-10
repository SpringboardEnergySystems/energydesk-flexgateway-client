"""Push the available flex of a group: the rolling forecast Springboard dispatches against.

Run from the root of this repository, after registering your resources and making a group (see docs/groups-and-dispatch.md):

    python examples/push_availability.py --group fjellkraft-bergen --once --dry-run   # show what would be sent
    python examples/push_availability.py --group fjellkraft-bergen --once            # one push now
    python examples/push_availability.py --group fjellkraft-bergen                   # every 15 minutes, until Ctrl-C

What you push, per 15-minute interval from now to 36 hours ahead, in kW (the average over the interval, never negative):

    USAGE_FORECAST              what the group will use according to your own plan
    DOWN_REGULATION_AVAILABLE   how much it can INCREASE consumption if asked
    UP_REGULATION_AVAILABLE     how much it can DECREASE consumption (or increase generation) if asked

Safe to repeat: every pushed interval replaces the stored one for that start, so push as often as your plan changes (every 15
minutes, and straight away when something changes), and the same push twice is harmless. A push is all or nothing.

To send your own plan, replace `read_my_plan` below. The rest stays as it is.
"""
import argparse
import time
import zlib
from datetime import datetime, timezone

from _common import partner_client

from flexclient import FlexgatewayApiError, groups


def read_my_plan(group: dict, now: datetime, hours: float) -> list[dict]:
    """Return one `groups.availability_interval(...)` per 15 minutes from the current quarter hour, `hours` ahead.

    This example invents the numbers; a real integration would ask your planning or optimisation system what the site will do
    and how much of it could be shifted, per interval. Availability is only meaningful next to the forecast: a battery that your
    own plan already discharges at full power has no UP_REGULATION_AVAILABLE left.
    """
    return groups.synthetic_availability(now, hours, seed=zlib.crc32(group["group_name"].encode()))


def push_once(client, group_name: str, hours: float, dry_run: bool) -> bool:
    group = client.get_group(group_name)
    if not group["resource_ids"]:
        print(f"note: {group_name} has no members yet; the push is accepted, but the group is eligible for nothing until it has some")
    rows = read_my_plan(group, datetime.now(timezone.utc), hours)
    stamp = datetime.now().strftime("%H:%M:%S")
    first, last = rows[0]["start"], rows[-1]["start"]
    if dry_run:
        print(f"{stamp} would push {len(rows)} intervals for {group_name}, {first} .. {last}; first: {rows[0]}")
        return True
    try:
        client.push_availability(group_name, rows)
    except FlexgatewayApiError as exc:
        if exc.status_code == 422:      # our own plan is wrong: say exactly how, and stop (retrying the same data cannot help)
            print(f"{stamp} the push was refused, nothing stored:")
            for problem in exc.problem.get("errors", [exc.detail]):
                print(f"    {problem}")
            return False
        print(f"{stamp} push failed ({exc.status_code or 'no answer'}): {exc.detail}; trying again next quarter hour")
        return True
    print(f"{stamp} pushed {len(rows)} intervals for {group_name}, {first} .. {last}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", required=True, help="the groupName (see: python -m flexclient list-groups)")
    parser.add_argument("--hours", type=float, default=groups.DEFAULT_HORIZON_HOURS, help="how far ahead to forecast (default 36)")
    parser.add_argument("--once", action="store_true", help="push once and stop (default: every 15 minutes)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be sent, send nothing")
    args = parser.parse_args()

    with partner_client() as client:
        while True:
            if not push_once(client, args.group, args.hours, args.dry_run):
                return 2
            if args.once:
                return 0
            # wake a few seconds after the next quarter hour, so the interval we just entered is already in the plan
            now = time.time()
            time.sleep(900 - now % 900 + 5)


if __name__ == "__main__":
    raise SystemExit(main())
