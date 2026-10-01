import base64
import json

import httpx
import pytest

from energydesk_flexgateway_client import cli
from energydesk_flexgateway_client import (
    FlexgatewayApiError,
    FlexgatewayClient,
    fetch_access_token,
)
from energydesk_flexgateway_client.cli import _load_resource, _load_resources


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
