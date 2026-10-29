"""Command-line interface for the Flexgateway reference client (OpenADR 3.1.0)."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .client import FlexgatewayApiError, FlexgatewayClient, discover_token_url, fetch_access_token
from .readings import demand_kw, generate_readings, resource_type_of
from .webhooks import make_server

DEFAULT_BASE_URL = "http://localhost:8444/openadr3"


def _load_json(path: str) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_objects(path: str) -> list[dict[str, Any]]:
    """A JSON object, or a non-empty array of objects."""
    payload = _load_json(path)
    items = payload if isinstance(payload, list) else [payload]
    if not items or not all(isinstance(item, dict) for item in items):
        raise ValueError(f"{path} must contain a JSON object or a non-empty array of objects")
    return items


def _redact(value: Any) -> Any:
    """Never echo a webhook bearer token to the terminal (it ends up in shell history and CI logs)."""
    if isinstance(value, dict):
        return {k: ("***" if k == "bearerToken" and v else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _print_json(payload: Any) -> None:
    print(json.dumps(_redact(payload), indent=2, sort_keys=True, default=str))


OPERATOR_COMMANDS = {"enroll"}   # operator actions: they need a BL client, not the VEN's own


def _access_token(args: argparse.Namespace) -> str:
    ready_token = args.access_token or os.getenv("FLEXGATEWAY_ACCESS_TOKEN")
    if ready_token:
        return ready_token

    operator = args.command in OPERATOR_COMMANDS and os.getenv("OPERATOR_CLIENT_ID") and os.getenv("OPERATOR_CLIENT_SECRET")
    if operator:
        client_id = args.client_id or os.getenv("OPERATOR_CLIENT_ID")
        client_secret = args.client_secret or os.getenv("OPERATOR_CLIENT_SECRET")
    else:
        client_id = args.client_id or os.getenv("OAUTH_CLIENT_ID") or os.getenv("ENERGYDESK_CLIENT_ID")
        client_secret = args.client_secret or os.getenv("OAUTH_CLIENT_SECRET") or os.getenv("ENERGYDESK_CLIENT_SECRET")
    missing = [n for n, v in (("OAUTH_CLIENT_ID", client_id), ("OAUTH_CLIENT_SECRET", client_secret)) if not v]
    if missing:
        raise ValueError("provide FLEXGATEWAY_ACCESS_TOKEN or OAuth settings: " + ", ".join(missing))
    # The VTN says where its token endpoint is; an explicit URL overrides that.
    token_url = (
        args.token_url
        or os.getenv("OAUTHCHECK_ACCESS_TOKEN_OBTAIN_URL")
        or os.getenv("ENERGYDESK_TOKEN_URL")
        or discover_token_url(args.base_url)
    )
    scope = args.scope or os.getenv("OAUTH_SCOPE") or os.getenv("ENERGYDESK_SCOPE")
    return fetch_access_token(token_url, client_id, client_secret, scope=scope)


def _webhook_token() -> str:
    token = os.getenv("WEBHOOK_BEARER_TOKEN")
    if not token:
        raise ValueError("set WEBHOOK_BEARER_TOKEN (a secret of your own choosing) in the environment or .env")
    return token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flexgateway-demo",
        description="Exercise the VEN-facing EnergyDesk Flexgateway API (OpenADR 3.1.0)",
    )
    parser.add_argument("--base-url", default=os.getenv("FLEXGATEWAY_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--ven-name", default=os.getenv("FLEXGATEWAY_VEN_NAME"),
                        help="name for the ven, used only the first time (the client creates it if it does not exist)")
    parser.add_argument("--access-token", help="bearer token; preferably use the environment")
    parser.add_argument("--token-url", help="default: asked from the VTN (GET /auth/server)")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret", help="prefer OAUTH_CLIENT_SECRET in the environment")
    parser.add_argument("--scope")

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="print where the VTN issues tokens (no authentication)")
    commands.add_parser("list-vens")
    commands.add_parser("ensure-ven", help="create this client's ven if it does not exist, and print it")

    register = commands.add_parser("register-resources", help="create or update resources (idempotent)")
    register.add_argument("--resources", required=True, help="JSON file: a VEN_RESOURCE_REQUEST or an array of them")
    commands.add_parser("list-resources")
    get_resource = commands.add_parser("get-resource")
    get_resource.add_argument("resource_id")

    commands.add_parser("list-programs", help="programs your resources are enrolled in (an operator/BL client sees all)")
    find = commands.add_parser("find-program", help="show the program whose name matches, e.g. 'Longflex Arm'")
    find.add_argument("name")
    enroll = commands.add_parser(
        "enroll",
        help="enroll this VEN's resources in a program (operator action: needs a BL client, see OPERATOR_CLIENT_ID)")
    enroll.add_argument("--program", required=True, help="program name or words of it, e.g. 'Longflex Arm'")
    which = enroll.add_mutually_exclusive_group(required=True)
    which.add_argument("--resource", action="append", metavar="NAME", help="a resourceName (repeat for several)")
    which.add_argument("--name-prefix", help="every resource whose resourceName starts with this, e.g. sim-")
    which.add_argument("--all", action="store_true", help="every resource of the VEN (--ven-name)")
    enroll.add_argument("--dry-run", action="store_true", help="show what would be enrolled, change nothing")
    events = commands.add_parser("list-events")
    events.add_argument("--program-id")
    events.add_argument("--active", action="store_true", help="only events running now")

    report = commands.add_parser("send-report", help="post a reportRequest")
    report.add_argument("--report", required=True, help="JSON file: a reportRequest")
    commands.add_parser("list-reports")

    subscribe = commands.add_parser("subscribe", help="receive notifications at an https callback")
    subscribe.add_argument("--callback-url", required=True)
    subscribe.add_argument("--objects", default="EVENT,PROGRAM", help="comma separated object types")
    subscribe.add_argument("--operations", default="CREATE,UPDATE,DELETE")
    subscribe.add_argument("--program-id")
    commands.add_parser("list-subscriptions")
    delete = commands.add_parser("delete-subscription")
    delete.add_argument("subscription_id")

    serve = commands.add_parser("serve-webhooks", help="run a development receiver that prints notifications")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9000)

    generate = commands.add_parser(
        "generate-readings",
        help="write one JSON file per registered resource with synthetic DEMAND readings (kW; consumption positive)")
    generate.add_argument("--out-dir", required=True)
    generate.add_argument("--days", type=int, default=30)
    generate.add_argument("--interval-minutes", type=int, default=15)
    generate.add_argument("--name-prefix", default="",
                          help="only resources whose resourceName starts with this (e.g. sim-): never invent data for real ones")

    report_readings = commands.add_parser(
        "report-readings",
        help="report every *.json file of generate-readings against your own metering event (no program needed; "
             "re-running replaces, never duplicates)")
    report_readings.add_argument("--dir", required=True)

    demo = commands.add_parser("demo", help="token, ven, resources (twice, to prove idempotency), programs, events")
    demo.add_argument("--resources", required=True, help="JSON file: a VEN_RESOURCE_REQUEST or an array of them")
    return parser


def _demo(client: FlexgatewayClient, resources: list[dict[str, Any]]) -> dict[str, Any]:
    ven = client.ensure_ven()
    registered = []
    for resource in resources:
        first = client.register_resource(resource)
        second = client.register_resource(resource)
        if first.get("id") != second.get("id"):
            raise RuntimeError(f"idempotency check failed for {resource['resourceName']}: the id changed")
        registered.append(first)
    visible = {item["id"] for item in client.list_resources()}
    missing = [item["id"] for item in registered if item["id"] not in visible]
    if missing:
        raise RuntimeError(f"{len(missing)} registered resource(s) are missing from the list")
    programs = client.list_programs()
    return {
        "status": "ok",
        "ven": ven,
        "resources": registered,
        "idempotent_retry": True,
        "visible_resource_count": len(visible),
        "programs": [{"id": p["id"], "programName": p["programName"]} for p in programs],
        "event_count": len(client.list_events()),
        "note": "No programs yet? The operator enrolls an approved resource in a program; it then appears here.",
    }


def run(args: argparse.Namespace) -> int:
    if args.command == "discover":
        print(discover_token_url(args.base_url))
        return 0
    if args.command == "serve-webhooks":
        server = make_server(args.host, args.port, _webhook_token(), lambda n: _print_json(n))
        print(f"listening on http://{args.host}:{args.port} (Ctrl-C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()
        return 0

    token = _access_token(args)
    with FlexgatewayClient(args.base_url, token, ven_name=args.ven_name, vtn_api_url=os.getenv("FLEXGATEWAY_VTN_API_URL")) as client:
        if args.command == "list-vens":
            _print_json(client.list_vens())
        elif args.command == "ensure-ven":
            _print_json(client.ensure_ven())
        elif args.command == "register-resources":
            registered = [client.register_resource(r) for r in _load_objects(args.resources)]
            _print_json(registered[0] if len(registered) == 1 else registered)
        elif args.command == "list-resources":
            _print_json(client.list_resources())
        elif args.command == "get-resource":
            _print_json(client.get_resource(args.resource_id))
        elif args.command == "list-programs":
            _print_json(client.list_programs())
        elif args.command == "find-program":
            _print_json(client.find_program(args.name))
        elif args.command == "enroll":
            _print_json(_enroll(client, args))
        elif args.command == "list-events":
            _print_json(client.list_events(args.program_id, active=True if args.active else None))
        elif args.command == "send-report":
            (report,) = _load_objects(args.report)
            _print_json(client.create_report(report))
        elif args.command == "list-reports":
            _print_json(client.list_reports())
        elif args.command == "subscribe":
            _print_json(client.subscribe(
                args.callback_url, client_name=args.ven_name or "flexgateway-demo",
                objects=tuple(args.objects.split(",")), operations=tuple(args.operations.split(",")),
                bearer_token=os.getenv("WEBHOOK_BEARER_TOKEN"), program_id=args.program_id))
        elif args.command == "list-subscriptions":
            _print_json(client.list_subscriptions())
        elif args.command == "delete-subscription":
            _print_json(client.delete_subscription(args.subscription_id))
        elif args.command == "generate-readings":
            _print_json(_generate_readings(client, Path(args.out_dir), args.days, args.interval_minutes, args.name_prefix))
        elif args.command == "report-readings":
            _print_json(_report_readings(client, Path(args.dir)))
        elif args.command == "demo":
            _print_json(_demo(client, _load_objects(args.resources)))
        else:
            raise AssertionError(f"unsupported command: {args.command}")
    return 0


def _enroll(client: FlexgatewayClient, args: argparse.Namespace) -> dict[str, Any]:
    """Enroll resources of ``--ven-name`` in the program that matches ``--program``. An operator action: the credentials
    must be a BL client's. Re-running is safe (an enrolled resource is reported as such)."""
    if not args.ven_name:
        raise ValueError("--ven-name (or FLEXGATEWAY_VEN_NAME) says whose resources to enroll")
    program = client.find_program(args.program)
    ven = next((v for v in client.list_vens() if v.get("venName") == args.ven_name), None)
    if ven is None:
        raise LookupError(f"no ven named {args.ven_name!r} is visible to these credentials")
    resources = [r for r in client.list_resources() if r.get("venID") == ven["id"]]
    if args.resource:
        unknown = sorted(set(args.resource) - {r["resourceName"] for r in resources})
        if unknown:
            raise LookupError(f"ven {args.ven_name!r} has no resource named: {', '.join(unknown)}")
        resources = [r for r in resources if r["resourceName"] in set(args.resource)]
    elif args.name_prefix:
        resources = [r for r in resources if r["resourceName"].startswith(args.name_prefix)]
    if not resources:
        raise LookupError(f"no resources of ven {args.ven_name!r} selected")
    results = []
    for resource in sorted(resources, key=lambda r: r["resourceName"]):
        entry = {"resourceName": resource["resourceName"], "resourceID": resource["id"]}
        if args.dry_run:
            entry["status"] = "would enroll"
        else:
            outcome = client.enroll_resource(program["id"], resource["id"])
            entry["status"] = "already enrolled" if outcome["already_enrolled"] else "enrolled"
        results.append(entry)
    return {"status": "ok", "dry_run": bool(args.dry_run), "program": {"id": program["id"], "programName": program["programName"]},
            "ven": args.ven_name, "count": len(results), "resources": results}


def _file_stem(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name) or "resource"


def _generate_readings(
    client: FlexgatewayClient, out_dir: Path, days: int, interval_minutes: int, name_prefix: str = "",
) -> dict[str, Any]:
    """One file per registered resource (only those whose name starts with ``name_prefix``): its ``resourceName`` and
    ``DEMAND`` readings in kW, consumption positive."""
    resources = [r for r in client.list_resources() if r["resourceName"].startswith(name_prefix)]
    if not resources:
        raise RuntimeError(f"no resources{f' starting with {name_prefix!r}' if name_prefix else ''} registered for this VEN: "
                           "register some first (demo, or register-resources)")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for resource in resources:
        kind = resource_type_of(resource)
        points = [{"interval_start": p["interval_start"], "value": demand_kw(kind, p["value"])}
                  for p in generate_readings(resource, days=days, interval_minutes=interval_minutes)]
        path = out_dir / f"{_file_stem(resource['resourceName'])}.json"
        path.write_text(json.dumps({
            "resourceName": resource["resourceName"], "resourceType": kind, "payloadType": "DEMAND",
            "intervalMinutes": interval_minutes, "dataPoints": points}, indent=2), encoding="utf-8")
        written.append({"file": str(path), "resourceName": resource["resourceName"], "points": len(points)})
    return {"status": "ok", "files_written": len(written), "files": written}


def _report_readings(client: FlexgatewayClient, in_dir: Path) -> dict[str, Any]:
    files = sorted(in_dir.glob("*.json"))
    if not files:
        raise RuntimeError(f"no .json files found in {in_dir}")
    results = []
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        name, points = payload.get("resourceName"), payload.get("dataPoints")
        if not name or not points:
            raise ValueError(f"{path}: missing resourceName or dataPoints")
        reports = client.report_readings(
            name, [(datetime.fromisoformat(p["interval_start"].replace("Z", "+00:00")), p["value"]) for p in points],
            payload_type=payload.get("payloadType", "DEMAND"), interval_minutes=payload.get("intervalMinutes", 15))
        results.append({"file": str(path), "resourceName": name, "points": len(points), "reports": len(reports)})
    return {"status": "ok", "files_reported": len(results), "total_points": sum(r["points"] for r in results),
            "total_reports": sum(r["reports"] for r in results), "results": results}


def main() -> int:
    load_dotenv()
    parser = _parser()
    try:
        return run(parser.parse_args())
    except (FlexgatewayApiError, OSError, ValueError, RuntimeError, LookupError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
