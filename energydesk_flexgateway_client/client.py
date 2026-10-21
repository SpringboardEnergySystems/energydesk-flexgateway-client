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

import httpx

from . import payloads

PAGE_SIZE = 50  # the specification's maximum for ``limit``


class FlexgatewayApiError(RuntimeError):
    """A non-success response from OAuth or Flexgateway."""

    def __init__(self, method: str, url: str, status_code: int, detail: str):
        super().__init__(f"{method} {url} returned {status_code}: {detail}")
        self.method = method
        self.url = url
        self.status_code = status_code
        self.detail = detail


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
    ) -> None:
        if not access_token:
            raise ValueError("access_token must not be empty")
        self.ven_name = ven_name
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=f"{self.base_url}/",
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "energydesk-flexgateway-client/0.2.0",
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
            raise FlexgatewayApiError(method, str(response.request.url), response.status_code, _error_detail(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

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

    # ── programs and events ──────────────────────────────────────────────

    def list_programs(self) -> list[dict[str, Any]]:
        """The programs your resources are enrolled in (the operator enrolls; you never do)."""
        return list(self._paged("programs"))

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
                event["id"], self.ven_name or "flexgateway-demo", resource_name,
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
