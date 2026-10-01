"""Command-line interface for the Flexgateway reference client."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .client import FlexgatewayApiError, FlexgatewayClient, fetch_access_token
from .readings import generate_readings


def _load_json(path: str) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_resource(path: str) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError("resource payload must be a JSON object")
    return payload


def _load_resources(path: str) -> list[dict[str, Any]]:
    payload = _load_json(path)
    resources = payload if isinstance(payload, list) else [payload]
    if not resources or not all(isinstance(item, dict) for item in resources):
        raise ValueError("resource manifest must be a non-empty JSON object or array")
    return resources


def _load_report(path: str) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError("report payload must be a JSON object")
    return payload


def _load_data_points(path: str) -> list[dict[str, Any]]:
    payload = _load_json(path)
    points = payload if isinstance(payload, list) else [payload]
    if not points or not all(isinstance(item, dict) for item in points):
        raise ValueError("report-data file must be a non-empty JSON object or array")
    return points


def _resource_file_stem(resource: dict[str, Any]) -> str:
    raw = resource.get("external_resource_id") or resource.get("id") or "resource"
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in str(raw))


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _access_token(args: argparse.Namespace) -> str:
    ready_token = args.access_token or os.getenv("FLEXGATEWAY_ACCESS_TOKEN")
    if ready_token:
        return ready_token

    token_url = (
        args.token_url
        or os.getenv("OAUTHCHECK_ACCESS_TOKEN_OBTAIN_URL")
        or os.getenv("ENERGYDESK_TOKEN_URL")
    )
    client_id = (
        args.client_id
        or os.getenv("OAUTH_CLIENT_ID")
        or os.getenv("ENERGYDESK_CLIENT_ID")
    )
    client_secret = (
        args.client_secret
        or os.getenv("OAUTH_CLIENT_SECRET")
        or os.getenv("ENERGYDESK_CLIENT_SECRET")
    )
    scope = args.scope or os.getenv("OAUTH_SCOPE") or os.getenv("ENERGYDESK_SCOPE")
    missing = [
        name
        for name, value in (
            ("OAUTHCHECK_ACCESS_TOKEN_OBTAIN_URL", token_url),
            ("OAUTH_CLIENT_ID", client_id),
            ("OAUTH_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "provide FLEXGATEWAY_ACCESS_TOKEN or OAuth settings: "
            + ", ".join(missing)
        )
    return fetch_access_token(token_url, client_id, client_secret, scope=scope)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flexgateway-demo",
        description="Exercise the partner-facing EnergyDesk Flexgateway API",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("FLEXGATEWAY_BASE_URL", "http://localhost:8444/openadr3"),
    )
    parser.add_argument(
        "--access-token", help="Bearer token; preferably use the environment"
    )
    parser.add_argument("--token-url")
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--scope")

    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list-vens")

    register_resource = commands.add_parser("register-resource")
    register_resource.add_argument("--resource", required=True)

    commands.add_parser("list-resources")

    get_resource = commands.add_parser("get-resource")
    get_resource.add_argument("resource_id")

    create_report = commands.add_parser(
        "create-report", help="Declare a report against a registered resource"
    )
    create_report.add_argument("--report", required=True, help="report JSON file")

    commands.add_parser("list-reports")

    submit_report_data = commands.add_parser(
        "submit-report-data", help="Upsert meter readings against a report"
    )
    submit_report_data.add_argument("--report-id", required=True)
    submit_report_data.add_argument(
        "--data", required=True, help="JSON file: list of {interval_start, value}"
    )

    get_report_data = commands.add_parser("get-report-data")
    get_report_data.add_argument("report_id")
    get_report_data.add_argument("--start-time")
    get_report_data.add_argument("--end-time")

    generate_readings_cmd = commands.add_parser(
        "generate-readings",
        help="Write one JSON file per currently-registered resource with synthetic interval readings",
    )
    generate_readings_cmd.add_argument("--out-dir", required=True)
    generate_readings_cmd.add_argument("--days", type=int, default=30)
    generate_readings_cmd.add_argument("--interval-minutes", type=int, default=15)

    report_readings_cmd = commands.add_parser(
        "report-readings",
        help="Submit every *.json file in a directory (from generate-readings) as bulk report data",
    )
    report_readings_cmd.add_argument("--dir", required=True)

    demo = commands.add_parser("demo")
    resource_input = demo.add_mutually_exclusive_group(required=True)
    resource_input.add_argument(
        "--resources",
        help="JSON manifest containing one or more resource objects",
    )
    resource_input.add_argument(
        "--resource",
        help="single-resource JSON file (backwards-compatible alias)",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    token = _access_token(args)
    with FlexgatewayClient(args.base_url, token) as client:
        if args.command == "list-vens":
            _print_json(client.list_vens())
        elif args.command == "register-resource":
            resources = _load_resources(args.resource)
            registered = [client.register_resource(resource) for resource in resources]
            _print_json(registered[0] if len(registered) == 1 else registered)
        elif args.command == "list-resources":
            _print_json(client.list_resources())
        elif args.command == "get-resource":
            _print_json(client.get_resource(args.resource_id))
        elif args.command == "create-report":
            _print_json(client.create_report(_load_report(args.report)))
        elif args.command == "list-reports":
            _print_json(client.list_reports())
        elif args.command == "submit-report-data":
            data_points = _load_data_points(args.data)
            _print_json(client.submit_report_data(args.report_id, data_points))
        elif args.command == "get-report-data":
            _print_json(
                client.get_report_data(
                    args.report_id,
                    start_time=args.start_time,
                    end_time=args.end_time,
                )
            )
        elif args.command == "generate-readings":
            resources = client.list_resources()
            if not resources:
                raise RuntimeError("no resources registered for this VEN -- register some first")
            out_dir = Path(args.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            written = []
            for resource in resources:
                data_points = generate_readings(
                    resource, days=args.days, interval_minutes=args.interval_minutes
                )
                path = out_dir / f"{_resource_file_stem(resource)}.json"
                path.write_text(
                    json.dumps(
                        {
                            "resource_id": resource.get("id"),
                            "external_resource_id": resource.get("external_resource_id"),
                            "resource_name": resource.get("resource_name"),
                            "resource_type": resource.get("resource_type"),
                            "data_points": data_points,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                written.append({"file": str(path), "resource_id": resource.get("id"), "points": len(data_points)})
            _print_json({"status": "ok", "files_written": len(written), "files": written})
        elif args.command == "report-readings":
            in_dir = Path(args.dir)
            files = sorted(in_dir.glob("*.json"))
            if not files:
                raise RuntimeError(f"no .json files found in {in_dir}")
            results = []
            for path in files:
                payload = _load_json(str(path))
                resource_id = payload.get("resource_id")
                data_points = payload.get("data_points")
                if not resource_id or not data_points:
                    raise ValueError(f"{path}: missing resource_id or data_points")
                result = client.submit_report_data_bulk(resource_id, data_points)
                results.append({"file": str(path), **result})
            _print_json(
                {
                    "status": "ok",
                    "files_reported": len(results),
                    "total_entries_written": sum(r["entries_written"] for r in results),
                    "results": results,
                }
            )
        elif args.command == "demo":
            resources = _load_resources(args.resources or args.resource)
            vens = client.list_vens()
            if len(vens) != 1:
                raise RuntimeError(
                    f"expected exactly one assigned VEN, received {len(vens)}"
                )
            registered = []
            for resource in resources:
                first = client.register_resource(resource)
                second = client.register_resource(resource)
                if first.get("id") != second.get("id"):
                    external_id = resource.get("external_resource_id", "<unknown>")
                    raise RuntimeError(
                        "idempotency check failed for "
                        f"{external_id}: repeated registration changed ID"
                    )
                registered.append(first)
            visible = client.list_resources()
            visible_ids = {item.get("id") for item in visible}
            missing_ids = [
                item.get("id")
                for item in registered
                if item.get("id") not in visible_ids
            ]
            if missing_ids:
                raise RuntimeError(
                    f"{len(missing_ids)} registered resource(s) are missing from caller's list"
                )
            _print_json(
                {
                    "status": "ok",
                    "ven": vens[0],
                    "resources": registered,
                    "registered_resource_count": len(registered),
                    "idempotent_retry": True,
                    "visible_resource_count": len(visible),
                }
            )
        else:
            raise AssertionError(f"unsupported command: {args.command}")
    return 0


def main() -> int:
    load_dotenv()
    parser = _parser()
    try:
        return run(parser.parse_args())
    except (FlexgatewayApiError, OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
