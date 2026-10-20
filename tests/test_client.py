"""The client against a fake VTN. Every request the client builds, and every response the fake returns, is validated
against the vendored OpenADR 3.1.0 YAML, so the client cannot drift from the specification (and the fake cannot lie)."""
import base64
import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from energydesk_flexgateway_client import (
    FlexgatewayApiError, FlexgatewayClient, cli, discover_token_url, fetch_access_token, payloads,
)
from energydesk_flexgateway_client.webhooks import make_server
from spec_contract import BASE, PREFIX, Contract, validate_component

EXAMPLES = cli.Path(__file__).resolve().parents[1] / "examples"
NOW = "2026-10-21T07:00:00Z"
CONTRACT = Contract()


def obj(kind, **fields):
    return {"id": str(uuid.uuid4()), "createdDateTime": NOW, "modificationDateTime": NOW, "objectType": kind, **fields}


class FakeVtn:
    """A tiny stateful VTN: one client, its ven, resources, programs and events it may see, reports, subscriptions."""

    def __init__(self, programs=(), events=()):
        self.client_id, self.ven, self.resources, self.reports, self.subscriptions = "client-1", None, {}, {}, {}
        self.programs, self.events = list(programs), list(events)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers.get("authorization") in (None, "Bearer partner-token"), "the token must be the one given"
        method, path = request.method, request.url.path[len(PREFIX):]
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        body = json.loads(request.content) if request.content else None
        if method != "GET" or path.startswith("/auth"):
            pass
        if path not in ("/auth/server",):
            CONTRACT.check_request(method, str(request.url), request.content)   # what we send is valid
        status, payload = self.route(method, path, query, body)
        if payload is not None:
            CONTRACT.check_response(method, str(request.url), status, payload, request.content)   # what we answer is valid
        return httpx.Response(status, json=payload) if payload is not None else httpx.Response(status)

    def page(self, items, query):
        skip, limit = int(query.get("skip", 0)), int(query.get("limit", len(items) or 1))
        return items[skip:skip + limit]

    def route(self, method, path, query, body):
        parts = path.strip("/").split("/")
        if path == "/auth/server":
            return 200, {"tokenURL": f"{BASE}{PREFIX}/auth/token"}
        if path == "/notifiers":
            return 200, {"WEBHOOK": True}
        if path == "/vens":
            if method == "GET":
                return 200, [self.ven] if self.ven else []
            assert not self.ven, "a ven must be created only once"
            self.ven = obj("VEN", clientID=self.client_id, venName=body["venName"], targets=[f"VEN:{body['venName']}"])
            return 201, self.ven
        if parts[0] == "resources":
            if len(parts) == 1 and method == "GET":
                items = [r for r in self.resources.values() if query.get("resourceName") in (None, r["resourceName"])]
                return 200, self.page(items, query)
            if method == "POST":
                assert body["venID"] == self.ven["id"]
                created = obj("RESOURCE", clientID=self.client_id, venID=self.ven["id"], resourceName=body["resourceName"],
                              attributes=body.get("attributes", []), targets=[])
                created["targets"] = [f"RESOURCE:{created['id']}"]
                self.resources[created["id"]] = created
                return 201, created
            if method == "PUT":
                updated = {**self.resources[parts[1]], "attributes": body.get("attributes", [])}
                self.resources[parts[1]] = updated
                return 200, updated
            return 200, self.resources[parts[1]]
        if parts[0] == "programs":
            return 200, self.page(self.programs, query)
        if parts[0] == "events":
            items = self.events
            if "programID" in query:
                items = [e for e in items if e["programID"] == query["programID"]]
            return 200, self.page(items, query)
        if parts[0] == "reports":
            if method == "POST":
                created = obj("REPORT", clientID=self.client_id, **body)
                self.reports[created["id"]] = created
                return 201, created
            return 200, self.page(list(self.reports.values()), query)
        if parts[0] == "subscriptions":
            if method == "POST":
                created = obj("SUBSCRIPTION", clientID=self.client_id, **body)
                self.subscriptions[created["id"]] = created
                return 201, created
            if method == "DELETE":
                return 200, self.subscriptions.pop(parts[1])
            return 200, self.page(list(self.subscriptions.values()), query)
        raise AssertionError(f"the fake VTN has no {method} {path}")


def program(n):
    return obj("PROGRAM", programName=f"PROGRAM_{n}", targets=[f"PROGRAM:PROGRAM_{n}"])


def setup(programs=(), events=(), ven_name="acme-ven"):
    vtn = FakeVtn(programs, events)
    client = FlexgatewayClient(f"{BASE}{PREFIX}/", "partner-token", ven_name=ven_name, transport=httpx.MockTransport(vtn))
    return vtn, client


# ── tokens ───────────────────────────────────────────────────────────────

def test_fetch_access_token_uses_client_credentials_without_exposing_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://auth.example/o/token/"
        assert request.headers["authorization"] == "Basic " + base64.b64encode(b"client-id:client-secret").decode()
        assert request.content == b"grant_type=client_credentials&scope=read_targets"
        return httpx.Response(200, json={"access_token": "secret-token", "token_type": "Bearer"})

    token = fetch_access_token("https://auth.example/o/token/", "client-id", "client-secret", scope="read_targets",
                               transport=httpx.MockTransport(handler))
    assert token == "secret-token"


def test_the_token_url_is_asked_from_the_vtn():
    vtn = FakeVtn()
    assert discover_token_url(f"{BASE}{PREFIX}", transport=httpx.MockTransport(vtn)) == f"{BASE}{PREFIX}/auth/token"


def test_token_errors_show_the_reason_and_never_the_secret():
    def handler(request):
        return httpx.Response(400, json={"type": "about:blank", "title": "Bad Request", "status": 400,
                                          "detail": "Invalid scope", "error": "invalid_scope"})

    with pytest.raises(FlexgatewayApiError) as caught:
        fetch_access_token("https://auth.example/o/token/", "id", "very-secret", scope="write_events",
                           transport=httpx.MockTransport(handler))
    assert caught.value.status_code == 400 and "Invalid scope" in str(caught.value)
    assert "very-secret" not in str(caught.value)


def test_an_empty_token_is_rejected():
    with pytest.raises(ValueError):
        FlexgatewayClient("https://gateway.example/openadr3", "")


# ── ven and resources ────────────────────────────────────────────────────

def test_the_ven_is_created_once_and_a_precreated_ven_is_used_under_its_own_name():
    vtn, client = setup()
    first = client.ensure_ven()
    assert first["venName"] == "acme-ven" and first["clientID"] == "client-1"
    assert client.ensure_ven() is first                       # remembered: no second request
    assert [r.method for r in vtn.requests].count("POST") == 1

    vtn2, client2 = setup(ven_name="not-used")
    vtn2.ven = obj("VEN", clientID="client-1", venName="created-by-springboard", targets=["VEN:created-by-springboard"])
    assert client2.ensure_ven()["venName"] == "created-by-springboard"
    assert not [r for r in vtn2.requests if r.method == "POST"]


def test_a_missing_ven_name_is_an_error_not_a_guess():
    _, client = setup(ven_name=None)
    with pytest.raises(ValueError, match="ven_name"):
        client.ensure_ven()


def test_registering_a_resource_creates_it_and_registering_again_updates_the_same_one():
    vtn, client = setup()
    body = payloads.resource_request("battery-1", resource_type="battery", meter_point_id="707057500000000001",
                                     latitude=60.39, longitude=5.32, max_power_consumption_kw=500, description="Battery")
    created = client.register_resource(body)
    again = client.register_resource({**body, "attributes": body["attributes"][:1]})
    assert again["id"] == created["id"] and len(vtn.resources) == 1
    sent = json.loads([r for r in vtn.requests if r.method == "POST" and r.url.path.endswith("/resources")][0].content)
    assert sent["venID"] == client.ensure_ven()["id"] and sent["objectType"] == "VEN_RESOURCE_REQUEST"
    assert {"type": "LOCATION", "values": [5.32, 60.39]} in sent["attributes"]          # longitude first
    assert "targets" not in sent                                                       # a VEN never sends granted targets
    assert [r.method for r in vtn.requests if r.url.path.endswith(f"/resources/{created['id']}")] == ["PUT"]


def test_resources_can_be_listed_and_fetched():
    vtn, client = setup()
    created = client.register_resource(payloads.resource_request("r1"))
    assert [r["id"] for r in client.list_resources()] == [created["id"]]
    assert client.get_resource(created["id"])["resourceName"] == "r1"


# ── reading: paging, programs, events ────────────────────────────────────

def test_lists_are_read_page_by_page():
    vtn, client = setup(programs=[program(n) for n in range(120)])
    assert len(client.list_programs()) == 120
    pages = [(r.url.params["skip"], r.url.params["limit"]) for r in vtn.requests if r.url.path.endswith("/programs")]
    assert pages == [("0", "50"), ("50", "50"), ("100", "50")]


def test_events_can_be_filtered_by_program():
    p1, p2 = program(1), program(2)
    events = [obj("EVENT", programID=p["id"], targets=p["targets"]) for p in (p1, p2, p2)]
    vtn, client = setup(programs=[p1, p2], events=events)
    assert len(client.list_events()) == 3 and len(client.list_events(p2["id"])) == 2
    client.list_events(active=True)
    assert vtn.requests[-1].url.params["active"] == "true"


# ── reports and event helpers ────────────────────────────────────────────

def test_a_report_built_with_the_helpers_is_valid_and_comes_back_as_posted():
    vtn, client = setup()
    event_id = str(uuid.uuid4())
    start = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)
    report = payloads.report_request(event_id, "acme-ven", "battery-1", [
        payloads.interval(0, start, "PT15M", {"USAGE": -0.05, "BASELINE": 0.5}),
        payloads.interval(1, start.replace(minute=15), "PT15M", {"USAGE": [-0.05]})], report_name="activation")
    created = client.create_report(report)
    assert created["resources"] == report["resources"] and created["clientID"] == "client-1"
    assert [r["id"] for r in client.list_reports()] == [created["id"]]
    assert report["resources"][0]["intervals"][0]["intervalPeriod"] == {"start": "2026-10-21T08:00:00Z", "duration": "PT15M"}


def test_event_helpers_read_setpoints_reservations_and_the_metering_event():
    activation = obj("EVENT", programID="p", targets=["PROGRAM:X"], intervalPeriod={"start": "2026-10-21T08:00:00Z", "duration": "PT15M"},
                     intervals=[{"id": 0, "payloads": [{"type": "DISPATCH_SETPOINT_RELATIVE", "values": [-200]}]},
                                {"id": 1, "intervalPeriod": {"start": "2026-10-21T08:15:00Z"},
                                 "payloads": [{"type": "DISPATCH_SETPOINT_RELATIVE", "values": [-150]}]}])
    assert payloads.setpoints(activation) == [
        {"id": 0, "start": "2026-10-21T08:00:00Z", "duration": "PT15M", "kw": -200.0},   # inherited from the event
        {"id": 1, "start": "2026-10-21T08:15:00Z", "duration": "PT15M", "kw": -150.0}]
    reservation = obj("EVENT", programID="p", targets=["PROGRAM:X"], intervals=[{"id": 0, "intervalPeriod": {
        "start": "2026-10-21T08:00:00Z", "duration": "PT1H"}, "payloads": [
        {"type": "RESERVATION_UP", "values": [300]}, {"type": "RESERVATION_DOWN", "values": [150]}]}])
    assert payloads.reservations(reservation) == [
        {"id": 0, "start": "2026-10-21T08:00:00Z", "duration": "PT1H", "up_kw": 300.0, "down_kw": 150.0}]
    metering = obj("EVENT", programID="p", eventName="metering", targets=["PROGRAM:X"])
    assert payloads.metering_event([activation, metering], "p") is metering
    assert payloads.metering_event([activation], "p") is None


# ── subscriptions ────────────────────────────────────────────────────────

def test_subscribe_list_and_delete():
    vtn, client = setup()
    assert client.notifiers() == {"WEBHOOK": True}
    created = client.subscribe("https://ven.acme.example/cb", client_name="acme-ven", bearer_token="my-own-secret")
    assert created["objectOperations"][0]["bearerToken"] == "my-own-secret"
    assert created["objectOperations"][0]["objects"] == ["EVENT", "PROGRAM"]
    assert [s["id"] for s in client.list_subscriptions()] == [created["id"]]
    assert client.delete_subscription(created["id"])["id"] == created["id"] and client.list_subscriptions() == []


# ── errors ───────────────────────────────────────────────────────────────

def test_api_errors_show_the_problem_detail_and_not_the_token():
    def handler(request):
        return httpx.Response(403, json={"type": "about:blank", "title": "Forbidden", "status": 403,
                                          "detail": "Token lacks the required scope 'write_events'"})

    with FlexgatewayClient("https://gateway.example/openadr3", "partner-token", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FlexgatewayApiError) as caught:
            client.list_vens()
    assert caught.value.status_code == 403 and "write_events" in caught.value.detail
    assert "partner-token" not in str(caught.value)


# ── webhook receiver ─────────────────────────────────────────────────────

def test_the_webhook_receiver_accepts_only_the_vtn_and_does_not_log_the_token(capsys):
    received = []
    server = make_server("127.0.0.1", 0, "my-own-secret", received.append)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/cb"
    notification = {"objectType": "EVENT", "operation": "CREATE", "object": obj("EVENT", programID="p", targets=["PROGRAM:X"]),
                    "targets": ["PROGRAM:X"]}
    validate_component("notification", notification)

    def post(token):
        request = urllib.request.Request(url, data=json.dumps(notification).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        return urllib.request.urlopen(request, timeout=5).status

    try:
        assert post("my-own-secret") == 204
        with pytest.raises(urllib.error.HTTPError) as caught:
            post("somebody-else")
        assert caught.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
    assert received == [notification]
    out = capsys.readouterr()
    assert "my-own-secret" not in out.out + out.err


# ── cli and examples ─────────────────────────────────────────────────────

def test_the_demo_registers_twice_and_reports_the_programs():
    p = program(1)
    vtn, client = setup(programs=[p])
    summary = cli._demo(client, cli._load_objects(str(EXAMPLES / "transport-sites.json")))
    assert summary["status"] == "ok" and summary["idempotent_retry"] and summary["visible_resource_count"] == 6
    assert summary["programs"] == [{"id": p["id"], "programName": "PROGRAM_1"}] and len(vtn.resources) == 6


def test_the_demo_fails_loudly_if_registration_is_not_idempotent():
    vtn, client = setup()
    original = vtn.route

    def forgetful(method, path, query, body):
        if method == "GET" and path == "/resources":
            return 200, []                                    # the VTN "forgets" the resource: a duplicate would be made
        return original(method, path, query, body)

    vtn.route = forgetful
    with pytest.raises(RuntimeError, match="idempotency"):
        cli._demo(client, [payloads.resource_request("r1")])


def test_the_examples_are_valid_3_1_0_requests():
    for resource in cli._load_objects(str(EXAMPLES / "transport-sites.json")):
        validate_component("resourceRequest", {**resource, "venID": str(uuid.uuid4())})
    (report,) = cli._load_objects(str(EXAMPLES / "report-activation.json"))
    validate_component("reportRequest", {**report, "eventID": str(uuid.uuid4())})


def test_the_cli_reads_the_token_from_the_environment_and_discovers_the_token_url(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "discover_token_url", lambda base: seen.setdefault("base", base) and "https://vtn.example/openadr3/auth/token")
    monkeypatch.setattr(cli, "fetch_access_token", lambda url, cid, secret, scope=None: seen.update(url=url, cid=cid, secret=secret) or "tok")
    for name in ("OAUTHCHECK_ACCESS_TOKEN_OBTAIN_URL", "ENERGYDESK_TOKEN_URL", "FLEXGATEWAY_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "secret-from-env")
    args = cli._parser().parse_args(["--base-url", "https://vtn.example/openadr3", "list-vens"])
    assert cli._access_token(args) == "tok"
    assert seen == {"base": "https://vtn.example/openadr3", "url": "https://vtn.example/openadr3/auth/token",
                    "cid": "cid", "secret": "secret-from-env"}
