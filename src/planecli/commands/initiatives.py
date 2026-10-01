"""Initiative commands (workspace-scoped).

The SDK initiative models are not JSON-serializable (the InitiativeState
enum breaks json.dumps), so all endpoints go through the raw HTTP helpers
in planecli.api.raw (ADR-0003 escape hatch).
"""

from __future__ import annotations

from typing import Annotated

import cyclopts
from cyclopts import Parameter
from plane.errors import PlaneError

from planecli.api.client import get_client, get_workspace, handle_api_error
from planecli.exceptions import ValidationError
from planecli.formatters import output, output_single
from planecli.utils.resolve import resolve_initiative_async, resolve_user_async

initiative_app = cyclopts.App(
    name=["initiative", "initiatives"],
    help="Manage initiatives.",
)

# Valid initiative states (Plane InitiativeState).
INITIATIVE_STATES = (
    "DRAFT",
    "PLANNED",
    "ACTIVE",
    "COMPLETED",
    "CLOSED",
)


def _normalize_state(value: str) -> str:
    """Normalize and validate an initiative state (case-insensitive)."""
    key = value.strip().upper()
    if key not in INITIATIVE_STATES:
        allowed = ", ".join(s.lower() for s in INITIATIVE_STATES)
        raise ValidationError(
            f"Invalid state '{value}'.",
            hint=f"Valid values: {allowed}.",
        )
    return key


INITIATIVE_COLUMNS = [
    ("name", "Name"),
    ("state", "State"),
    ("start_date", "Start"),
    ("end_date", "End"),
]

INITIATIVE_FIELDS = [
    ("id", "UUID"),
    ("name", "Name"),
    ("description_text", "Description"),
    ("state", "State"),
    ("lead_name", "Lead"),
    ("start_date", "Start Date"),
    ("end_date", "End Date"),
    ("created_at", "Created"),
    ("updated_at", "Updated"),
]


def _enrich_initiative(data: dict, member_map: dict[str, str] | None = None) -> dict:
    """Add convenience fields to an initiative dict."""
    desc_html = data.get("description_html") or ""
    if desc_html:
        import re

        data["description_text"] = re.sub(r"<[^>]+>", "", desc_html).strip()
    else:
        data["description_text"] = ""

    lead = data.get("lead")
    if isinstance(lead, str) and member_map and lead in member_map:
        data["lead_name"] = member_map[lead]
    elif isinstance(lead, str) and lead:
        data["lead_name"] = lead[:8]  # truncated UUID fallback
    else:
        data["lead_name"] = ""
    return data


async def _member_map(workspace: str) -> dict[str, str]:
    """Workspace member UUIDs to display names (degrades to empty on failure)."""
    from planecli.cache import cached_list_members
    from planecli.exceptions import PlaneCLIError

    try:
        members = await cached_list_members(workspace)
    except (PlaneError, PlaneCLIError):
        return {}
    return {
        m["id"]: " ".join(
            p for p in [m.get("first_name", ""), m.get("last_name", "")] if p
        )
        or m.get("display_name", "")
        for m in members
        if m.get("id")
    }


@initiative_app.command(name="list", alias="ls")
async def list_(
    *,
    sort: str = "created",
    limit: Annotated[int, Parameter(alias="-l")] = 50,
    json: bool = False,
) -> None:
    """List initiatives in the workspace.

    Parameters
    ----------
    sort
        Sort by: created (default), updated.
    limit
        Maximum results to show.
    """
    from planecli.cache import cached_list_initiatives

    try:
        workspace = get_workspace()
        initiatives = await cached_list_initiatives(workspace)
    except PlaneError as e:
        raise handle_api_error(e)

    data = [_enrich_initiative(i) for i in initiatives]

    if sort == "updated":
        data.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    else:
        data.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    data = data[:limit]
    output(data, INITIATIVE_COLUMNS, title="Initiatives", as_json=json)


@initiative_app.command(alias="read")
async def show(
    initiative: str,
    *,
    json: bool = False,
) -> None:
    """Show initiative details.

    Parameters
    ----------
    initiative
        Initiative name or UUID.
    """
    try:
        client = get_client()
        workspace = get_workspace()
        data = await resolve_initiative_async(initiative, client, workspace)
    except PlaneError as e:
        raise handle_api_error(e)

    data = _enrich_initiative(data, await _member_map(workspace))
    output_single(data, INITIATIVE_FIELDS, title="Initiative Details", as_json=json)


@initiative_app.command(alias="new")
async def create(
    name: str,
    *,
    description: Annotated[str | None, Parameter(alias="-d")] = None,
    start_date: str | None = None,
    end_date: str | None = None,
    state: str | None = None,
    lead: str | None = None,
    json: bool = False,
) -> None:
    """Create a new initiative.

    Parameters
    ----------
    name
        Initiative name.
    description
        Initiative description (plain text).
    start_date
        Start date (YYYY-MM-DD).
    end_date
        End date (YYYY-MM-DD).
    state
        Initiative state: draft, planned, active, completed, closed.
    lead
        Lead name, email, or 'me'.
    """
    from planecli.api.raw import api_post, initiative_base
    from planecli.commands.work_items import _validate_date

    try:
        normalized_state = _normalize_state(state) if state is not None else None
        validated_start = _validate_date(start_date, "--start-date")
        validated_end = _validate_date(end_date, "--end-date")

        client = get_client()
        workspace = get_workspace()

        payload: dict = {"name": name}
        if description:
            payload["description_html"] = f"<p>{description}</p>"
        if validated_start:
            payload["start_date"] = validated_start
        if validated_end:
            payload["end_date"] = validated_end
        if normalized_state:
            payload["state"] = normalized_state
        if lead is not None:
            user = await resolve_user_async(lead, client, workspace)
            payload["lead"] = user["id"]

        data = await api_post(
            initiative_base(workspace) + "/",
            payload,
            action="create initiative",
            resource="Initiative",
            ref=name,
        )

        from planecli.cache import invalidate_resource

        await invalidate_resource("initiatives", workspace)
    except PlaneError as e:
        raise handle_api_error(e)

    data = _enrich_initiative(data, await _member_map(workspace))
    output_single(data, INITIATIVE_FIELDS, title="Initiative Created", as_json=json)


@initiative_app.command
async def update(
    initiative: str,
    *,
    name: str | None = None,
    description: Annotated[str | None, Parameter(alias="-d")] = None,
    start_date: str | None = None,
    end_date: str | None = None,
    state: str | None = None,
    lead: str | None = None,
    json: bool = False,
) -> None:
    """Update an initiative.

    Parameters
    ----------
    initiative
        Initiative name or UUID.
    name
        New initiative name.
    description
        New description (plain text).
    start_date
        New start date (YYYY-MM-DD).
    end_date
        New end date (YYYY-MM-DD).
    state
        New state: draft, planned, active, completed, closed.
    lead
        New lead name, email, or 'me'.
    """
    from planecli.api.raw import api_patch, initiative_base
    from planecli.commands.work_items import _validate_date
    from planecli.exceptions import APIError

    try:
        normalized_state = _normalize_state(state) if state is not None else None
        validated_start = _validate_date(start_date, "--start-date")
        validated_end = _validate_date(end_date, "--end-date")

        client = get_client()
        workspace = get_workspace()
        init_data = await resolve_initiative_async(initiative, client, workspace)
        initiative_id = init_data["id"]

        payload: dict = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description_html"] = f"<p>{description}</p>"
        if validated_start is not None:
            payload["start_date"] = validated_start
        if validated_end is not None:
            payload["end_date"] = validated_end
        if normalized_state is not None:
            payload["state"] = normalized_state
        if lead is not None:
            user = await resolve_user_async(lead, client, workspace)
            payload["lead"] = user["id"]

        data = await api_patch(
            f"{initiative_base(workspace)}/{initiative_id}/",
            payload,
            action="update initiative",
            resource="Initiative",
            ref=initiative,
        )

        # Verify the server applied the scalar fields (ADR-0007: a 200 is
        # not always a write).
        for flag, asked, got in (
            ("--name", name, data.get("name")),
            ("--state", normalized_state, data.get("state")),
            ("--start-date", validated_start, data.get("start_date")),
            ("--end-date", validated_end, data.get("end_date")),
        ):
            if asked is not None and got != asked:
                raise APIError(
                    f"The server did not apply {flag}: asked {asked!r}, got {got!r}."
                )

        from planecli.cache import invalidate_resource

        await invalidate_resource("initiatives", workspace)
    except PlaneError as e:
        raise handle_api_error(e)

    data = _enrich_initiative(data, await _member_map(workspace))
    output_single(data, INITIATIVE_FIELDS, title="Initiative Updated", as_json=json)


@initiative_app.command
async def delete(
    initiative: str,
) -> None:
    """Delete an initiative.

    Parameters
    ----------
    initiative
        Initiative name or UUID.
    """
    from planecli.api.raw import api_delete, initiative_base
    from planecli.formatters import console

    try:
        client = get_client()
        workspace = get_workspace()
        init_data = await resolve_initiative_async(initiative, client, workspace)
        initiative_id = init_data["id"]
        initiative_name = init_data.get("name", initiative_id)

        await api_delete(
            f"{initiative_base(workspace)}/{initiative_id}/",
            action="delete initiative",
            resource="Initiative",
            ref=initiative,
        )

        from planecli.cache import invalidate_resource

        await invalidate_resource("initiatives", workspace)
    except PlaneError as e:
        raise handle_api_error(e)

    console.print(f"[green]Initiative '{initiative_name}' deleted.[/]")
