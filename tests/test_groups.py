"""Resource groups, available flex and group dispatch on the partner side (/api/vtn), plus the examples' building blocks.

A small fake VTN, not the spec-validating one of test_client.py: groups and dispatch are deliberately not OpenADR operations.
It enforces what the real server does for a partner: groups are per VEN, a resource is in one group, members share a meter point,
and an availability push is all or nothing on UTC quarter hours."""
import argparse
import importlib
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from flexclient import FlexgatewayApiError, FlexgatewayClient, cli, groups, listen, payloads

ROOT = "https://vtn.example.test/flexibility"
BASE = f"{ROOT}/openadr3"
VEN = {"id": str(uuid.uuid4()), "venName": "fjellkraft-ven"}


def resource(name, meter, *, export=None, consumption=None):
    attributes = [{"type": "METER_POINT_ID", "values": [meter]}]
    if export:
        attributes.append({"type": "MAX_POWER_EXPORT", "values": [export]})
    if consumption:
        attributes.append({"type": "MAX_POWER_CONSUMPTION", "values": [consumption]})
    return {"id": str(uuid.uuid4()), "venID": VEN["id"], "resourceName": name, "attributes": attributes}


RESOURCES = [resource("bergen-ev", "MP1", consumption=1200), resource("bergen-battery", "MP1", export=500, consumption=500),
             resource("bergen-solar", "MP1", export=350), resource("oslo-battery", "MP2", export=750, consumption=750)]
BY_NAME = {r["resourceName"]: r for r in RESOURCES}
START = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)


class Vtn:
    def __init__(self):
        self.groups, self.availability, self.requests, self.events, self.reports = {}, {}, [], [], {}
        self.in_portfolio = False

    def body(self, request):
        return json.loads(request.content) if request.content else None

    def view(self, name):
        g = self.groups[name]
        return {"group_name": name, "group_id": g["id"], "description": g["description"], "resource_ids": sorted(g["resource_ids"]),
                "target": f"GROUP:{VEN['venName']}/{name}", "location": {"meterpoint_id": "MP1", "latitude": None, "longitude": None, "grid_node_id": None},
                "eligible_programs": [], "ineligible_programs": [{"program": "DIRECT_ACTIVATION", "missing_resource_ids": g["resource_ids"][:1]}]}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path[len("/flexibility"):], request.method
        data = self.body(request)
        if path == "/openadr3/resources" and method == "GET":
            return httpx.Response(200, json=RESOURCES[int(request.url.params.get("skip", 0)):])
        if path == "/openadr3/vens" and method == "GET":
            return httpx.Response(200, json=[VEN])
        if path == "/openadr3/events" and method == "GET":
            return httpx.Response(200, json=self.events)
        if path == "/openadr3/reports":
            if method == "GET":
                return httpx.Response(200, json=list(self.reports.values()))
            report = {**data, "id": str(uuid.uuid4())}
            self.reports[report["reportName"]] = report
            return httpx.Response(201, json=report)
        if path.startswith("/openadr3/reports/") and method == "PUT":
            self.reports[data["reportName"]] = {**data, "id": path.rsplit("/", 1)[1]}
            return httpx.Response(200, json=self.reports[data["reportName"]])
        if path == "/api/vtn/groups" and method == "GET":
            return httpx.Response(200, json=[self.view(n) for n in sorted(self.groups)])
        parts = path.split("/")
        if len(parts) >= 5 and parts[:4] == ["", "api", "vtn", "groups"]:
            name = parts[4].replace("%20", " ")
            if len(parts) == 6 and parts[5] == "availability":
                return self.availability_call(name, method, data, request)
            return self.group_call(name, method, data)
        return httpx.Response(404, json={"detail": f"unexpected {method} {path}"})

    def group_call(self, name, method, data):
        if method == "GET":
            return httpx.Response(200, json=self.view(name)) if name in self.groups else httpx.Response(404, json={"detail": "Group not found"})
        if method == "DELETE":
            if name not in self.groups:
                return httpx.Response(404, json={"detail": "Group not found"})
            if self.in_portfolio:
                return httpx.Response(409, json={"detail": "The group is in use by Springboard: ask Springboard to release it"})
            del self.groups[name]
            return httpx.Response(204)
        ids = data["resource_ids"]
        clash = [{"resource_id": i, "group_name": n} for n, g in self.groups.items() if n != name for i in ids if i in g["resource_ids"]]
        if clash:
            return httpx.Response(409, json={"title": "Conflict", "detail": "Some resources are already members of another group", "conflicts": clash})
        meters = sorted({a["values"][0] for r in RESOURCES if r["id"] in ids for a in r["attributes"] if a["type"] == "METER_POINT_ID"})
        if len(meters) > 1:
            return httpx.Response(422, json={"title": "Unprocessable Entity", "detail": "The members of a group must share one meter point", "meterpoint_ids": meters})
        created = name not in self.groups
        self.groups[name] = {"id": self.groups[name]["id"] if not created else str(uuid.uuid4()), "description": data["description"], "resource_ids": list(ids)}
        return httpx.Response(201 if created else 200, json=self.view(name))

    def availability_call(self, name, method, data, request):
        if name not in self.groups:
            return httpx.Response(404, json={"detail": "Group not found"})
        if method == "GET":
            return httpx.Response(200, json={"interval": "PT15M", "intervals": [self.availability[k] for k in sorted(self.availability)]})
        errors = []
        if data.get("interval") != "PT15M":
            errors.append("interval must be 'PT15M'")
        for i, row in enumerate(data.get("intervals") or []):
            moment = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
            if moment.minute % 15 or moment.second:
                errors.append(f"intervals[{i}]: start is not on a UTC quarter hour")
            if any(row[k] < 0 for k in groups.PAYLOAD_TYPES):
                errors.append(f"intervals[{i}]: must not be negative")
        if errors:
            return httpx.Response(422, json={"title": "Unprocessable Entity", "detail": errors[0], "errors": errors})
        for row in data["intervals"]:
            self.availability[row["start"]] = row
        return httpx.Response(204)


def client(vtn):
    return FlexgatewayClient(BASE, "token", ven_name="fjellkraft-ven", transport=httpx.MockTransport(vtn))


def ids(*names):
    return [BY_NAME[n]["id"] for n in names]


def sent(vtn, method):
    return [r for r in vtn.requests if r.method == method and "/api/vtn" in r.url.path]


# ── groups ───────────────────────────────────────────────────────────────────

def test_a_group_is_created_then_replaced_and_the_call_is_idempotent():
    vtn = Vtn()
    with client(vtn) as c:
        first = c.put_group("fjellkraft-bergen", ids("bergen-ev", "bergen-battery"), description="Bergen site")
        again = c.put_group("fjellkraft-bergen", ids("bergen-ev", "bergen-battery"), description="Bergen site")
        grown = c.put_group("fjellkraft-bergen", ids("bergen-ev", "bergen-battery", "bergen-solar"))
    assert (first["created"], again["created"], grown["created"]) == (True, False, False)
    assert first["group_id"] == again["group_id"] == grown["group_id"] and first["target"] == "GROUP:fjellkraft-ven/fjellkraft-bergen"
    put = sent(vtn, "PUT")[0]
    assert str(put.url) == f"{ROOT}/api/vtn/groups/fjellkraft-bergen"                       # beside the OpenADR prefix
    assert json.loads(put.content) == {"description": "Bergen site", "resource_ids": ids("bergen-ev", "bergen-battery")}
    assert len(grown["resource_ids"]) == 3                                                  # the full member set replaces


def test_a_group_name_with_spaces_is_url_encoded_and_an_empty_group_is_allowed():
    vtn = Vtn()
    with client(vtn) as c:
        empty = c.put_group("Site one", [])
    assert empty["resource_ids"] == [] and "Site%20one" in str(sent(vtn, "PUT")[0].url)


def test_a_resource_already_in_another_group_is_a_409_that_says_which():
    vtn = Vtn()
    with client(vtn) as c:
        c.put_group("first", ids("bergen-ev"))
        with pytest.raises(FlexgatewayApiError) as error:
            c.put_group("second", ids("bergen-ev", "bergen-solar"))
    assert error.value.status_code == 409
    assert error.value.problem["conflicts"] == [{"resource_id": BY_NAME["bergen-ev"]["id"], "group_name": "first"}]
    assert "is already in group 'first'" in cli._problem_lines(error.value)


def test_members_on_different_meter_points_are_a_422_naming_them():
    with client(Vtn()) as c, pytest.raises(FlexgatewayApiError) as error:
        c.put_group("mixed", ids("bergen-ev", "oslo-battery"))
    assert error.value.status_code == 422 and error.value.problem["meterpoint_ids"] == ["MP1", "MP2"]
    assert "meter points: MP1, MP2" in cli._problem_lines(error.value)


def test_list_get_and_delete_a_group():
    vtn = Vtn()
    with client(vtn) as c:
        c.put_group("a", ids("bergen-ev"))
        assert [g["group_name"] for g in c.list_groups()] == ["a"] and c.get_group("a")["target"].endswith("/a")
        assert c.delete_group("a") == "deleted" and c.delete_group("a") == "not found"      # deleting twice is not an error
        with pytest.raises(FlexgatewayApiError) as error:
            c.get_group("a")
    assert error.value.status_code == 404


def test_a_group_in_a_portfolio_cannot_be_deleted():
    vtn = Vtn()
    with client(vtn) as c:
        c.put_group("a", ids("bergen-ev"))
        vtn.in_portfolio = True
        with pytest.raises(FlexgatewayApiError, match="in use by Springboard") as error:
            c.delete_group("a")
    assert error.value.status_code == 409


# ── available flex ───────────────────────────────────────────────────────────

def interval(minutes=0, **kw):
    return groups.availability_interval(START + timedelta(minutes=minutes), **{"forecast_kw": 800, "down_kw": 600, "up_kw": 300, **kw})


def test_a_push_is_one_call_in_the_documented_shape_and_pushing_again_is_harmless():
    vtn = Vtn()
    with client(vtn) as c:
        c.put_group("a", ids("bergen-ev"))
        assert c.push_availability("a", [interval(0), interval(15, up_kw=0)]) == 2
        assert c.push_availability("a", [interval(0), interval(15, up_kw=0)]) == 2
        got = c.get_availability("a", START, START + timedelta(hours=1))
    assert json.loads(sent(vtn, "PUT")[-1].content) == {"interval": "PT15M", "intervals": [
        {"start": "2026-11-10T06:00:00Z", "USAGE_FORECAST": 800.0, "DOWN_REGULATION_AVAILABLE": 600.0, "UP_REGULATION_AVAILABLE": 300.0},
        {"start": "2026-11-10T06:15:00Z", "USAGE_FORECAST": 800.0, "DOWN_REGULATION_AVAILABLE": 600.0, "UP_REGULATION_AVAILABLE": 0.0}]}
    assert len(got["intervals"]) == 2                                                       # the same two, not four
    get = sent(vtn, "GET")[-1]
    assert get.url.params["from"] == "2026-11-10T06:00:00Z" and get.url.params["to"] == "2026-11-10T07:00:00Z"


def test_a_mistake_in_a_push_is_caught_before_any_call():
    vtn = Vtn()
    with client(vtn) as c:
        c.put_group("a", ids("bergen-ev"))
        before = len(vtn.requests)
        for bad in ({"start": "2026-11-10T06:10:00Z", "USAGE_FORECAST": 1.0, "DOWN_REGULATION_AVAILABLE": 1.0, "UP_REGULATION_AVAILABLE": 1.0},
                    {"start": "2026-11-10T06:00:00Z", "USAGE_FORECAST": -1.0, "DOWN_REGULATION_AVAILABLE": 1.0, "UP_REGULATION_AVAILABLE": 1.0},
                    {"start": "2026-11-10T06:00:00Z", "USAGE_FORECAST": 1.0, "DOWN_REGULATION_AVAILABLE": 1.0}):
            with pytest.raises(ValueError):
                c.push_availability("a", [bad])
        with pytest.raises(ValueError, match="at least one"):
            c.push_availability("a", [])
        with pytest.raises(ValueError, match="at most"):
            c.push_availability("a", [interval(15 * i) for i in range(groups.MAX_PUSH_INTERVALS + 1)])
    assert len(vtn.requests) == before


def test_the_servers_own_refusal_of_a_push_lists_what_is_wrong():
    vtn = Vtn()
    with client(vtn) as c:
        c.put_group("a", ids("bergen-ev"))
        with pytest.raises(FlexgatewayApiError) as error:
            c._vtn("PUT", "groups/a/availability", json={"interval": "PT30M", "intervals": [
                {"start": "2026-11-10T06:10:00Z", "USAGE_FORECAST": 1, "DOWN_REGULATION_AVAILABLE": 1, "UP_REGULATION_AVAILABLE": 1}]})
    assert error.value.status_code == 422 and len(error.value.problem["errors"]) == 2
    assert "quarter hour" in cli._problem_lines(error.value)


def test_an_interval_needs_an_aware_utc_quarter_hour_and_three_non_negative_numbers():
    with pytest.raises(ValueError, match="no timezone"):
        groups.availability_interval(datetime(2026, 11, 10, 6, 0), forecast_kw=1, down_kw=1, up_kw=1)
    with pytest.raises(ValueError, match="quarter hour"):
        groups.availability_interval(START + timedelta(seconds=30), forecast_kw=1, down_kw=1, up_kw=1)
    oslo = datetime(2026, 11, 10, 7, 0, tzinfo=timezone(timedelta(hours=1)))
    assert groups.availability_interval(oslo, forecast_kw=1, down_kw=0, up_kw=0)["start"] == "2026-11-10T06:00:00Z"   # converted to UTC
    for value in (-0.1, float("nan"), float("inf"), "5", True, None):
        with pytest.raises(ValueError):
            groups.availability_interval(START, forecast_kw=value, down_kw=1, up_kw=1)


def test_the_rolling_window_is_144_quarter_hours_from_the_current_one():
    now = datetime(2026, 11, 10, 6, 7, 31, tzinfo=timezone.utc)
    window = groups.rolling_window(now)
    assert len(window) == 144 and window[0] == START and window[-1] == START + timedelta(hours=36) - timedelta(minutes=15)
    assert all(w.minute % 15 == 0 and w.second == 0 for w in window)
    assert groups.quarter_floor(datetime(2026, 11, 10, 7, 59, 59, tzinfo=timezone(timedelta(hours=1)))) == datetime(2026, 11, 10, 6, 45, tzinfo=timezone.utc)


def test_the_synthetic_plan_is_valid_deterministic_with_a_seed_and_never_negative():
    now = datetime(2026, 11, 10, 6, 7, tzinfo=timezone.utc)
    plan = groups.synthetic_availability(now, 36, seed=7)
    assert plan == groups.synthetic_availability(now, 36, seed=7) and plan != groups.synthetic_availability(now, 36, seed=8)
    body = groups.availability_body(plan)                                                    # passes its own check
    assert len(body["intervals"]) == 144
    assert all(row[k] >= 0 for row in plan for k in groups.PAYLOAD_TYPES)


# ── receiving a group dispatch ───────────────────────────────────────────────

def dispatch_event(target, kw=-300, start="2026-11-10T06:00:00Z", duration="PT15M", event_id=None):
    return {"id": event_id or str(uuid.uuid4()), "eventName": "DISPATCH fjellkraft-bergen 2026-11-10T06:00:00Z", "targets": [target],
            "modificationDateTime": "2026-11-10T05:00:00Z",
            "intervals": [{"id": 0, "intervalPeriod": {"start": start, "duration": duration},
                           "payloads": [{"type": "DISPATCH_SETPOINT_RELATIVE", "values": [kw]}]}]}


def test_only_events_addressed_to_the_group_are_its_dispatch():
    vtn = Vtn()
    vtn.events = [dispatch_event("GROUP:fjellkraft-ven/a"), dispatch_event("GROUP:fjellkraft-ven/b"), dispatch_event("PROGRAM:X")]
    with client(vtn) as c:
        group = c.put_group("a", ids("bergen-ev"))
        assert [e["targets"] for e in c.group_events(group)] == [["GROUP:fjellkraft-ven/a"]]
    with pytest.raises(ValueError, match="no target"):
        groups.events_for_group([], {})


def test_a_groups_setpoint_splits_in_proportion_keeps_its_sign_and_adds_up_exactly():
    parts = groups.split_setpoint(-1000, {"ev": 1200, "battery": 500, "solar": 350})
    assert parts == {"ev": -585.366, "battery": -243.902, "solar": -170.732} and round(sum(parts.values()), 3) == -1000
    assert groups.split_setpoint(100, {"a": 1, "b": 1, "c": 1}) == {"a": 33.334, "b": 33.333, "c": 33.333}      # the remainder to the heaviest
    assert round(sum(groups.split_setpoint(0.007, {"a": 3, "b": 3, "c": 1}).values()), 3) == 0.007
    assert groups.split_setpoint(-50, {"a": 10, "b": 0}) == {"a": -50.0}                                         # no weight, no share
    with pytest.raises(ValueError, match="positive weight"):
        groups.split_setpoint(-50, {"a": 0})


def test_a_resource_can_declare_the_notice_it_needs():
    body = payloads.resource_request("r", resource_type="battery", min_lead_time_minutes=30)
    assert {"type": "MIN_LEAD_TIME_MINUTES", "values": [30]} in body["attributes"]
    assert "MIN_LEAD_TIME_MINUTES" not in str(payloads.resource_request("r", resource_type="battery"))      # left out unless declared


def test_the_weight_of_a_resource_is_its_larger_rated_power():
    assert [groups.resource_power_kw(BY_NAME[n]) for n in ("bergen-ev", "bergen-battery", "bergen-solar")] == [1200.0, 500.0, 350.0]
    assert groups.resource_power_kw({"attributes": [{"type": "MAX_POWER_EXPORT", "values": ["x"]}]}) == 0.0


def test_a_dispatch_is_described_with_its_meaning_and_split():
    lines = groups.describe_dispatch(dispatch_event("GROUP:fjellkraft-ven/a", -300), {"ev": 1200, "battery": 600})
    text = "\n".join(lines)
    assert "setpoint -300 kW  up-regulation" in text and "ev: -200 kW" in text and "battery: -100 kW" in text
    assert "down-regulation" in "\n".join(groups.describe_dispatch(dispatch_event("GROUP:x/a", 50)))


def test_listen_shows_where_an_event_is_addressed():
    line = listen.describe({"objectType": "EVENT", "operation": "CREATE", "object": dispatch_event("GROUP:fjellkraft-ven/a")})
    assert "targets=GROUP:fjellkraft-ven/a" in line and "DISPATCH_SETPOINT_RELATIVE=-300" in line


# ── the commands ─────────────────────────────────────────────────────────────

def ns(**kw):
    defaults = dict(file=None, name=None, resource=None, description=None, dry_run=False, group=None, synthetic=False, hours=36.0,
                    forecast_kw=800.0, down_kw=600.0, up_kw=300.0, active=False)
    return argparse.Namespace(**{**defaults, **kw})


def test_put_group_resolves_names_to_ids_and_is_safe_to_rerun(tmp_path):
    vtn = Vtn()
    path = tmp_path / "groups.json"
    path.write_text(json.dumps([{"groupName": "bergen", "description": "d", "resources": ["bergen-ev", "bergen-solar"]},
                                {"groupName": "oslo", "resources": ["oslo-battery"]}]))
    with client(vtn) as c:
        first = cli._put_groups(c, ns(file=str(path)))
        second = cli._put_groups(c, ns(file=str(path)))
    assert [g["status"] for g in first["groups"]] == ["created", "created"] and [g["status"] for g in second["groups"]] == ["replaced", "replaced"]
    assert first["groups"][0]["target"] == "GROUP:fjellkraft-ven/bergen" and sorted(vtn.groups) == ["bergen", "oslo"]
    assert sorted(vtn.groups["bergen"]["resource_ids"]) == sorted(ids("bergen-ev", "bergen-solar"))


def test_put_group_by_name_dry_run_and_unknown_resources(tmp_path):
    vtn = Vtn()
    with client(vtn) as c:
        dry = cli._put_groups(c, ns(name="x", resource=["bergen-ev"], dry_run=True))
        assert dry["dry_run"] is True and dry["groups"][0]["status"] == "would create or replace" and vtn.groups == {}
        with pytest.raises(LookupError, match="no resource named: nope"):
            cli._put_groups(c, ns(name="x", resource=["nope"]))
        with pytest.raises(ValueError, match="--file, or --name"):
            cli._put_groups(c, ns())
        assert cli._put_groups(c, ns(name="empty"))["groups"][0]["status"] == "created"      # a group with no members yet


def test_push_availability_dry_run_sends_nothing_and_synthetic_sends_a_rolling_36_hours():
    vtn = Vtn()
    with client(vtn) as c:
        c.put_group("a", ids("bergen-ev"))
        dry = cli._push_availability(c, ns(group="a", synthetic=True, dry_run=True))
        assert dry["dry_run"] is True and dry["would_send"]["intervals"] == 144 and not sent(vtn, "PUT")[1:]
        real = cli._push_availability(c, ns(group="a", synthetic=True))
    assert real["sent"]["intervals"] == 144 and len(vtn.availability) == 144


def test_push_availability_from_a_file(tmp_path):
    vtn = Vtn()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"intervals": [interval(0), interval(15)]}))
    with client(vtn) as c:
        c.put_group("a", ids("bergen-ev"))
        assert cli._push_availability(c, ns(group="a", file=str(path)))["sent"]["intervals"] == 2


def test_group_events_lists_the_dispatch_with_its_split_by_rated_power():
    vtn = Vtn()
    with client(vtn) as c:
        c.put_group("bergen", ids("bergen-ev", "bergen-battery", "bergen-solar"))
        assert cli._group_events(c, ns(group="bergen")) == ["no events for GROUP:fjellkraft-ven/bergen"]
        vtn.events = [dispatch_event("GROUP:fjellkraft-ven/bergen", -1000)]
        text = "\n".join(cli._group_events(c, ns(group="bergen")))
    assert "setpoint -1000 kW" in text and "bergen-ev: -585.366 kW" in text and "bergen-solar: -170.732 kW" in text


def test_the_cli_knows_the_new_commands():
    parsed = cli._parser().parse_args(["push-availability", "--group", "a", "--synthetic", "--hours", "12"])
    assert (parsed.command, parsed.group, parsed.synthetic, parsed.hours) == ("push-availability", "a", True, 12.0)
    for words in (["put-group", "--name", "a"], ["list-groups"], ["get-group", "a"], ["delete-group", "a"],
                  ["get-availability", "--group", "a"], ["group-events", "--group", "a"]):
        cli._parser().parse_args(words)
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["push-availability", "--group", "a"])           # a source is required


# ── the examples' building blocks ────────────────────────────────────────────

@pytest.fixture()
def examples(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "examples"))
    for name in ("handle_group_dispatch", "push_availability", "_common"):
        sys.modules.pop(name, None)
    return importlib.import_module


def test_the_examples_import_and_parse_durations(examples):
    handler = examples("handle_group_dispatch")
    assert [handler.seconds(d) for d in ("PT15M", "PT1H", "PT90M", "PT2H30M", "PT45S")] == [900, 3600, 5400, 9000, 45]
    with pytest.raises(ValueError):
        handler.seconds("P1D")
    assert examples("push_availability").read_my_plan({"group_name": "a"}, START, 1)[0]["start"] == "2026-11-10T06:00:00Z"


def test_the_handler_splits_a_group_dispatch_and_reports_each_resource_idempotently(examples, capsys):
    handler = examples("handle_group_dispatch")
    vtn = Vtn()
    with client(vtn) as c:
        group = c.put_group("bergen", ids("bergen-ev", "bergen-battery", "bergen-solar"))
        by_id = {r["id"]: r for r in RESOURCES}
        event = dispatch_event(group["target"], -1000, duration="PT30M", event_id="evt-1")
        handler.handle(c, group, event, by_id, report=True)
        first = dict(vtn.reports)
        handler.handle(c, group, event, by_id, report=True)                      # again: the reports are replaced, not duplicated
    out = capsys.readouterr().out
    assert "group setpoint -1000 kW (up-regulation)" in out and "bergen-ev: -585.366 kW" in out
    assert sorted(first) == ["dispatch-evt-1-bergen-battery", "dispatch-evt-1-bergen-ev", "dispatch-evt-1-bergen-solar"]
    assert vtn.reports.keys() == first.keys()
    ev = first["dispatch-evt-1-bergen-ev"]["resources"][0]["intervals"]
    assert len(ev) == 2 and [i["intervalPeriod"]["start"] for i in ev] == ["2026-11-10T06:00:00Z", "2026-11-10T06:15:00Z"]
    usage = {p["type"]: p["values"][0] for p in ev[0]["payloads"]}
    assert usage["BASELINE"] == 360.0 and usage["USAGE"] == round((360.0 - 585.366) * 0.25, 4)       # baseline + its share, kWh over 15 min
