"""Finding a program by name and enrolling resources in it (an operator action: a BL client, /api/vtn).

A small fake VTN, not the spec-validating one of test_client.py: enrollment is deliberately not an OpenADR operation."""
import argparse
import json
import uuid

import httpx
import pytest

from energydesk_flexgateway_client import FlexgatewayApiError, FlexgatewayClient, cli

ROOT = "https://vtn.example.test/flexibility"
BASE = f"{ROOT}/openadr3"


def make_program(name):
    return {"id": str(uuid.uuid4()), "objectType": "PROGRAM", "programName": name}


PROGRAMS = [make_program(n) for n in ("MFRR_CM", "MFRR_EAM", "NODES_LONGFLEX_ARMED", "NODES_SHORTFLEX", "METERING")]
VEN = {"id": str(uuid.uuid4()), "venName": "fjellkraft-ven"}
RESOURCES = [{"id": str(uuid.uuid4()), "venID": VEN["id"], "resourceName": n} for n in ("sim-NO1-0001", "sim-NO1-0002", "real-meter")]
RESOURCES.append({"id": str(uuid.uuid4()), "venID": str(uuid.uuid4()), "resourceName": "someone-elses"})


class Vtn:
    def __init__(self, *, bl=True, programs=PROGRAMS):
        self.bl, self.programs, self.enrolled, self.requests = bl, list(programs), set(), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path[len("/flexibility"):]
        if request.method == "GET" and path == "/openadr3/programs":
            return httpx.Response(200, json=self.page(self.programs, request))
        if request.method == "GET" and path == "/openadr3/vens":
            return httpx.Response(200, json=self.page([VEN], request))
        if request.method == "GET" and path == "/openadr3/resources":
            return httpx.Response(200, json=self.page(RESOURCES, request))
        if request.method == "POST" and path.startswith("/api/vtn/programs/") and path.endswith("/resources"):
            if not self.bl:
                return httpx.Response(403, json={"title": "Forbidden", "detail": "This route needs a business logic (BL) client"})
            key = (path.split("/")[4], request.url.params["resource_id"])
            if key in self.enrolled:
                return httpx.Response(409, json={"detail": "Resource is already enrolled in this program"})
            self.enrolled.add(key)
            return httpx.Response(201, json={"program_id": key[0], "resource_id": key[1], "is_active": True})
        return httpx.Response(404, json={"detail": f"unexpected {request.method} {path}"})

    @staticmethod
    def page(items, request):
        skip, limit = int(request.url.params.get("skip", 0)), int(request.url.params.get("limit", 50))
        return items[skip:skip + limit]


def client(vtn):
    return FlexgatewayClient(BASE, "token", ven_name="fjellkraft-ven", transport=httpx.MockTransport(vtn))


def args(**kw):
    defaults = dict(program="Longflex Arm", resource=None, name_prefix=None, all=False, dry_run=False, ven_name="fjellkraft-ven")
    return argparse.Namespace(**{**defaults, **kw})


# ── finding the program ──────────────────────────────────────────────────────

@pytest.mark.parametrize("query", ["Longflex Arm", "longflex arm", "LONGFLEX_ARMED", "nodes longflex armed", "NODES_LONGFLEX_ARMED"])
def test_longflex_armed_is_found_by_its_words(query):
    with client(Vtn()) as c:
        assert c.find_program(query)["programName"] == "NODES_LONGFLEX_ARMED"


def test_an_exact_name_wins_over_a_partial_match():
    programs = [make_program("MFRR"), make_program("MFRR_EAM"), make_program("MFRR_CM")]
    with client(Vtn(programs=programs)) as c:
        assert c.find_program("mfrr")["programName"] == "MFRR"


def test_several_matches_are_an_error_that_names_them():
    with client(Vtn()) as c, pytest.raises(LookupError) as error:
        c.find_program("MFRR")
    assert "MFRR_CM" in str(error.value) and "MFRR_EAM" in str(error.value)


def test_no_match_lists_what_is_visible_and_says_why_a_ven_client_sees_little():
    with client(Vtn(programs=[PROGRAMS[0]])) as c, pytest.raises(LookupError) as error:
        c.find_program("Longflex Arm")
    message = str(error.value)
    assert "MFRR_CM" in message and "operator (BL) credentials" in message


# ── enrolling ────────────────────────────────────────────────────────────────

def test_enroll_posts_to_api_vtn_beside_the_openadr_prefix():
    vtn = Vtn()
    with client(vtn) as c:
        program, resource = PROGRAMS[2], RESOURCES[0]
        result = c.enroll_resource(program["id"], resource["id"])
    post = vtn.requests[-1]
    assert str(post.url).startswith(f"{ROOT}/api/vtn/programs/{program['id']}/resources") and post.url.params["resource_id"] == resource["id"]
    assert result["already_enrolled"] is False


def test_enrolling_twice_is_reported_not_an_error():
    with client(Vtn()) as c:
        program, resource = PROGRAMS[2], RESOURCES[0]
        c.enroll_resource(program["id"], resource["id"])
        assert c.enroll_resource(program["id"], resource["id"])["already_enrolled"] is True


def test_a_ven_credential_is_refused_with_a_hint_about_operator_credentials():
    with client(Vtn(bl=False)) as c, pytest.raises(FlexgatewayApiError) as error:
        c.enroll_resource(PROGRAMS[2]["id"], RESOURCES[0]["id"])
    assert error.value.status_code == 403 and "OPERATOR_CLIENT_ID" in error.value.detail and "BL" in error.value.detail


def test_the_api_url_can_be_overridden():
    c = FlexgatewayClient(BASE, "t", vtn_api_url="http://127.0.0.1:8444/api/vtn/")
    assert c.vtn_api_url == "http://127.0.0.1:8444/api/vtn"
    assert FlexgatewayClient("http://localhost:8444/openadr3", "t").vtn_api_url == "http://localhost:8444/api/vtn"


# ── the command ──────────────────────────────────────────────────────────────

def run(vtn, **kw):
    with client(vtn) as c:
        return cli._enroll(c, args(**kw))


def test_a_name_prefix_enrolls_only_this_vens_matching_resources():
    vtn = Vtn()
    result = run(vtn, name_prefix="sim-")
    assert [r["resourceName"] for r in result["resources"]] == ["sim-NO1-0001", "sim-NO1-0002"]
    assert {r["status"] for r in result["resources"]} == {"enrolled"} and result["program"]["programName"] == "NODES_LONGFLEX_ARMED"
    assert len(vtn.enrolled) == 2


def test_named_resources_and_all():
    assert [r["resourceName"] for r in run(Vtn(), resource=["real-meter"])["resources"]] == ["real-meter"]
    assert run(Vtn(), all=True)["count"] == 3                       # not someone-elses


def test_rerunning_reports_already_enrolled():
    vtn = Vtn()
    run(vtn, all=True)
    assert {r["status"] for r in run(vtn, all=True)["resources"]} == {"already enrolled"}


def test_a_dry_run_changes_nothing():
    vtn = Vtn()
    result = run(vtn, all=True, dry_run=True)
    assert result["dry_run"] is True and {r["status"] for r in result["resources"]} == {"would enroll"}
    assert vtn.enrolled == set() and not [r for r in vtn.requests if r.method == "POST"]


def test_unknown_resource_unknown_ven_and_empty_selection_are_errors():
    with pytest.raises(LookupError, match="no resource named: nope"):
        run(Vtn(), resource=["nope"])
    with pytest.raises(LookupError, match="no ven named"):
        run(Vtn(), ven_name="other-ven")
    with pytest.raises(LookupError, match="no resources"):
        run(Vtn(), name_prefix="zzz-")
    with pytest.raises(ValueError):
        run(Vtn(), ven_name=None)


# ── credentials: operator for enroll, the VEN's own for everything else ──────

def test_enroll_uses_operator_credentials_when_set(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "fetch_access_token", lambda url, cid, secret, scope=None: seen.append((cid, secret)) or "tok")
    monkeypatch.setattr(cli, "discover_token_url", lambda base: "https://t/token")
    for name in ("FLEXGATEWAY_ACCESS_TOKEN", "OAUTHCHECK_ACCESS_TOKEN_OBTAIN_URL", "ENERGYDESK_TOKEN_URL", "OAUTH_SCOPE", "ENERGYDESK_SCOPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OAUTH_CLIENT_ID", "ven-client")
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "ven-secret")
    monkeypatch.setenv("OPERATOR_CLIENT_ID", "bl-client")
    monkeypatch.setenv("OPERATOR_CLIENT_SECRET", "bl-secret")
    base = dict(access_token=None, client_id=None, client_secret=None, token_url=None, scope=None, base_url=BASE)
    cli._access_token(argparse.Namespace(command="enroll", **base))
    cli._access_token(argparse.Namespace(command="list-resources", **base))
    assert seen == [("bl-client", "bl-secret"), ("ven-client", "ven-secret")]
    cli._access_token(argparse.Namespace(command="find-program", **base))          # finding what to enroll in is an operator view too
    cli._access_token(argparse.Namespace(command="list-programs", operator=True, **base))   # --operator: any command
    cli._access_token(argparse.Namespace(command="list-programs", **base))                  # without it: the VEN's own view
    assert seen[2:] == [("bl-client", "bl-secret"), ("bl-client", "bl-secret"), ("ven-client", "ven-secret")]
    monkeypatch.delenv("OPERATOR_CLIENT_ID")
    cli._access_token(argparse.Namespace(command="enroll", **base))
    assert seen[-1] == ("ven-client", "ven-secret")                   # falls back; the server then explains what is missing
