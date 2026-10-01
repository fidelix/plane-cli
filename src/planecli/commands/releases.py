"""Release commands.

Releases group work items under a named version and can be scoped to a
project (-p) or to the whole workspace (no -p). The SDK has no release
resource at all, so every endpoint goes through the raw HTTP helpers in
planecli.api.raw (ADR-0003 escape hatch).
"""

from __future__ import annotations

from typing import Annotated

import cyclopts
from cyclopts import Parameter
from plane.errors import PlaneError

from planecli.api.client import get_client, get_workspace, handle_api_error
from planecli.exceptions import ValidationError
from planecli.formatters import output, output_single
from planecli.utils.resolve import resolve_project_async, resolve_user_async

release_app = cyclopts.App(
    name=["release", "releases"],
    help="Manage releases.",
)

# Valid release statuses (Plane API).
RELEASE_STATUSES = (
    "unreleased",
    "released",
    "cancelled",
)


def _normalize_status(value: str) -> str:
    """Normalize and validate a release status (case-insensitive)."""
    key = value.strip().lower()
    if key not in RELEASE_STATUSES:
        allowed = ", ".join(RELEASE_STATUSES)
        raise ValidationError(
            f"Invalid status '{value}'.",
            hint=f"Valid values: {allowed}.",
        )
    return key


RELEASE_COLUMNS = [
    ("name", "Name"),
    ("status", "Status"),
    ("target_date", "Target"),
    ("release_date", "Released"),
]

RELEASE_FIELDS = [
    ("id", "UUID"),
    ("name", "Name"),
    ("description_text", "Description"),
    ("status", "Status"),
    ("project_display", "Project"),
    ("lead_name", "Lead"),
    ("target_date", "Target Date"),
    ("release_date", "Release Date"),
    ("created_at", "Created"),
    ("updated_at", "Updated"),
]


def _enrich_release(
    data: dict,
    member_map: dict[str, str] | None = None,
    project_identifier: str | None = None,
) -> dict:
    """Add convenience fields to a release dict."""
    desc = data.get("description") or {}
    stripped = desc.get("description_stripped") if isinstance(desc, dict) else None
    data["description_text"] = (stripped or "").strip()

    lead = data.get("lead")
    if isinstance(lead, str) and member_map and lead in member_map:
        data["lead_name"] = member_map[lead]
    elif isinstance(lead, str) and lead:
        data["lead_name"] = lead[:8]  # truncated UUID fallback
    else:
        data["lead_name"] = ""

    data["project_display"] = project_identifier or "(workspace)"
    return data


async def _resolve_scope(project: str | None) -> str | None:
    """Resolve the -p flag to a project UUID, or None for workspace scope."""
    if not project:
        return None
    client = get_client()
    workspace = get_workspace()
    proj = await resolve_project_async(project, client, workspace)
    return proj["id"]


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


async def _project_identifier(workspace: str, project_id: str | None) -> str | None:
    """Project identifier for a scope, or None for workspace-level releases."""
    if not project_id:
        return None
    from planecli.cache import cached_list_projects
    from planecli.exceptions import PlaneCLIError

    try:
        projects = await cached_list_projects(workspace)
    except (PlaneError, PlaneCLIError):
        return None
    return next(
        (p.get("identifier") for p in projects if p.get("id") == project_id),
        None,
    )


@release_app.command(name="list", alias="ls")
async def list_(
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    sort: str = "created",
    limit: Annotated[int, Parameter(alias="-l")] = 50,
    json: bool = False,
) -> None:
    """List releases.

    Parameters
    ----------
    project
        Project name, identifier, or UUID. If omitted, lists
        workspace-level releases only (project releases need -p).
    sort
        Sort by: created (default), updated.
    limit
        Maximum results to show.
    """
    from planecli.cache import cached_list_releases

    try:
        workspace = get_workspace()
        project_id = await _resolve_scope(project)
        releases = await cached_list_releases(workspace, project_id)
    except PlaneError as e:
        raise handle_api_error(e)

    data = [_enrich_release(r) for r in releases]

    if sort == "updated":
        data.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    else:
        data.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    data = data[:limit]
    output(data, RELEASE_COLUMNS, title="Releases", as_json=json)


@release_app.command(alias="read")
async def show(
    release: str,
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    json: bool = False,
) -> None:
    """Show release details.

    Parameters
    ----------
    release
        Release name or UUID.
    project
        Project name, identifier, or UUID. If omitted, looks up
        workspace-level releases only.
    """
    from planecli.utils.resolve import resolve_release_async

    try:
        client = get_client()
        workspace = get_workspace()
        project_id = await _resolve_scope(project)
        data = await resolve_release_async(release, client, workspace, project_id)
    except PlaneError as e:
        raise handle_api_error(e)

    data = _enrich_release(
        data,
        await _member_map(workspace),
        await _project_identifier(workspace, project_id),
    )
    output_single(data, RELEASE_FIELDS, title="Release Details", as_json=json)


@release_app.command(alias="new")
async def create(
    name: str,
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    description: Annotated[str | None, Parameter(alias="-d")] = None,
    status: str | None = None,
    target_date: str | None = None,
    release_date: str | None = None,
    lead: str | None = None,
    json: bool = False,
) -> None:
    """Create a new release.

    Parameters
    ----------
    name
        Release name (unique within the workspace).
    project
        Project name, identifier, or UUID. If omitted, creates a
        workspace-level release.
    description
        Release description (plain text).
    status
        Release status: unreleased, released, cancelled.
    target_date
        Target date (YYYY-MM-DD).
    release_date
        Actual release date (YYYY-MM-DD).
    lead
        Lead name, email, or 'me'.
    """
    from planecli.api.raw import api_post, release_base
    from planecli.commands.work_items import _validate_date

    try:
        normalized_status = _normalize_status(status) if status is not None else None
        validated_target = _validate_date(target_date, "--target-date")
        validated_released = _validate_date(release_date, "--release-date")

        client = get_client()
        workspace = get_workspace()
        project_id = await _resolve_scope(project)

        payload: dict = {"name": name}
        if description:
            payload["description_html"] = f"<p>{description}</p>"
        if normalized_status:
            payload["status"] = normalized_status
        if validated_target:
            payload["target_date"] = validated_target
        if validated_released:
            payload["release_date"] = validated_released
        if lead is not None:
            user = await resolve_user_async(lead, client, workspace)
            payload["lead"] = user["id"]

        data = await api_post(
            release_base(workspace, project_id) + "/",
            payload,
            action="create release",
            resource="Release",
            ref=name,
        )

        from planecli.cache import invalidate_resource

        await invalidate_resource("releases", workspace, project_id)
    except PlaneError as e:
        raise handle_api_error(e)

    data = _enrich_release(
        data,
        await _member_map(workspace),
        await _project_identifier(workspace, project_id),
    )
    output_single(data, RELEASE_FIELDS, title="Release Created", as_json=json)


@release_app.command
async def update(
    release: str,
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    name: str | None = None,
    description: Annotated[str | None, Parameter(alias="-d")] = None,
    status: str | None = None,
    target_date: str | None = None,
    release_date: str | None = None,
    lead: str | None = None,
    json: bool = False,
) -> None:
    """Update a release.

    Parameters
    ----------
    release
        Release name or UUID.
    project
        Project name, identifier, or UUID. If omitted, looks up
        workspace-level releases only.
    name
        New release name (unique within the workspace).
    description
        New description (plain text).
    status
        New status: unreleased, released, cancelled.
    target_date
        New target date (YYYY-MM-DD).
    release_date
        New actual release date (YYYY-MM-DD).
    lead
        New lead name, email, or 'me'.
    """
    from planecli.api.raw import api_patch, release_base
    from planecli.commands.work_items import _validate_date
    from planecli.exceptions import APIError
    from planecli.utils.resolve import resolve_release_async

    try:
        normalized_status = _normalize_status(status) if status is not None else None
        validated_target = _validate_date(target_date, "--target-date")
        validated_released = _validate_date(release_date, "--release-date")

        client = get_client()
        workspace = get_workspace()
        project_id = await _resolve_scope(project)
        rel_data = await resolve_release_async(release, client, workspace, project_id)
        release_id = rel_data["id"]

        payload: dict = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description_html"] = f"<p>{description}</p>"
        if normalized_status is not None:
            payload["status"] = normalized_status
        if validated_target is not None:
            payload["target_date"] = validated_target
        if validated_released is not None:
            payload["release_date"] = validated_released
        if lead is not None:
            user = await resolve_user_async(lead, client, workspace)
            payload["lead"] = user["id"]

        data = await api_patch(
            f"{release_base(workspace, project_id)}/{release_id}/",
            payload,
            action="update release",
            resource="Release",
            ref=release,
        )

        # Verify the server applied the scalar fields (ADR-0007: a 200 is
        # not always a write).
        for flag, asked, got in (
            ("--name", name, data.get("name")),
            ("--status", normalized_status, data.get("status")),
            ("--target-date", validated_target, data.get("target_date")),
            ("--release-date", validated_released, data.get("release_date")),
        ):
            if asked is not None and got != asked:
                raise APIError(
                    f"The server did not apply {flag}: asked {asked!r}, got {got!r}."
                )

        from planecli.cache import invalidate_resource

        await invalidate_resource("releases", workspace, project_id)
    except PlaneError as e:
        raise handle_api_error(e)

    data = _enrich_release(
        data,
        await _member_map(workspace),
        await _project_identifier(workspace, project_id),
    )
    output_single(data, RELEASE_FIELDS, title="Release Updated", as_json=json)


@release_app.command
async def delete(
    release: str,
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
) -> None:
    """Delete a release.

    Parameters
    ----------
    release
        Release name or UUID.
    project
        Project name, identifier, or UUID. If omitted, deletes a
        workspace-level release.
    """
    from planecli.api.raw import api_delete, release_base
    from planecli.formatters import console
    from planecli.utils.resolve import resolve_release_async

    try:
        client = get_client()
        workspace = get_workspace()
        project_id = await _resolve_scope(project)
        rel_data = await resolve_release_async(release, client, workspace, project_id)
        release_id = rel_data["id"]
        release_name = rel_data.get("name", release_id)

        await api_delete(
            f"{release_base(workspace, project_id)}/{release_id}/",
            action="delete release",
            resource="Release",
            ref=release,
        )

        from planecli.cache import invalidate_resource

        await invalidate_resource("releases", workspace, project_id)
    except PlaneError as e:
        raise handle_api_error(e)

    console.print(f"[green]Release '{release_name}' deleted.[/]")
