"""Thin typed httpx client targeting the canonical ``/v1`` HTTP API.

JSON response shapes are unchanged from the unversioned aliases; this client
only pins the URL prefix. Generate a fuller client with ``make client-go`` /
``make client-python`` from ``clients/openapi.json``.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx


class RaglogsAPIError(Exception):
    """Raised when the API returns a non-success status code."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body}")


def _omit_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


class RaglogsClient:
    """Synchronous httpx client for raglogs ``/v1`` query and ingest routes."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        token: Optional[str] = None,
        timeout: float = 30.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RaglogsClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        if not self._base_url:
            return path
        return self._base_url + path

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> Any:
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        response = self._client.request(
            method,
            self._url(path),
            headers=headers,
            json=json_body,
        )
        if response.status_code >= 400:
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text
            raise RaglogsAPIError(response.status_code, body)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, json_body=_omit_none(body))

    def _get(self, path: str) -> Any:
        return self._request("GET", path)

    def health(self) -> dict[str, Any]:
        """GET /health (unversioned)."""
        return self._get("/health")

    def get_config(self) -> dict[str, Any]:
        """GET /v1/config."""
        return self._get("/v1/config")

    def create_ingestion(
        self,
        *,
        idempotency_key: Optional[str] = None,
        **body: Any,
    ) -> dict[str, Any]:
        """POST /v1/ingestions — enqueue an ingest job.

        Pass ``callback_url`` (http/https) for an HMAC-signed completion POST
        when the batch worker reaches a terminal state. Pass
        ``idempotency_key`` to send an ``Idempotency-Key`` header so a retry
        within the TTL returns the original job.
        """
        extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request(
            "POST",
            "/v1/ingestions",
            json_body=_omit_none(body),
            extra_headers=extra,
        )

    def list_ingestions(self) -> dict[str, Any]:
        """GET /v1/ingestions."""
        return self._get("/v1/ingestions")

    def get_ingestion(self, ingestion_job_id: str) -> dict[str, Any]:
        """GET /v1/ingestions/{ingestion_job_id}."""
        return self._get(f"/v1/ingestions/{ingestion_job_id}")

    def push_lines(
        self,
        body: str,
        content_type: str = "application/x-ndjson",
    ) -> dict[str, Any]:
        """POST /v1/ingestions/lines — NDJSON of raw or pre-parsed log lines."""
        headers = self._headers()
        headers["Content-Type"] = content_type
        response = self._client.request(
            "POST",
            self._url("/v1/ingestions/lines"),
            headers=headers,
            content=body,
        )
        if response.status_code >= 400:
            try:
                err_body: Any = response.json()
            except ValueError:
                err_body = response.text
            raise RaglogsAPIError(response.status_code, err_body)
        return response.json()

    def pause_ingestion(self, ingestion_job_id: str) -> dict[str, Any]:
        """POST /v1/ingestions/{id}:pause — pause a tail job."""
        return self._post(f"/v1/ingestions/{ingestion_job_id}:pause", {})

    def resume_ingestion(self, ingestion_job_id: str) -> dict[str, Any]:
        """POST /v1/ingestions/{id}:resume — resume a paused tail job."""
        return self._post(f"/v1/ingestions/{ingestion_job_id}:resume", {})

    def stop_ingestion(self, ingestion_job_id: str) -> dict[str, Any]:
        """POST /v1/ingestions/{id}:stop — stop a tail job (terminal)."""
        return self._post(f"/v1/ingestions/{ingestion_job_id}:stop", {})

    def explain(
        self,
        *,
        since: Optional[str] = None,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        service: Optional[str] = None,
        env: Optional[str] = None,
        no_llm: bool = False,
        max_clusters: int = 10,
        baseline_window: Optional[str] = None,
        ingestion_job_id: Optional[str] = None,
        force_refresh: bool = False,
        format: str = "json",
    ) -> dict[str, Any]:
        """POST /v1/query/explain."""
        return self._post(
            "/v1/query/explain",
            {
                "since": since,
                "from_time": from_time,
                "to_time": to_time,
                "service": service,
                "env": env,
                "no_llm": no_llm,
                "max_clusters": max_clusters,
                "baseline_window": baseline_window,
                "ingestion_job_id": ingestion_job_id,
                "force_refresh": force_refresh,
                "format": format,
            },
        )

    def ask(
        self,
        *,
        question: str,
        since: Optional[str] = None,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        service: Optional[str] = None,
        ingestion_job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST /v1/query/ask."""
        return self._post(
            "/v1/query/ask",
            {
                "question": question,
                "since": since,
                "from_time": from_time,
                "to_time": to_time,
                "service": service,
                "ingestion_job_id": ingestion_job_id,
            },
        )

    def clusters(
        self,
        *,
        since: Optional[str] = None,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        service: Optional[str] = None,
        env: Optional[str] = None,
        top: int = 15,
        ingestion_job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST /v1/query/clusters."""
        return self._post(
            "/v1/query/clusters",
            {
                "since": since,
                "from_time": from_time,
                "to_time": to_time,
                "service": service,
                "env": env,
                "top": top,
                "ingestion_job_id": ingestion_job_id,
            },
        )

    def timeline(
        self,
        *,
        since: Optional[str] = None,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        service: Optional[str] = None,
        env: Optional[str] = None,
        all_ingestions: bool = False,
        ingestion_job_id: Optional[str] = None,
        format: str = "json",
    ) -> dict[str, Any]:
        """POST /v1/query/timeline."""
        return self._post(
            "/v1/query/timeline",
            {
                "since": since,
                "from_time": from_time,
                "to_time": to_time,
                "service": service,
                "env": env,
                "all_ingestions": all_ingestions,
                "ingestion_job_id": ingestion_job_id,
                "format": format,
            },
        )

    def compare(
        self,
        *,
        since: Optional[str] = None,
        baseline: Optional[str] = None,
        window_a_from: Optional[str] = None,
        window_a_to: Optional[str] = None,
        window_b_from: Optional[str] = None,
        window_b_to: Optional[str] = None,
        service: Optional[str] = None,
        env: Optional[str] = None,
        all_ingestions: bool = False,
        ingestion_job_id: Optional[str] = None,
        format: str = "json",
    ) -> dict[str, Any]:
        """POST /v1/query/compare."""
        return self._post(
            "/v1/query/compare",
            {
                "since": since,
                "baseline": baseline,
                "window_a_from": window_a_from,
                "window_a_to": window_a_to,
                "window_b_from": window_b_from,
                "window_b_to": window_b_to,
                "service": service,
                "env": env,
                "all_ingestions": all_ingestions,
                "ingestion_job_id": ingestion_job_id,
                "format": format,
            },
        )
