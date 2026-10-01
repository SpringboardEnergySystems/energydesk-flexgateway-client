"""Small, dependency-light Flexgateway HTTP client."""

from __future__ import annotations

from typing import Any, Mapping

import httpx


class FlexgatewayApiError(RuntimeError):
    """A non-success response from OAuth or Flexgateway."""

    def __init__(self, method: str, url: str, status_code: int, detail: str):
        super().__init__(f"{method} {url} returned {status_code}: {detail}")
        self.method = method
        self.url = url
        self.status_code = status_code
        self.detail = detail


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or response.reason_phrase
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return str(payload)


def fetch_access_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    *,
    scope: str | None = None,
    timeout: float = 15.0,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Obtain an OAuth 2 token using the client-credentials grant."""

    data = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope

    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(
            token_url,
            data=data,
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
        )
    if not response.is_success:
        raise FlexgatewayApiError(
            "POST", token_url, response.status_code, _error_detail(response)
        )

    payload = response.json()
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not token:
        raise FlexgatewayApiError(
            "POST", token_url, response.status_code, "response has no access_token"
        )
    return str(token)


class FlexgatewayClient:
    """Synchronous client for the partner-facing OpenADR endpoints."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("access_token must not be empty")
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=f"{self.base_url}/",
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "energydesk-flexgateway-client/0.1.0",
            },
        )

    def __enter__(self) -> "FlexgatewayClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        response = self._client.request(
            method, path.lstrip("/"), json=json, params=params
        )
        if not response.is_success:
            raise FlexgatewayApiError(
                method,
                str(response.request.url),
                response.status_code,
                _error_detail(response),
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def list_vens(self) -> list[dict[str, Any]]:
        return self._request("GET", "vens")

    def register_resource(self, resource: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "resources", json=resource)

    def list_resources(self) -> list[dict[str, Any]]:
        return self._request("GET", "resources")

    def get_resource(self, resource_id: str) -> dict[str, Any]:
        return self._request("GET", f"resources/{resource_id}")

    def create_report(self, report: Mapping[str, Any]) -> dict[str, Any]:
        """Declare a report: what resource, what reading_type/report_type,
        at what interval_period. Returns the report, including its id --
        pass that id to submit_report_data."""
        return self._request("POST", "reports", json=report)

    def list_reports(self) -> list[dict[str, Any]]:
        return self._request("GET", "reports")

    def submit_report_data(
        self, report_id: str, data_points: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Upsert meter readings against an existing report. Each point is
        {"interval_start": <ISO 8601>, "value": <float>}. Resubmitting the
        same interval_start is safe and idempotent -- it overwrites, not
        duplicates."""
        return self._request(
            "POST",
            "report_data",
            json=list(data_points),
            params={"report_id": report_id},
        )

    def submit_report_data_bulk(
        self,
        resource_id: str,
        data_points: list[Mapping[str, Any]],
        *,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        """Historical/bulk meter data directly against a resource, without
        requiring a report to exist first. report_id is optional context."""
        params: dict[str, Any] = {"resource_id": resource_id}
        if report_id:
            params["report_id"] = report_id
        return self._request(
            "POST", "report_data/bulk", json=list(data_points), params=params
        )

    def get_report_data(
        self,
        report_id: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        params = {k: v for k, v in {"start_time": start_time, "end_time": end_time}.items() if v}
        return self._request("GET", f"report_data/{report_id}", params=params)
