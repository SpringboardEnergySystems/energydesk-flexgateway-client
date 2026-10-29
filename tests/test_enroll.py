"""Finding a program by name and enrolling resources in it, with the VEN's own credentials (/api/vtn).

A small fake VTN, not the spec-validating one of test_client.py: enrollment is deliberately not an OpenADR operation."""
import argparse
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
    """A fake VTN that enforces what the real one does for a partner: only open programs are visible, only approved resources
    can be enrolled, and a withdrawn resource is no longer enrolled."""

    def __init__(self, *, approved=True, programs=PROGRAMS):
        self.approved, self.programs, self.enrolled, self.requests = approved, list(programs), set(), []

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
            if not self.approved:
                return httpx.Response(403, json={"title": "Forbidden", "detail": "Resource 'x' has not been approved by Springboard yet (status: pending)"})
            key = (path.split("/")[4], request.url.params["resource_id"])
            if key in self.enrolled:
                return httpx.Response(409, json={"detail": "Resource is already enrolled in this program"})
            self.enrolled.add(key)
            return httpx.Response(201, json={"program_id": key[0], "resource_id": key[1], "is_active": True})
        if request.method == "DELETE" and path.startswith("/api/vtn/programs/"):
            _, _, _, _, program_id, _, resource_id = path.split("/")
            if (program_id, resource_id) not in self.enrolled:
                return httpx.Response(404, json={"detail": "Active enrollment not found"})
            self.enrolled.discard((program_id, resource_id))
            return httpx.Response(204)
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


def test_no_match_lists_what_is_visible_and_says_why_one_may_be_missing():
    with client(Vtn(programs=[PROGRAMS[0]])) as c, pytest.raises(LookupError) as error:
        c.find_program("Longflex Arm")
    message = str(error.value)
    assert "MFRR_CM" in message and "opened for enrollment" in message and "ask Springboard" in message


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


def test_a_resource_springboard_has_not_approved_is_refused_with_the_servers_reason():
    with client(Vtn(approved=False)) as c, pytest.raises(FlexgatewayApiError) as error:
        c.enroll_resource(PROGRAMS[2]["id"], RESOURCES[0]["id"])
    assert error.value.status_code == 403 and "not been approved" in error.value.detail


def test_withdrawing_and_withdrawing_again():
    vtn = Vtn()
    with client(vtn) as c:
        program, resource = PROGRAMS[2], RESOURCES[0]
        c.enroll_resource(program["id"], resource["id"])
        assert c.unenroll_resource(program["id"], resource["id"]) == "withdrawn"
        assert c.unenroll_resource(program["id"], resource["id"]) == "not enrolled"


def test_the_api_url_can_be_overridden():
    c = FlexgatewayClient(BASE, "t", vtn_api_url="http://127.0.0.1:8444/api/vtn/")
    assert c.vtn_api_url == "http://127.0.0.1:8444/api/vtn"
    assert FlexgatewayClient("http://localhost:8444/openadr3", "t").vtn_api_url == "http://localhost:8444/api/vtn"


# ── the command ──────────────────────────────────────────────────────────────

def run(vtn, withdraw=False, **kw):
    with client(vtn) as c:
        return cli._enroll(c, args(**kw), withdraw=withdraw)


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


def test_unenroll_withdraws_the_selected_resources_and_a_dry_run_changes_nothing():
    vtn = Vtn()
    run(vtn, name_prefix="sim-")
    dry = run(vtn, withdraw=True, name_prefix="sim-", dry_run=True)
    assert {r["status"] for r in dry["resources"]} == {"would withdraw"} and len(vtn.enrolled) == 2
    result = run(vtn, withdraw=True, name_prefix="sim-")
    assert {r["status"] for r in result["resources"]} == {"withdrawn"} and vtn.enrolled == set()
