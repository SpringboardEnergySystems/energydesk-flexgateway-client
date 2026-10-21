"""The client against a fake VTN. Every request the client builds, and every response the fake returns, is validated
against the vendored OpenADR 3.1.0 YAML, so the client cannot drift from the specification (and the fake cannot lie)."""
import base64
import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
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
            if method == "PUT":
                self.reports[parts[1]] = {**self.reports[parts[1]], **body}
                return 200, self.reports[parts[1]]
            items = [r for r in self.reports.values() if query.get("eventID") in (None, r["eventID"])]
            return 200, self.page(items, query)
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
    own = obj("EVENT", programID="system", eventName="metering", targets=["VEN:acme-ven"])
    assert payloads.metering_event([activation, metering, own]) is own, "no program: your own"
    assert payloads.metering_event([activation, metering]) is None, "a program's event is not yours"
    assert payloads.metering_event([own], "p") is None


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


# ── synthetic readings, reported to your own metering event ──────────────

from energydesk_flexgateway_client.readings import demand_kw, generate_readings, resource_type_of  # noqa: E402

OWN_EVENT = obj("EVENT", programID="system-metering", eventName="metering", targets=["VEN:acme-ven"],
                intervalPeriod={"start": "2026-01-01T00:00:00Z", "duration": "P9999Y"},
                reportDescriptors=[{"payloadType": "DEMAND", "units": "KW", "historical": True, "numIntervals": -1}])
HOURS = [(datetime(2026, 10, 18, 22, tzinfo=timezone.utc) + timedelta(hours=i), float(i)) for i in range(6)]  # spans two weeks


def test_the_generator_covers_the_period_with_no_gaps_and_solar_is_zero_at_night():
    end = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    points = generate_readings({"id": "r1", "attributes": [{"type": "RESOURCE_TYPE", "values": ["solar"]}]},
                               days=2, interval_minutes=15, end=end)
    assert len(points) == 2 * 24 * 4
    assert points[0]["interval_start"] == "2026-09-29T12:00:00Z" and points[-1]["interval_start"] == "2026-10-01T11:45:00Z"
    assert all(isinstance(p["value"], float) for p in points) and any(p["value"] == 0.0 for p in points)


def test_the_generator_is_deterministic_per_resource_and_reads_the_3_1_0_type():
    resource = {"id": "r1", "attributes": [{"type": "RESOURCE_TYPE", "values": ["battery"]}]}
    assert generate_readings(resource, days=1, interval_minutes=60) == generate_readings(resource, days=1, interval_minutes=60)
    assert resource_type_of(resource) == "battery" and resource_type_of({"resource_type": "Solar"}) == "solar"
    assert resource_type_of({}) == ""


def test_demand_is_positive_for_consumption_and_negative_for_generation():
    assert demand_kw("ev_charger", 7.0) == 7.0 and demand_kw("heat_pump", 1.5) == 1.5
    assert demand_kw("solar", 4.2) == -4.2, "production is generation"
    assert demand_kw("battery", 3.0) == -3.0 and demand_kw("battery", -3.0) == 3.0, "discharging generates, charging consumes"


def test_readings_group_into_one_report_per_week():
    groups = payloads.group_by_week(reversed(HOURS))
    assert [(w.date().isoformat(), [v for _, v in pts]) for w, pts in groups] == [
        ("2026-10-12", [0.0, 1.0]), ("2026-10-19", [2.0, 3.0, 4.0, 5.0])]


def test_readings_go_to_your_own_metering_event_one_report_per_week_and_a_resend_replaces():
    vtn, client = setup(events=[OWN_EVENT])
    client.ensure_ven()
    first = client.report_readings("site-1", HOURS, interval_minutes=60)
    assert [r["reportName"] for r in first] == ["readings-site-1-demand-2026-10-12", "readings-site-1-demand-2026-10-19"]
    assert {r["eventID"] for r in first} == {OWN_EVENT["id"]}
    assert [len(r["resources"][0]["intervals"]) for r in first] == [2, 4]
    assert first[0]["resources"][0]["intervals"][0]["intervalPeriod"] == {"start": "2026-10-18T22:00:00Z", "duration": "PT1H"}
    posts = [r for r in vtn.requests if r.method == "POST" and r.url.path.endswith("/reports")]
    again = client.report_readings("site-1", HOURS, interval_minutes=60)
    assert len(vtn.reports) == 2, "the same data again replaced the reports, it did not add copies"
    assert len([r for r in vtn.requests if r.method == "POST" and r.url.path.endswith("/reports")]) == len(posts)
    assert [r["id"] for r in again] == [r["id"] for r in first]


def test_usage_in_kwh_can_be_reported_the_same_way():
    vtn, client = setup(events=[OWN_EVENT])
    client.ensure_ven()
    (report,) = client.report_readings("site-1", HOURS[:2], payload_type="USAGE", interval_minutes=60)
    assert report["reportName"] == "readings-site-1-usage-2026-10-12"
    assert report["resources"][0]["intervals"][0]["payloads"] == [{"type": "USAGE", "values": [0.0]}]


def test_without_a_metering_event_it_says_what_is_missing_instead_of_guessing():
    vtn, client = setup(events=[obj("EVENT", programID="p", eventName="metering", targets=["PROGRAM:X"])])
    client.ensure_ven()
    with pytest.raises(ValueError, match="no metering event yet"):
        client.report_readings("site-1", HOURS)
    assert vtn.reports == {}, "a program's event is not yours and is not used"


def test_generate_then_report_a_month_for_every_registered_resource(tmp_path):
    vtn, client = setup(events=[OWN_EVENT])
    for name, kind in (("site-1", "solar"), ("charger-1", "ev_charger")):
        client.register_resource(payloads.resource_request(name, resource_type=kind, meter_point_id="1" + name[-1] * 5))
    out = tmp_path / "readings"
    generated = cli._generate_readings(client, out, days=30, interval_minutes=60)
    assert generated["files_written"] == 2 and sorted(p.name for p in out.glob("*.json")) == ["charger-1.json", "site-1.json"]
    solar = json.loads((out / "site-1.json").read_text())
    assert (solar["resourceName"], solar["payloadType"], solar["intervalMinutes"]) == ("site-1", "DEMAND", 60)
    assert len(solar["dataPoints"]) == 30 * 24 and max(p["value"] for p in solar["dataPoints"]) <= 0, "solar generates: negative"
    assert min(p["value"] for p in json.loads((out / "charger-1.json").read_text())["dataPoints"]) >= 0

    reported = cli._report_readings(client, out)
    assert (reported["files_reported"], reported["total_points"]) == (2, 2 * 30 * 24)
    assert reported["total_reports"] == len(vtn.reports) and 10 <= len(vtn.reports) <= 12   # about 5 weeks each
    assert {r["eventID"] for r in vtn.reports.values()} == {OWN_EVENT["id"]}
    cli._report_readings(client, out)
    assert len(vtn.reports) == reported["total_reports"], "running it again replaces: nothing piles up"


def test_the_commands_fail_clearly_when_there_is_nothing_to_do(tmp_path):
    vtn, client = setup(events=[OWN_EVENT])
    client.ensure_ven()
    with pytest.raises(RuntimeError, match="no resources registered"):
        cli._generate_readings(client, tmp_path / "x", 1, 60)
    with pytest.raises(RuntimeError, match="no .json files"):
        cli._report_readings(client, tmp_path)
    (tmp_path / "bad.json").write_text('{"resourceName": "x"}')
    with pytest.raises(ValueError, match="missing resourceName or dataPoints"):
        cli._report_readings(client, tmp_path)


def test_the_cli_runs_generate_and_report(monkeypatch, tmp_path, capsys):
    vtn, client = setup(events=[OWN_EVENT])
    client.register_resource(payloads.resource_request("site-1", resource_type="solar"))
    class Shared:                       # cli.run uses the client as a context manager and would close ours
        def __enter__(self):
            return client

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(cli, "FlexgatewayClient", lambda *a, **k: Shared())
    out = tmp_path / "r"
    assert cli.run(cli._parser().parse_args(["--access-token", "t", "generate-readings", "--out-dir", str(out),
                                             "--days", "2", "--interval-minutes", "60"])) == 0
    assert json.loads(capsys.readouterr().out)["files_written"] == 1
    assert cli.run(cli._parser().parse_args(["--access-token", "t", "report-readings", "--dir", str(out)])) == 0
    assert json.loads(capsys.readouterr().out)["total_points"] == 48
