"""Raw HTTP helpers for Plane endpoints the SDK does not cover (ADR-0003).

Uses requests + X-Api-Key through run_sdk (concurrency limit), mirroring
the documents escape hatch. Known gaps: releases have no SDK resource at
all, and the SDK initiative models are not JSON-serializable (the
InitiativeState enum breaks json.dumps), so both resources go through here.
"""

from __future__ import annotations

from typing import Any

import requests

from planecli.api.async_sdk import run_sdk
from planecli.exceptions import (
    APIError,
    AuthenticationError,
    ResourceNotFoundError,
    ValidationError,
)

_TIMEOUT = 30


def api_base(workspace: str) -> str:
    """Base URL for workspace-scoped API paths."""
    from planecli.api.client import get_config

    config = get_config()
    return f"{config.base_url}/api/v1/workspaces/{workspace}"


def auth_headers(*, json: bool = False) -> dict[str, str]:
    """Auth headers for raw API calls."""
    from planecli.api.client import get_config

    config = get_config()
    headers = {"X-Api-Key": config.api_key}
    if json:
        headers["Content-Type"] = "application/json"
    return headers


def release_base(workspace: str, project_id: str | None) -> str:
    """Endpoint base for releases: project-scoped with -p, workspace-level without."""
    base = api_base(workspace)
    if project_id:
        return f"{base}/projects/{project_id}/releases"
    return f"{base}/releases"


def initiative_base(workspace: str) -> str:
    """Endpoint base for (workspace-scoped) initiatives."""
    return f"{api_base(workspace)}/initiatives"


def checked(
    resp: requests.Response, *, action: str, resource: str = "Resource", ref: str = ""
) -> Any:
    """Map a raw response to JSON (or None) or raise a typed CLI error.

    Never returns an unchecked error body: 404 becomes ResourceNotFoundError
    (exit 3), auth/plan problems become AuthenticationError/APIError (exit
    2/4), and known validation codes become ValidationError (exit 5).
    """
    if resp.status_code in (200, 201):
        return resp.json() if resp.content else None
    if resp.status_code == 204:
        return None
    body = (resp.text or "")[:300]
    if resp.status_code == 404:
        raise ResourceNotFoundError(resource, ref or action)
    if resp.status_code == 401:
        raise AuthenticationError()
    if resp.status_code == 403:
        raise APIError(f"{action} forbidden (HTTP 403): {body}", status_code=403)
    if resp.status_code == 402:
        raise APIError(
            f"{action} is not enabled for this workspace (HTTP 402 Payment Required).",
            status_code=402,
        )
    if resp.status_code == 400 and "RELEASE_NAME_ALREADY_EXISTS" in body:
        raise ValidationError(
            f"A release named {ref!r} already exists in this scope.",
            hint="Release names must be unique within the workspace.",
        )
    raise APIError(f"{action} failed: {body}", status_code=resp.status_code)


async def api_get(url: str, *, action: str, resource: str = "Resource", ref: str = "") -> Any:
    """GET a raw endpoint and return the checked JSON body."""
    resp = await run_sdk(requests.get, url, headers=auth_headers(), timeout=_TIMEOUT)
    return checked(resp, action=action, resource=resource, ref=ref)


async def get_all_pages(url: str, *, action: str) -> list[dict]:
    """GET all cursor pages of a raw list endpoint."""
    out: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"per_page": 100}
        if cursor:
            params["cursor"] = cursor
        resp = await run_sdk(
            requests.get, url, headers=auth_headers(), params=params, timeout=_TIMEOUT
        )
        page = checked(resp, action=action)
        out.extend(page.get("results", []))
        if not page.get("next_page_results"):
            break
        cursor = page.get("next_cursor")
    return out


async def api_post(
    url: str, payload: dict, *, action: str, resource: str = "Resource", ref: str = ""
) -> Any:
    """POST a raw endpoint and return the checked JSON body."""
    resp = await run_sdk(
        requests.post, url, headers=auth_headers(json=True), json=payload, timeout=_TIMEOUT
    )
    return checked(resp, action=action, resource=resource, ref=ref)


async def api_patch(
    url: str, payload: dict, *, action: str, resource: str = "Resource", ref: str = ""
) -> Any:
    """PATCH a raw endpoint and return the checked JSON body."""
    resp = await run_sdk(
        requests.patch,
        url,
        headers=auth_headers(json=True),
        json=payload,
        timeout=_TIMEOUT,
    )
    return checked(resp, action=action, resource=resource, ref=ref)


async def api_delete(url: str, *, action: str, resource: str = "Resource", ref: str = "") -> None:
    """DELETE a raw endpoint (204 expected)."""
    resp = await run_sdk(requests.delete, url, headers=auth_headers(), timeout=_TIMEOUT)
    checked(resp, action=action, resource=resource, ref=ref)
