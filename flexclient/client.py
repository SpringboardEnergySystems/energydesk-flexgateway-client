"""Small, dependency-light client for the EnergyDesk Flexgateway (OpenADR 3.1.0 VTN).

The VTN serves exactly the operations of the OpenADR 3.1.0 OpenAPI file, so any 3.1.0 client works against it;
this one is a readable reference for the journey a VEN goes through:

1. get a token (``discover_token_url`` + ``fetch_access_token``);
2. make sure the VEN exists (``ensure_ven``) and register its resources (``register_resource``);
3. read the programs and events it has been granted (``list_programs``, ``list_events``);
4. send reports (``create_report``) and, if wanted, subscribe to webhooks (``subscribe``).

Wire format: camelCase JSON as in the specification. Errors are RFC 7807 problem objects.
Nothing here logs or prints a token or a client secret.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Iterator, Mapping

import re
from urllib.parse import quote

import httpx

from . import groups, payloads

PAGE_SIZE = 50  # the specification's maximum for ``limit``


class FlexgatewayApiError(RuntimeError):
    """A non-success response from OAuth or Flexgateway."""

    def __init__(self, method: str, url: str, status_code: int, detail: str, problem: dict[str, Any] | None = None):
        super().__init__(f"{method} {url} returned {status_code}: {detail}")
        self.method = method
        self.url = url
        self.status_code = status_code
        self.detail = detail
        # The whole problem object when the server sent one: some carry more than ``detail``, e.g. a group claim that clashes
        # lists ``conflicts`` (resource and the group it is in), an availability push lists ``errors``, a refused dispatch ``problems``.
        self.problem = problem or {}


def _error_detail(response: httpx.Response) -> str:
    """The ``detail`` of a problem object (or the OAuth ``error_description``), else the body text."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or response.reason_phrase
    if isinstance(payload, dict):
        for key in ("detail", "error_description", "title"):
            if payload.get(key):
                return str(payload[key])
    return str(payload)


def _error_problem(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def discover_token_url(base_url: str, *, timeout: float = 15.0, transport: httpx.BaseTransport | None = None) -> str:
    """Ask the VTN where to get a token (``GET /auth/server``). No authentication needed."""
    url = f"{base_url.rstrip('/')}/auth/server"
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.get(url, headers={"Accept": "application/json"})
    if not response.is_success:
        raise FlexgatewayApiError("GET", url, response.status_code, _error_detail(response))
    token_url = response.json().get("tokenURL")
    if not token_url:
        raise FlexgatewayApiError("GET", url, response.status_code, "response has no tokenURL")
    return str(token_url)


def fetch_access_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    *,
    scope: str | None = None,
    timeout: float = 15.0,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Obtain an OAuth 2 token with the client-credentials grant (HTTP Basic client authentication).

    A VEN client holds a fixed set of scopes and gets all of them when no ``scope`` is asked for; asking for a scope
    outside the set is an ``invalid_scope`` error. The secret is sent to the token endpoint and nowhere else.
    """
    data = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope

    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(
            token_url, data=data, auth=(client_id, client_secret), headers={"Accept": "application/json"}
        )
    if not response.is_success:
        raise FlexgatewayApiError("POST", token_url, response.status_code, _error_detail(response))

    payload = response.json()
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not token:
        raise FlexgatewayApiError("POST", token_url, response.status_code, "response has no access_token")
    return str(token)


class FlexgatewayClient:
    """Synchronous client for the VEN-facing OpenADR 3.1.0 operations."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        ven_name: str | None = None,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        vtn_api_url: str | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("access_token must not be empty")
        self.ven_name = ven_name
        self.base_url = base_url.rstrip("/")
        # Enrollment is not an OpenADR operation: it lives beside the spec's prefix, at <host>/api/vtn.
        head, _, tail = self.base_url.rpartition("/")
        self.vtn_api_url = (vtn_api_url or (head if tail == "openadr3" else self.base_url) + "/api/vtn").rstrip("/")
        self._client = httpx.Client(
            base_url=f"{self.base_url}/",
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "flexclient/0.2.0",
            },
        )
        self._ven: dict[str, Any] | None = None

    def __enter__(self) -> "FlexgatewayClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── plumbing ─────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        response = self._client.request(method, path.lstrip("/"), json=json, params=params)
        if not response.is_success:
            raise FlexgatewayApiError(method, str(response.request.url), response.status_code, _error_detail(response),
                                      _error_problem(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _vtn(self, method: str, path: str, *, json: Mapping[str, Any] | None = None,
             params: Mapping[str, Any] | None = None) -> httpx.Response:
        """A call to ``/api/vtn`` (beside the OpenADR prefix): groups and availability. Returns the response; a problem raises."""
        try:
            response = self._client.request(method, f"{self.vtn_api_url}/{path.lstrip('/')}", json=json, params=params)
        except httpx.HTTPError as exc:
            raise FlexgatewayApiError(method, self.vtn_api_url, 0, f"{type(exc).__name__}") from exc
        if not response.is_success:
            raise FlexgatewayApiError(method, str(response.request.url), response.status_code, _error_detail(response),
                                      _error_problem(response))
        return response

    def _paged(self, path: str, params: Mapping[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Every object of a list operation, page by page (``skip`` / ``limit``)."""
        skip = 0
        while True:
            page = self._request("GET", path, params={**(params or {}), "skip": skip, "limit": PAGE_SIZE})
            yield from page
            if len(page) < PAGE_SIZE:
                return
            skip += len(page)

    # ── ven ──────────────────────────────────────────────────────────────

    def list_vens(self) -> list[dict[str, Any]]:
        """The VEN objects this client may see: its own, and nobody else's."""
        return self._request("GET", "vens")

    def ensure_ven(self, ven_name: str | None = None) -> dict[str, Any]:
        """Return this client's ``ven``, creating it the first time (a stock 3.1.0 client does exactly this).

        The VTN writes ``clientID`` from the token and grants the ``VEN:<venName>`` target itself. A client gets one
        ven; if Springboard pre-created it under another name, that one is returned.
        """
        if self._ven is None:
            vens = self.list_vens()
            if vens:
                self._ven = vens[0]
            else:
                name = ven_name or self.ven_name
                if not name:
                    raise ValueError("this client has no ven yet: give a ven_name to create it")
                self._ven = self._request("POST", "vens", json={"objectType": "VEN_VEN_REQUEST", "venName": name})
        return self._ven

    # ── resources ────────────────────────────────────────────────────────

    def register_resource(self, resource: Mapping[str, Any]) -> dict[str, Any]:
        """Create the resource, or update it if one with the same ``resourceName`` exists (idempotent).

        ``resource`` is a ``VEN_RESOURCE_REQUEST`` body; ``objectType`` and ``venID`` are filled in when missing.
        ``resourceName`` is yours and unique within your ven. Any ``targets`` are ignored by the VTN: granted
        targets (which decide what you can see) are never taken from a VEN.
        """
        body = {"objectType": "VEN_RESOURCE_REQUEST", **resource}
        body.setdefault("venID", self.ensure_ven()["id"])
        existing = self._request("GET", "resources", params={"resourceName": body["resourceName"]})
        if existing:
            return self._request("PUT", f"resources/{existing[0]['id']}", json=body)
        return self._request("POST", "resources", json=body)

    def list_resources(self) -> list[dict[str, Any]]:
        return list(self._paged("resources"))

    def get_resource(self, resource_id: str) -> dict[str, Any]:
        return self._request("GET", f"resources/{resource_id}")

    # ── resource groups and available flex (/api/vtn/groups) ─────────────

    def put_group(self, group_name: str, resource_ids: Iterable[str], *, description: str | None = None) -> dict[str, Any]:
        """Create the group, or replace its description and members in one go (idempotent: the same call twice changes nothing).

        A group is your reporting and dispatch unit, usually one site: you push its available flex, and Springboard's dispatch
        arrives as one event targeted at the group. ``resource_ids`` is the **full** member set; a resource is in at most one
        group, and all members must sit on the same meter point. An empty list is a group with no members yet.

        Returns the group as the server sees it plus ``created`` (``True`` the first time): ``target`` (the exact string events
        are targeted with: you never build it yourself), the ``location`` derived from the members, and ``eligible_programs`` /
        ``ineligible_programs`` (which programs the group can be dispatched in: every member must be enrolled and approved there).

        Refused with ``409`` when a resource is already in another group (``error.problem["conflicts"]`` says which), and ``422``
        when a resource is not yours or the members are on different meter points.
        """
        response = self._vtn("PUT", f"groups/{quote(group_name, safe='')}",
                             json={"description": description, "resource_ids": list(resource_ids)})
        return {**response.json(), "created": response.status_code == 201}

    def list_groups(self) -> list[dict[str, Any]]:
        """Your groups (never anyone else's), each with members, target, location and program eligibility."""
        return self._vtn("GET", "groups").json()

    def get_group(self, group_name: str) -> dict[str, Any]:
        return self._vtn("GET", f"groups/{quote(group_name, safe='')}").json()

    def delete_group(self, group_name: str) -> str:
        """Delete a group with its availability series; its members stay registered. Returns ``deleted`` or ``not found``.
        Refused with ``409`` while a member is in a portfolio (it may be part of a bid): Springboard releases it first."""
        try:
            self._vtn("DELETE", f"groups/{quote(group_name, safe='')}")
        except FlexgatewayApiError as exc:
            if exc.status_code == 404:
                return "not found"
            raise
        return "deleted"

    def push_availability(self, group_name: str, intervals: Iterable[Mapping[str, Any]]) -> int:
        """Push your forecast of the group's available flex (see ``flexclient.groups.availability_interval``). Every interval
        replaces what is stored for that start, and the others are left alone, so pushing the same data again is harmless: push
        every 15 minutes, and straight away when your plan changes. Returns the number of intervals sent.

        ``422`` says what is wrong with an interval (``error.problem["errors"]``): not on a UTC quarter hour, a negative or
        missing value, another interval length than 15 minutes. A push is all or nothing."""
        body = groups.availability_body(intervals)
        self._vtn("PUT", f"groups/{quote(group_name, safe='')}/availability", json=body)
        return len(body["intervals"])

    def get_availability(self, group_name: str, start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
        """What is stored for the group in ``[start, end)``, in the shape of the push (default: now and 36 hours ahead)."""
        params = {k: payloads.iso(v) for k, v in (("from", start), ("to", end)) if v is not None}
        return self._vtn("GET", f"groups/{quote(group_name, safe='')}/availability", params=params).json()

    def group_events(self, group: Mapping[str, Any], *, active: bool | None = None) -> list[dict[str, Any]]:
        """The events addressed to this group (``GROUP:<ven>/<name>``): how Springboard's dispatch reaches you."""
        return groups.events_for_group(self.list_events(active=active), group)

    # ── programs and events ──────────────────────────────────────────────

    def list_programs(self) -> list[dict[str, Any]]:
        """The programs Springboard has opened for enrollment, plus the ones your resources are already in."""
        return list(self._paged("programs"))

    @staticmethod
    def _words(text: str) -> list[str]:
        return [w for w in re.split(r"[^A-Za-z0-9]+", text.upper()) if w]

    def find_program(self, query: str) -> dict[str, Any]:
        """The one program whose name matches ``query``: every word of the query (any case, any separator) must occur in
        the program name, so ``"Longflex Arm"`` finds ``NODES_LONGFLEX_ARMED``. An exact name always wins. Several matches
        or none is an error that lists what was seen. Only programs you can see are searched: the ones Springboard has opened
        for enrollment, and the ones your resources are already in."""
        programs = self.list_programs()
        wanted = self._words(query)
        if not wanted:
            raise ValueError("a program name is required")
        exact = [p for p in programs if p.get("programName", "").upper() == query.strip().upper()]
        matches = exact or [p for p in programs if all(w in p.get("programName", "").upper() for w in wanted)]
        if len(matches) == 1:
            return matches[0]
        names = ", ".join(sorted(p.get("programName", "?") for p in programs)) or "none"
        if not matches:
            raise LookupError(f"no program matches {query!r}. Programs you can see: {names}. You see the programs Springboard "
                              "has opened for enrollment and the ones your resources are already in; ask Springboard if one is missing.")
        raise LookupError(f"{query!r} matches {len(matches)} programs: {', '.join(p['programName'] for p in matches)}. Be more specific.")

    def enroll_resource(self, program_id: str, resource_id: str) -> dict[str, Any]:
        """Enroll one of your resources in a program (``POST /api/vtn/programs/{id}/resources``). The program must be open
        for enrollment and the resource approved by Springboard. Enrolling twice is not an error here: ``already_enrolled`` says so."""
        try:
            response = self._client.request("POST", f"{self.vtn_api_url}/programs/{program_id}/resources",
                                            params={"resource_id": resource_id})
        except httpx.HTTPError as exc:
            raise FlexgatewayApiError("POST", self.vtn_api_url, 0, f"{type(exc).__name__}") from exc
        if response.status_code == 409 and "already enrolled" in _error_detail(response).lower():
            return {"program_id": program_id, "resource_id": resource_id, "already_enrolled": True}
        if not response.is_success:
            detail = _error_detail(response)
            if response.status_code == 404:
                detail += " (the program is not open for enrollment, or the resource is not yours)"
            raise FlexgatewayApiError("POST", str(response.request.url), response.status_code, detail)
        return {**response.json(), "already_enrolled": False}

    def unenroll_resource(self, program_id: str, resource_id: str) -> str:
        """Withdraw one of your resources from a program. Returns ``withdrawn``, or ``not enrolled``; a resource Springboard has put
        in a portfolio cannot be withdrawn by you (409): Springboard releases it."""
        response = self._client.request("DELETE", f"{self.vtn_api_url}/programs/{program_id}/resources/{resource_id}")
        if response.status_code == 404 and "enrollment not found" in _error_detail(response).lower():
            return "not enrolled"
        if not response.is_success:
            raise FlexgatewayApiError("DELETE", str(response.request.url), response.status_code, _error_detail(response))
        return "withdrawn"

    def list_events(self, program_id: str | None = None, *, active: bool | None = None) -> list[dict[str, Any]]:
        """Events targeted at your ven, your resources or your programs. ``active`` limits to those running now."""
        params: dict[str, Any] = {}
        if program_id:
            params["programID"] = program_id
        if active is not None:
            params["active"] = str(active).lower()
        return list(self._paged("events", params))

    def get_event(self, event_id: str) -> dict[str, Any]:
        return self._request("GET", f"events/{event_id}")

    # ── reports ──────────────────────────────────────────────────────────

    def create_report(self, report: Mapping[str, Any]) -> dict[str, Any]:
        """Post a ``reportRequest``: the event it answers, and per resource its intervals with payloads."""
        return self._request("POST", "reports", json=report)

    def update_report(self, report_id: str, report: Mapping[str, Any]) -> dict[str, Any]:
        """Replace a report (a corrected re-post overwrites its intervals)."""
        return self._request("PUT", f"reports/{report_id}", json=report)

    def list_reports(self, *, event_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._paged("reports", {"eventID": event_id} if event_id else None))

    def get_report(self, report_id: str) -> dict[str, Any]:
        return self._request("GET", f"reports/{report_id}")

    def report_readings(
        self,
        resource_name: str,
        points: Iterable[tuple[datetime, float]],
        *,
        payload_type: str = "DEMAND",
        interval_minutes: int = 15,
    ) -> list[dict[str, Any]]:
        """Report interval readings for one resource against **your own metering event**, no program needed.

        ``points`` are ``(interval start, value)``; ``DEMAND`` is kW (positive consumption, negative generation) and
        ``USAGE`` is kWh over the interval. Readings are sent as one report per resource, payload type and UTC week,
        named so that sending the same data again **replaces** the report instead of adding a copy: the call is
        idempotent. What you report here serves every program the resource joins later; nothing to backfill.
        """
        event = payloads.metering_event(self.list_events())
        if event is None:
            raise ValueError("this ven has no metering event yet: the VTN creates one with the ven, so it needs a restart "
                             "or an upgrade")
        existing = {r["reportName"]: r["id"] for r in self.list_reports(event_id=event["id"]) if r.get("reportName")}
        duration = f"PT{interval_minutes // 60}H" if interval_minutes % 60 == 0 else f"PT{interval_minutes}M"
        sent: list[dict[str, Any]] = []
        for week, group in payloads.group_by_week(points):
            name = f"readings-{resource_name}-{payload_type.lower()}-{week:%Y-%m-%d}"
            body = payloads.report_request(
                event["id"], self.ven_name or "flexclient", resource_name,
                (payloads.interval(i, start, duration, {payload_type: value}) for i, (start, value) in enumerate(group)),
                report_name=name)
            sent.append(self.update_report(existing[name], body) if name in existing else self.create_report(body))
        return sent

    # ── subscriptions (webhooks) ─────────────────────────────────────────

    def notifiers(self) -> dict[str, Any]:
        """The push bindings the VTN offers: ``{"WEBHOOK": true}``."""
        return self._request("GET", "notifiers")

    def subscribe(
        self,
        callback_url: str,
        *,
        client_name: str,
        objects: tuple[str, ...] = ("EVENT", "PROGRAM"),
        operations: tuple[str, ...] = ("CREATE", "UPDATE", "DELETE"),
        bearer_token: str | None = None,
        program_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask the VTN to ``POST`` notifications to ``callback_url`` (https, a public address).

        ``bearer_token`` is a secret of your own choosing, which the VTN presents as ``Authorization: Bearer ...``
        so your endpoint can tell it is the VTN. It is stored encrypted by the VTN and returned only to you.
        """
        entry: dict[str, Any] = {"objects": list(objects), "operations": list(operations), "callbackUrl": callback_url}
        if bearer_token:
            entry["bearerToken"] = bearer_token
        body: dict[str, Any] = {"clientName": client_name, "objectOperations": [entry]}
        if program_id:
            body["programID"] = program_id
        return self._request("POST", "subscriptions", json=body)

    def list_subscriptions(self) -> list[dict[str, Any]]:
        return list(self._paged("subscriptions"))

    def delete_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"subscriptions/{subscription_id}")
