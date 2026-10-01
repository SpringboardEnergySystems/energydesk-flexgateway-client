import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from energydesk_flexgateway_client import cli
from energydesk_flexgateway_client import (
    FlexgatewayApiError,
    FlexgatewayClient,
    fetch_access_token,
)
from energydesk_flexgateway_client.cli import _load_resource, _load_resources
from energydesk_flexgateway_client.readings import generate_readings


def test_fetch_access_token_uses_client_credentials_without_exposing_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://auth.example/o/token/"
        assert request.headers["authorization"] == "Basic " + base64.b64encode(
            b"client-id:client-secret"
        ).decode()
        assert request.content == b"grant_type=client_credentials&scope=read+write"
        return httpx.Response(200, json={"access_token": "secret-token"})

    token = fetch_access_token(
        "https://auth.example/o/token/",
        "client-id",
        "client-secret",
        scope="read write",
        transport=httpx.MockTransport(handler),
    )

    assert token == "secret-token"


def test_resource_registration_and_reads_send_bearer_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer partner-token"
        if request.method == "POST":
            return httpx.Response(201, json={"id": "resource-id"})
        return httpx.Response(200, json=[{"id": "resource-id"}])

    with FlexgatewayClient(
        "https://gateway.example/flexibility/openadr3/",
        "partner-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        created = client.register_resource({"external_resource_id": "partner-1"})
        resources = client.list_resources()

    assert created["id"] == "resource-id"
    assert resources == [{"id": "resource-id"}]
    assert requests[0].url == "https://gateway.example/flexibility/openadr3/resources"


def test_report_and_report_data_round_trip():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer partner-token"
        if request.url.path.endswith("/reports") and request.method == "POST":
            return httpx.Response(201, json={"id": "report-1", "report_name": "Usage"})
        if request.url.path.endswith("/reports") and request.method == "GET":
            return httpx.Response(200, json=[{"id": "report-1"}])
        if request.url.path.endswith("/report_data"):
            assert request.url.params["report_id"] == "report-1"
            return httpx.Response(
                201,
                json={"report_id": "report-1", "resource_id": "r1", "entries_written": 2, "series_id": 1},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with FlexgatewayClient(
        "https://gateway.example/flexibility/openadr3/",
        "partner-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        report = client.create_report({"report_name": "Usage", "resource_id": "r1", "report_type": "usage", "reading_type": "energy"})
        reports = client.list_reports()
        result = client.submit_report_data(
            "report-1",
            [{"interval_start": "2026-10-01T12:00:00Z", "value": 3.2}],
        )

    assert report["id"] == "report-1"
    assert reports == [{"id": "report-1"}]
    assert result["entries_written"] == 2


def test_api_error_preserves_status_and_safe_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "resource is not owned by caller"})

    with FlexgatewayClient(
        "https://gateway.example/openadr3",
        "partner-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(FlexgatewayApiError) as raised:
            client.get_resource("not-mine")

    assert raised.value.status_code == 403
    assert raised.value.detail == "resource is not owned by caller"
    assert "partner-token" not in str(raised.value)


def test_resource_manifest_loader_accepts_multiple_resources(tmp_path):
    manifest = tmp_path / "resources.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "external_resource_id": "site-1-charger",
                    "resource_type": "ev_charger",
                },
                {"external_resource_id": "site-1-battery", "resource_type": "battery"},
            ]
        ),
        encoding="utf-8",
    )

    resources = _load_resources(str(manifest))

    assert [item["resource_type"] for item in resources] == [
        "ev_charger",
        "battery",
    ]
    with pytest.raises(ValueError, match="JSON object"):
        _load_resource(str(manifest))


def test_register_resource_command_accepts_a_manifest(monkeypatch, tmp_path, capsys):
    manifest = tmp_path / "resources.json"
    manifest.write_text(
        json.dumps(
            [
                {"external_resource_id": "charger-1"},
                {"external_resource_id": "battery-1"},
            ]
        ),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def register_resource(self, resource):
            return {"id": "created-" + resource["external_resource_id"]}

    monkeypatch.setattr(cli, "FlexgatewayClient", FakeClient)
    args = cli._parser().parse_args(
        [
            "--access-token",
            "test-token",
            "register-resource",
            "--resource",
            str(manifest),
        ]
    )

    assert cli.run(args) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"id": "created-charger-1"},
        {"id": "created-battery-1"},
    ]


def test_generate_readings_covers_the_full_period_with_no_gaps():
    end = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    points = generate_readings(
        {"id": "r1", "resource_type": "solar"},
        days=2,
        interval_minutes=15,
        end=end,
    )

    assert len(points) == 2 * 24 * 4
    assert points[0]["interval_start"] == "2026-09-29T12:00:00Z"
    assert points[-1]["interval_start"] == "2026-10-01T11:45:00Z"
    assert all(isinstance(p["value"], float) for p in points)
    # solar must be exactly zero overnight
    assert any(p["value"] == 0.0 for p in points)


def test_generate_readings_is_deterministic_for_the_same_resource():
    resource = {"id": "r1", "resource_type": "battery"}
    first = generate_readings(resource, days=1, interval_minutes=60)
    second = generate_readings(resource, days=1, interval_minutes=60)
    assert first == second


def test_generate_and_report_readings_commands(monkeypatch, tmp_path, capsys):
    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def list_resources(self):
            return [{"id": "r1", "external_resource_id": "site-1", "resource_type": "solar"}]

        def submit_report_data_bulk(self, resource_id, data_points):
            return {
                "resource_id": resource_id,
                "entries_written": len(data_points),
                "series_id": 1,
            }

    monkeypatch.setattr(cli, "FlexgatewayClient", FakeClient)
    out_dir = tmp_path / "readings"

    gen_args = cli._parser().parse_args(
        [
            "--access-token", "test-token",
            "generate-readings", "--out-dir", str(out_dir),
            "--days", "1", "--interval-minutes", "60",
        ]
    )
    assert cli.run(gen_args) == 0
    gen_output = json.loads(capsys.readouterr().out)
    assert gen_output["files_written"] == 1
    written_files = list(out_dir.glob("*.json"))
    assert len(written_files) == 1
    assert written_files[0].name == "site-1.json"

    report_args = cli._parser().parse_args(
        ["--access-token", "test-token", "report-readings", "--dir", str(out_dir)]
    )
    assert cli.run(report_args) == 0
    report_output = json.loads(capsys.readouterr().out)
    assert report_output["files_reported"] == 1
    assert report_output["total_entries_written"] == 24
