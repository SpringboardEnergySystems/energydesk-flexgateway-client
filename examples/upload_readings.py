"""Upload (simulated) meter readings for your resources: the whole flow in one short script.

Run from the root of this repository, after filling in `.env` and registering resources (see the README):

    python examples/upload_readings.py --days 90 --dry-run          # show what would be sent
    python examples/upload_readings.py --days 90                    # send 90 days of 15-minute readings
    python examples/upload_readings.py --days 90 --name-prefix sim- # only resources named sim-*

Safe to repeat: readings go in as one report per resource and UTC week, named so that sending the same data again
replaces the report instead of adding a copy.

To send your own data, replace `read_my_meter_data` below. The rest stays as it is.
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, no install needed

from dotenv import load_dotenv

from flexclient import FlexgatewayClient, discover_token_url, fetch_access_token
from flexclient.readings import demand_kw, generate_readings, resource_type_of


def read_my_meter_data(resource: dict, days: int, interval_minutes: int) -> list[tuple[datetime, float]]:
    """Return `(interval start, kW)` for one resource. Timestamps must be timezone-aware.

    DEMAND is kW: positive = consumption, negative = generation (solar production, battery discharge).
    This example invents the data; a real integration would query your meter database or SCADA system here.
    """
    kind = resource_type_of(resource)
    return [
        (datetime.fromisoformat(p["interval_start"].replace("Z", "+00:00")), demand_kw(kind, p["value"]))
        for p in generate_readings(resource, days=days, interval_minutes=interval_minutes)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=90, help="days of history to send (default 90)")
    parser.add_argument("--interval-minutes", type=int, default=15, help="spacing of the readings (default 15)")
    parser.add_argument("--name-prefix", default="", help="only resources whose resourceName starts with this")
    parser.add_argument("--dry-run", action="store_true", help="show what would be sent, send nothing")
    args = parser.parse_args()

    load_dotenv()
    base_url = os.environ["FLEXGATEWAY_BASE_URL"]

    # 1. A token: a ready one, or the OAuth client credentials Springboard issued you.
    token = os.getenv("FLEXGATEWAY_ACCESS_TOKEN") or fetch_access_token(
        os.getenv("OAUTHCHECK_ACCESS_TOKEN_OBTAIN_URL") or discover_token_url(base_url),
        os.environ["OAUTH_CLIENT_ID"], os.environ["OAUTH_CLIENT_SECRET"], scope=os.getenv("OAUTH_SCOPE") or None)

    with FlexgatewayClient(base_url, token, ven_name=os.getenv("FLEXGATEWAY_VEN_NAME")) as client:
        # 2. Your registered resources (only those you may report for).
        resources = [r for r in client.list_resources() if r["resourceName"].startswith(args.name_prefix)]
        if not resources:
            print("No matching resources: register some first, e.g. "
                  "python -m flexclient register-resources --resources examples/transport-sites.json")
            return 1

        # 3. Readings in, one call per resource. They go against your own metering event; no program is needed.
        total = 0
        for resource in resources:
            points = read_my_meter_data(resource, args.days, args.interval_minutes)
            total += len(points)
            if args.dry_run:
                print(f"would send {len(points):5d} readings for {resource['resourceName']}")
                continue
            reports = client.report_readings(
                resource["resourceName"], points, payload_type="DEMAND", interval_minutes=args.interval_minutes)
            print(f"sent {len(points):5d} readings for {resource['resourceName']} in {len(reports)} weekly report(s)")
        print(f"{'would send' if args.dry_run else 'sent'} {total} readings for {len(resources)} resource(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
