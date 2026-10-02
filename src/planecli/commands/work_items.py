"""Work item / issue CRUD commands."""

from __future__ import annotations

import asyncio
import datetime
from typing import Annotated

import cyclopts
from cyclopts import Parameter
from loguru import logger
from plane.errors import PlaneError
from rich.text import Text

from planecli.api.async_sdk import create_client, run_sdk
from planecli.api.client import get_client, get_workspace, handle_api_error
from planecli.formatters import output, output_single
from planecli.utils.colors import PRIORITY_COLORS, colorize, lighten_hex
from planecli.utils.resolve import (
    resolve_estimate_point_async,
    resolve_label_async,
    resolve_module_async,
    resolve_project_async,
    resolve_state_async,
    resolve_user_async,
    resolve_work_item_across_projects_async,
    resolve_work_item_async,
)

wi_app = cyclopts.App(
    name=["work-item", "wi", "issues", "issue"],
    help="Manage work items (issues).",
)

WI_COLUMNS = [
    ("sequence_id", "ID"),
    ("name", "Title"),
    ("priority", "Priority"),
    ("state_detail_name", "State"),
    ("assignee_names", "Assignees"),
    ("created_at", "Created"),
]


WI_FIELDS = [
    ("id", "UUID"),
    ("sequence_id", "Sequence ID"),
    ("name", "Title"),
    ("description_stripped", "Description"),
    ("priority", "Priority"),
    ("state_detail_name", "State"),
    ("assignee_names", "Assignees"),
    ("label_names", "Labels"),
    ("estimate_display", "Estimate"),
    ("start_date", "Start Date"),
    ("target_date", "Target Date"),
    ("created_at", "Created"),
    ("updated_at", "Updated"),
]


RELATION_COLUMNS = [
    ("identifier", "ID"),
    ("name", "Title"),
]


def _enrich_work_item(
    data: dict,
    *,
    state_map: dict[str, dict[str, str | None]] | None = None,
    member_map: dict[str, str] | None = None,
    label_map: dict[str, dict[str, str | None]] | None = None,
    project_identifier: str | None = None,
) -> dict:
    """Add convenience fields to a work item dict.

    Args:
        data: Work item dict from model_dump().
        state_map: Optional UUID->{"name": str, "color": str|None} mapping for states.
        member_map: Optional UUID->display_name mapping for workspace members.
        label_map: Optional UUID->{"name": str, "color": str|None} mapping for labels.
        project_identifier: Optional project identifier (e.g. "CHATFIN") for sequence IDs.
    """
    # Add sequence_id like CHATFIN-30
    if not project_identifier:
        project_detail = data.get("project_detail")
        project_identifier = (
            project_detail.get("identifier", "")
            if isinstance(project_detail, dict)
            else ""
        )
    seq = data.get("sequence_id", "")
    if project_identifier and seq:
        data["sequence_id"] = f"{project_identifier}-{seq}"

    # Priority - colorize with hardcoded color map; keep blank when unset
    priority_val = data.get("priority") or ""
    if priority_val and priority_val != "none" and isinstance(priority_val, str):
        data["priority"] = colorize(priority_val, PRIORITY_COLORS.get(priority_val))
    else:
        data["priority"] = ""

    # State name - try expanded object first, then lookup map, then raw value
    # Lighten "unstarted" group colors (e.g. Todo) to distinguish from "backlog"
    state_detail = data.get("state_detail") or data.get("state")
    if isinstance(state_detail, dict):
        state_name = state_detail.get("name", "")
        state_color = state_detail.get("color")
        if state_color and state_detail.get("group") == "unstarted":
            state_color = lighten_hex(state_color)
        data["state_detail_name"] = colorize(state_name, state_color)
    elif isinstance(state_detail, str) and state_map and state_detail in state_map:
        info = state_map[state_detail]
        state_color = info.get("color")
        if state_color and info.get("group") == "unstarted":
            state_color = lighten_hex(state_color)
        data["state_detail_name"] = colorize(info["name"] or "", state_color)
    elif isinstance(state_detail, str):
        data["state_detail_name"] = state_detail
    else:
        data["state_detail_name"] = ""

    # Assignee names - try expanded objects first, then lookup map
    assignees = data.get("assignees") or data.get("assignee_detail") or []
    if isinstance(assignees, list):
        names = []
        for a in assignees:
            if isinstance(a, dict):
                names.append(a.get("display_name") or a.get("first_name", ""))
            elif isinstance(a, str) and member_map and a in member_map:
                names.append(member_map[a])
            elif isinstance(a, str):
                names.append(a[:8])  # Show truncated UUID as fallback
        data["assignee_names"] = ", ".join(names) if names else ""
    else:
        data["assignee_names"] = ""

    # Label names - try expanded objects first, then lookup map (with per-label colors)
    labels = data.get("labels") or data.get("label_detail") or []
    if isinstance(labels, list):
        label_parts: list[str | Text] = []
        for lbl in labels:
            if isinstance(lbl, dict):
                label_parts.append(
                    colorize(lbl.get("name", ""), lbl.get("color"))
                )
            elif isinstance(lbl, str) and label_map and lbl in label_map:
                info = label_map[lbl]
                label_parts.append(
                    colorize(info["name"] or "", info.get("color"))
                )
            elif isinstance(lbl, str):
                label_parts.append(lbl)
        if label_parts:
            combined = Text()
            for i, part in enumerate(label_parts):
                if i > 0:
                    combined.append(", ")
                if isinstance(part, Text):
                    combined.append_text(part)
                else:
                    combined.append(str(part))
            data["label_names"] = combined
            data["label_detail_names"] = [str(part) for part in label_parts]
        else:
            data["label_names"] = ""
            data["label_detail_names"] = []
    else:
        data["label_names"] = ""
        data["label_detail_names"] = []

    # Description stripped
    desc_html = data.get("description_html") or ""
    if desc_html:
        import re

        data["description_stripped"] = re.sub(r"<[^>]+>", "", desc_html).strip()

    # Estimate display - handle expanded dict, raw UUID, or None
    ep = data.get("estimate_point")
    if isinstance(ep, dict) and ep.get("value"):
        data["estimate_display"] = ep["value"]
    elif isinstance(ep, str) and ep:
        data["estimate_display"] = ep  # UUID fallback
    else:
        data["estimate_display"] = ""

    return data


async def _resolve_project_id_async(project: str | None) -> str:
    """Resolve project flag to a project UUID (async)."""
    from planecli.exceptions import ValidationError

    if not project:
        raise ValidationError(
            "Project is required for this command.",
            hint="Use --project <name-or-id> to specify the project.",
        )
    client = get_client()
    workspace = get_workspace()
    resolved = await resolve_project_async(project, client, workspace)
    return resolved["id"]


def _validate_date(value: str | None, flag: str) -> str | None:
    """Validate a YYYY-MM-DD date flag; None passes through unchanged.

    Rejecting bad formats here matters more than for other flags: the API
    silently ignores a malformed or misnamed date field and answers 200
    (ADR-0007), so a typo would otherwise look like success.
    """
    if value is None:
        return None
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        from planecli.exceptions import ValidationError

        raise ValidationError(
            f"Invalid date for {flag}: {value!r} (expected YYYY-MM-DD).",
            hint=f"Example: {flag} 2026-09-15",
        )
    return value


async def _fetch_project_data(
    client, workspace: str, project_id: str
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch work items, states, and labels for a project in parallel.

    All three use caching. Work items have a short TTL (2m).
    """
    from planecli.cache import cached_list_labels, cached_list_states, cached_list_work_items

    items, states, labels = await asyncio.gather(
        cached_list_work_items(workspace, project_id),
        cached_list_states(workspace, project_id),
        cached_list_labels(workspace, project_id),
    )
    return items, states, labels


async def fetch_issue_relations(
    workspace: str, project_id: str, item_id: str
) -> dict[str, list[dict]]:
    """Fetch and enrich a work item's blocked_by/blocking relations.

    Single source of truth shared by `wi show` (and the relation write
    path for display labels). Related UUIDs resolve to identifier/name via
    the (already 2m-cached) project work items list.

    Returns {"blocked_by": [...], "blocking": [...]} where each entry is
    {"id", "identifier" (or None), "name"}. Unknown UUIDs degrade to a
    truncated-UUID name instead of losing the relation.

    Always raises on failure — callers own the failure policy.
    """
    from plane.errors import PlaneError

    from planecli.cache import (
        cached_get_relations,
        cached_list_projects,
        cached_list_work_items,
    )
    from planecli.exceptions import PlaneCLIError

    relations = await cached_get_relations(workspace, project_id, item_id)

    # Name resolution is a nicety, not the point of this call: if the
    # project lists fail to load, fall back to empty maps (entries then
    # degrade to truncated UUIDs) instead of losing the relations.
    try:
        items, projects = await asyncio.gather(
            cached_list_work_items(workspace, project_id),
            cached_list_projects(workspace),
        )
    except (PlaneError, PlaneCLIError):
        items, projects = [], []
    item_map = {i["id"]: i for i in items if i.get("id")}
    project_identifier = next(
        (p.get("identifier", "") for p in projects if p.get("id") == project_id),
        "",
    )

    def _enrich(ids: list[str] | None) -> list[dict]:
        rows = []
        for uid in ids or []:
            info = item_map.get(uid, {})
            name = info.get("name") or uid[:8]
            seq = info.get("sequence_id")
            identifier = (
                f"{project_identifier}-{seq}"
                if project_identifier and seq
                else None
            )
            rows.append({"id": uid, "identifier": identifier, "name": name})
        return rows

    return {
        "blocked_by": _enrich(relations.get("blocked_by")),
        "blocking": _enrich(relations.get("blocking")),
    }


async def _apply_block_relations(
    *,
    client,
    workspace: str,
    project_id: str,
    item_id: str,
    blocked_by: list[str] | None,
    unblocked_by: list[str] | None,
    project_flag: str | None,
) -> dict[str, list[str]]:
    """Add/remove "blocked by" relations on a work item.

    Each reference is a work item UUID, identifier (ABC-123), or name
    (names need --project, like the issue argument). Related items must
    live in the same project — the relations endpoint is project-scoped.
    Adds are idempotent; removals only touch relations that exist.

    Writes are verified by re-reading the relations (ADR-0007): a silent
    no-op from the API raises APIError instead of reporting success.

    Returns {"added": [...], "removed": [...]} with the display labels used.
    """
    from plane.errors import PlaneError
    from plane.models.work_items import CreateWorkItemRelation, RemoveWorkItemRelation

    from planecli.cache import cached_get_relations, invalidate_resource
    from planecli.exceptions import APIError, ValidationError

    async def _resolve_ids(refs: list[str] | None) -> list[tuple[str, str]]:
        resolved: list[tuple[str, str]] = []
        for ref in refs or []:
            label = ref.strip() if isinstance(ref, str) else ""
            if not label:
                raise ValidationError(
                    "Empty work item reference in a relation flag.",
                    hint="Pass a name, identifier (ABC-123), or UUID per flag.",
                )
            if project_flag:
                blocker = await resolve_work_item_async(
                    label, client, workspace, project_id
                )
                blocker_project = blocker.get("project") or project_id
            else:
                blocker, blocker_project = (
                    await resolve_work_item_across_projects_async(
                        label, client, workspace
                    )
                )
            if blocker_project != project_id:
                raise ValidationError(
                    f"Relation target {label!r} is in another project.",
                    hint="Relations only link work items of the same project.",
                )
            if blocker["id"] == item_id:
                raise ValidationError(
                    "A work item cannot block itself.",
                    hint=f"Remove {label!r} from the relation flags.",
                )
            if blocker["id"] not in [rid for rid, _ in resolved]:
                resolved.append((blocker["id"], label))
        return resolved

    try:
        to_add = await _resolve_ids(blocked_by)
        to_remove = await _resolve_ids(unblocked_by)
    except PlaneError as e:
        raise handle_api_error(e)

    overlap = {rid for rid, _ in to_add} & {rid for rid, _ in to_remove}
    if overlap:
        raise ValidationError(
            "A work item cannot be both added and removed in one command.",
            hint="Drop it from either --blocked-by or --unblocked-by.",
        )

    current = await cached_get_relations(workspace, project_id, item_id)
    existing = set(current.get("blocked_by") or [])
    add_ids = [(rid, label) for rid, label in to_add if rid not in existing]
    remove_ids = [(rid, label) for rid, label in to_remove if rid in existing]

    if add_ids:
        await run_sdk(
            client.work_items.relations.create,
            workspace,
            project_id,
            item_id,
            CreateWorkItemRelation(
                relation_type="blocked_by", issues=[rid for rid, _ in add_ids]
            ),
        )
    for rid, _ in remove_ids:
        await run_sdk(
            client.work_items.relations.delete,
            workspace,
            project_id,
            item_id,
            RemoveWorkItemRelation(related_issue=rid),
        )

    if add_ids or remove_ids:
        await invalidate_resource("relations", workspace, project_id, item_id)
        verified = await cached_get_relations(workspace, project_id, item_id)
        now = set(verified.get("blocked_by") or [])
        missing = [label for rid, label in add_ids if rid not in now]
        lingering = [label for rid, label in remove_ids if rid in now]
        if missing or lingering:
            problems = []
            if missing:
                problems.append(f"not added: {', '.join(missing)}")
            if lingering:
                problems.append(f"not removed: {', '.join(lingering)}")
            raise APIError(
                "The server did not apply the relation change "
                f"({'; '.join(problems)})."
            )

    from planecli.formatters import console

    if add_ids:
        console.print(
            f"[green]Blocked by {', '.join(label for _, label in add_ids)}.[/]"
        )
    if remove_ids:
        console.print(
            "[green]No longer blocked by "
            f"{', '.join(label for _, label in remove_ids)}.[/]"
        )

    return {
        "added": [label for _, label in add_ids],
        "removed": [label for _, label in remove_ids],
    }


@wi_app.command(name="list", alias="ls")
async def list_(
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    assignee: str | None = None,
    state: str | None = None,
    labels: str | None = None,
    parent: str | None = None,
    sort: str = "created",
    limit: Annotated[int, Parameter(alias="-l")] = 50,
    json: bool = False,
) -> None:
    """List work items.

    Parameters
    ----------
    project
        Project name, identifier, or UUID. If omitted, lists from all projects.
    assignee
        Filter by assignee name or 'me'.
    state
        Filter by state name (comma-separated).
    labels
        Filter by label name (comma-separated).
    parent
        Filter by parent work item identifier (ABC-123), UUID, or name
        (name requires --project).
    sort
        Sort by: created (default), updated.
    limit
        Maximum results to show.
    """
    try:
        client = get_client()
        workspace = get_workspace()

        # Workspace-scoped: fetch members once (cached)
        from planecli.cache import cached_list_members, cached_list_projects

        members = await cached_list_members(workspace)
        member_map = {}
        for m in members:
            if not m.get("id"):
                continue
            full_name = " ".join(
                p for p in [m.get("first_name", ""), m.get("last_name", "")]
                if p
            )
            member_map[m["id"]] = full_name or m.get("display_name", "")

        if project:
            proj = await resolve_project_async(project, client, workspace)
            projects_to_list = [proj]
        else:
            projects_to_list = await cached_list_projects(workspace)

        # Fetch all projects in parallel (each project fetches items+states+labels in parallel)
        data = []
        if projects_to_list:
            # Use fresh client instances for parallel project fetching
            async def _fetch_and_enrich(proj_dict: dict) -> list[dict]:
                proj_client = create_client()
                project_id = proj_dict["id"]
                proj_identifier = proj_dict.get("identifier", "")

                items, states, labels_list = await _fetch_project_data(
                    proj_client, workspace, project_id
                )

                if not items:
                    return []

                # states and labels are already dicts (from cache)
                state_map = {
                    s["id"]: {
                        "name": s.get("name"),
                        "color": s.get("color"),
                        "group": s.get("group"),
                    }
                    for s in states if s.get("id") and s.get("name")
                }
                label_map = {
                    lb["id"]: {"name": lb.get("name"), "color": lb.get("color")}
                    for lb in labels_list if lb.get("id") and lb.get("name")
                }

                enriched_items = []
                for i in items:
                    item_dict = i if isinstance(i, dict) else i.model_dump()
                    enriched = _enrich_work_item(
                        item_dict,
                        state_map=state_map,
                        member_map=member_map,
                        label_map=label_map,
                        project_identifier=proj_identifier,
                    )
                    enriched["project_identifier"] = proj_identifier
                    enriched_items.append(enriched)
                return enriched_items

            results = await asyncio.gather(
                *[_fetch_and_enrich(p) for p in projects_to_list],
                return_exceptions=True,
            )

            from planecli.formatters import console

            for proj_dict, result in zip(projects_to_list, results):
                if isinstance(result, Exception):
                    if project:
                        raise result
                    console.print(
                        f"[yellow]Warning: Failed to fetch "
                        f"{proj_dict.get('identifier', proj_dict['id'])}: {result}[/]"
                    )
                    continue
                data.extend(result)
    except PlaneError as e:
        raise handle_api_error(e)

    # Filter by assignee
    if assignee:
        user = await resolve_user_async(assignee, client, workspace)
        user_id = user["id"]
        data = [
            d for d in data
            if user_id in (d.get("assignees") or [])
            or any(
                (isinstance(a, dict) and a.get("id") == user_id)
                for a in (d.get("assignees") or [])
            )
        ]

    # Filter by state (comma-separated, OR logic, substring match)
    if state:
        state_tokens = [s.strip().lower() for s in state.split(",") if s.strip()]
        if state_tokens:
            data = [
                d for d in data
                if any(
                    token in str(d.get("state_detail_name") or "").lower()
                    for token in state_tokens
                )
            ]

    # Filter by parent work item
    if parent:
        if project:
            project_id = projects_to_list[0]["id"] if projects_to_list else None
            if project_id:
                parent_data = await resolve_work_item_async(
                    parent, client, workspace, project_id
                )
            else:
                parent_data, _ = await resolve_work_item_across_projects_async(
                    parent, client, workspace
                )
        else:
            parent_data, _ = await resolve_work_item_across_projects_async(
                parent, client, workspace
            )
        parent_id = parent_data["id"]
        data = [d for d in data if d.get("parent") == parent_id]

    # Filter by labels (comma-separated, OR logic, substring match)
    if labels:
        label_tokens = [ln.strip().lower() for ln in labels.split(",") if ln.strip()]
        if label_tokens:
            data = [
                d for d in data
                if any(
                    token in name.lower()
                    for token in label_tokens
                    for name in (d.get("label_detail_names") or [])
                )
            ]

    # Sort
    if sort == "updated":
        data.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    else:
        data.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    data = data[:limit]
    columns = WI_COLUMNS
    output(data, columns, title="Work Items", as_json=json)


@wi_app.command(alias="read")
async def show(
    issue: str,
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    json: bool = False,
    no_comments: bool = False,
    no_relations: bool = False,
) -> None:
    """Show work item details.

    Parameters
    ----------
    issue
        Work item identifier (ABC-123), UUID, or name.
    project
        Project name/ID (required for name-based lookup).
    no_comments
        Skip fetching the work item's comments.
    no_relations
        Skip fetching the work item's blocked-by/blocking relations.
    """
    try:
        client = get_client()
        workspace = get_workspace()

        if project:
            project_id = await _resolve_project_id_async(project)
            data = await resolve_work_item_async(issue, client, workspace, project_id)
        else:
            data, _ = await resolve_work_item_across_projects_async(
                issue, client, workspace
            )

        # Re-fetch with expanded estimate_point for display
        if data.get("id") and data.get("project"):
            expanded = await run_sdk(
                client.work_items._get,
                f"{workspace}/projects/{data['project']}/work-items/{data['id']}",
                {"expand": "estimate_point"},
            )
            data["estimate_point"] = expanded.get("estimate_point")
    except PlaneError as e:
        raise handle_api_error(e)

    # The detail endpoint returns state/labels/assignees as bare UUIDs (SDK
    # model mismatch, ADR-0003). Build the same lookup maps wi ls uses so the
    # *_name fields hold real names here too. Failure degrades to raw UUIDs.
    state_map: dict[str, dict[str, str | None]] = {}
    member_map: dict[str, str] = {}
    label_map: dict[str, dict[str, str | None]] = {}
    if data.get("project"):
        try:
            from planecli.cache import (
                cached_list_labels,
                cached_list_members,
                cached_list_states,
            )

            members, states, labels_list = await asyncio.gather(
                cached_list_members(workspace),
                cached_list_states(workspace, data["project"]),
                cached_list_labels(workspace, data["project"]),
            )
            member_map = {
                m["id"]: " ".join(
                    p for p in [m.get("first_name", ""), m.get("last_name", "")]
                    if p
                )
                or m.get("display_name", "")
                for m in members
                if m.get("id")
            }
            state_map = {
                s["id"]: {
                    "name": s.get("name"),
                    "color": s.get("color"),
                    "group": s.get("group"),
                }
                for s in states
                if s.get("id") and s.get("name")
            }
            label_map = {
                lb["id"]: {"name": lb.get("name"), "color": lb.get("color")}
                for lb in labels_list
                if lb.get("id") and lb.get("name")
            }
        except PlaneError:
            pass

    data = _enrich_work_item(
        data,
        state_map=state_map,
        member_map=member_map,
        label_map=label_map,
    )

    # Comments are a SECONDARY enrichment: fetched in their own block OUTSIDE the
    # resolve/estimate try above, so a comment API failure degrades to null
    # (ADR-0006) instead of hitting the outer handler and aborting the command.
    if not no_comments and data.get("id") and data.get("project"):
        from planecli.commands.comments import fetch_issue_comments
        from planecli.exceptions import PlaneCLIError
        try:
            data["comments"] = await fetch_issue_comments(
                workspace, data["project"], data["id"]
            )
        except (PlaneError, PlaneCLIError) as exc:
            logger.warning("Failed to fetch comments: {}", exc)
            data["comments"] = None  # signal failure, do NOT abort

    # Relations are a SECONDARY enrichment too: same degrade-to-null policy.
    if not no_relations and data.get("id") and data.get("project"):
        from planecli.exceptions import PlaneCLIError

        try:
            relations = await fetch_issue_relations(
                workspace, data["project"], data["id"]
            )
            data["blocked_by"] = relations["blocked_by"]
            data["blocking"] = relations["blocking"]
        except (PlaneError, PlaneCLIError) as exc:
            logger.warning("Failed to fetch relations: {}", exc)
            data["blocked_by"] = None  # signal failure, do NOT abort
            data["blocking"] = None

    output_single(data, WI_FIELDS, title="Work Item Details", as_json=json)

    # Human-readable Comments section (non-JSON only, so stdout stays clean under --json).
    # Gated on the same id/project check as the fetch above, so we only ever
    # render "(failed to load)" when a fetch was actually attempted and failed.
    if not json and not no_comments and data.get("id") and data.get("project"):
        from planecli.commands.comments import COMMENT_COLUMNS
        from planecli.formatters import console

        comments = data.get("comments")
        if comments:
            output(comments, COMMENT_COLUMNS, title="Comments")
        elif comments == []:
            console.print("Comments: (none)")
        else:  # None -> fetch failed
            console.print("Comments: (failed to load)")

    # Human-readable relations sections, same gating and policy as comments.
    if not json and not no_relations and data.get("id") and data.get("project"):
        from planecli.formatters import console

        for key, title in (("blocked_by", "Blocked by"), ("blocking", "Blocking")):
            rows = data.get(key)
            if rows:
                output(rows, RELATION_COLUMNS, title=title)
            elif rows == []:
                console.print(f"{title}: (none)")
            else:  # None -> fetch failed
                console.print(f"{title}: (failed to load)")


@wi_app.command(alias="new")
async def create(
    title: str,
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    assignee: Annotated[str | None, Parameter(name=["--assignee", "--assign"])] = None,
    state: str | None = None,
    labels: str | None = None,
    priority: Annotated[str | None, Parameter()] = None,
    module: str | None = None,
    parent: str | None = None,
    estimate: Annotated[int | None, Parameter(alias="-e")] = None,
    description: Annotated[str | None, Parameter(alias="-d")] = None,
    image: Annotated[list[str] | None, Parameter(alias="-i")] = None,
    force: bool = False,
    start_date: str | None = None,
    target_date: str | None = None,
    blocked_by: list[str] | None = None,
    json: bool = False,
) -> None:
    """Create a new work item.

    Parameters
    ----------
    title
        Work item title.
    project
        Project name, identifier, or UUID (required).
    assignee
        Assignee name, email, or 'me'.
    state
        State name (e.g. 'Todo', 'In Progress').
    labels
        Comma-separated label names.
    priority
        Priority: urgent, high, medium, low, none. Also accepts 0-4 (0=none, 1=urgent).
    module
        Module name or UUID to add the item to.
    parent
        Parent work item identifier (ABC-123) for creating sub-issues.
    estimate
        Story point estimate.
    description
        Work item description (plain text).
    image
        Image file path to embed in the description (repeatable). The image is
        uploaded as an attachment and an img tag is appended to the description.
    force
        Upload images even if an attachment with the same file name already exists.
    start_date
        Start date (YYYY-MM-DD). Defaults to the creation date when omitted.
    target_date
        Target end date (YYYY-MM-DD).
    blocked_by
        Work item that blocks the new one, by identifier (ABC-123), UUID, or
        name (repeatable, one per flag). Relations are added after creation.
    """
    from plane.models.work_items import CreateWorkItem

    start_date = _validate_date(start_date, "--start-date")
    target_date = _validate_date(target_date, "--target-date")

    try:
        client = get_client()
        workspace = get_workspace()

        # Resolve project and assignee in parallel (independent)
        parallel_tasks = [_resolve_project_id_async(project)]
        if assignee:
            parallel_tasks.append(resolve_user_async(assignee, client, workspace))
        if parent:
            parallel_tasks.append(
                resolve_work_item_across_projects_async(parent, client, workspace)
            )

        parallel_results = await asyncio.gather(*parallel_tasks)

        project_id = parallel_results[0]
        idx = 1

        create_data = CreateWorkItem(name=title)

        if description and not image:
            create_data.description_html = f"<p>{description}</p>"

        if start_date:
            create_data.start_date = start_date

        if target_date:
            create_data.target_date = target_date

        if priority:
            priority_map = {"0": "none", "1": "urgent", "2": "high", "3": "medium", "4": "low"}
            create_data.priority = priority_map.get(priority, priority.lower())

        if assignee:
            user = parallel_results[idx]
            create_data.assignees = [user["id"]]
            idx += 1

        if parent:
            parent_data, _ = parallel_results[idx]
            create_data.parent = parent_data["id"]
            idx += 1

        # Resolve state, labels, and estimate (depend on project_id) in parallel
        dependent_tasks = []
        dep_keys = []
        if state:
            dependent_tasks.append(
                resolve_state_async(state, client, workspace, project_id)
            )
            dep_keys.append("state")
        if estimate is not None:
            dependent_tasks.append(
                resolve_estimate_point_async(str(estimate), workspace, project_id)
            )
            dep_keys.append("estimate")
        if labels:
            label_names = [ln.strip() for ln in labels.split(",") if ln.strip()]
            for ln in label_names:
                dependent_tasks.append(
                    resolve_label_async(ln, client, workspace, project_id)
                )
                dep_keys.append("label")

        if dependent_tasks:
            dep_results = await asyncio.gather(*dependent_tasks)
            label_ids = []
            for key, result in zip(dep_keys, dep_results):
                if key == "state":
                    create_data.state = result["id"]
                elif key == "estimate":
                    create_data.estimate_point = result["id"]
                elif key == "label":
                    label_ids.append(result["id"])
            if label_ids:
                create_data.labels = label_ids

        item = await run_sdk(client.work_items.create, workspace, project_id, create_data)
        data = _enrich_work_item(item.model_dump())

        # Invalidate work items cache for this project
        from planecli.cache import invalidate_resource

        await invalidate_resource("work_items", workspace, project_id)

        # Add to module if specified
        if module:
            module_data = await resolve_module_async(module, client, workspace, project_id)
            await run_sdk(
                client.modules.add_work_items,
                workspace, project_id, module_data["id"], [data["id"]],
            )

        # Embed images: upload each, then append <img> tags to the description
        # (set here, not in create, because uploads need the issue to exist).
        if image:
            from plane.models.work_items import UpdateWorkItem

            from planecli.commands.attachments import embed_html, upload_embed_image

            parts = [f"<p>{description}</p>"] if description else []
            for path in image:
                asset_id = await upload_embed_image(
                    client, workspace, project_id, data["id"], path, force=force
                )
                parts.append(embed_html(asset_id))
            await run_sdk(
                client.work_items.update,
                workspace, project_id, data["id"],
                UpdateWorkItem(description_html="".join(parts)),
            )
            data["description_html"] = "".join(parts)
            data = _enrich_work_item(data)

        # Relations are added after the item exists and verified by
        # re-reading them (ADR-0007).
        if blocked_by:
            await _apply_block_relations(
                client=client,
                workspace=workspace,
                project_id=project_id,
                item_id=data["id"],
                blocked_by=blocked_by,
                unblocked_by=None,
                project_flag=project,
            )

    except PlaneError as e:
        raise handle_api_error(e)

    output_single(data, WI_FIELDS, title="Work Item Created", as_json=json)


@wi_app.command
async def update(
    issue: str,
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    state: str | None = None,
    priority: str | None = None,
    assignee: Annotated[str | None, Parameter(name=["--assignee", "--assign"])] = None,
    labels: str | None = None,
    clear_labels: bool = False,
    name: str | None = None,
    estimate: Annotated[int | None, Parameter(alias="-e")] = None,
    description: Annotated[str | None, Parameter(alias="-d")] = None,
    image: Annotated[list[str] | None, Parameter(alias="-i")] = None,
    force: bool = False,
    module: str | None = None,
    start_date: str | None = None,
    target_date: str | None = None,
    blocked_by: list[str] | None = None,
    unblocked_by: list[str] | None = None,
    json: bool = False,
) -> None:
    """Update a work item.

    Parameters
    ----------
    issue
        Work item identifier (ABC-123), UUID, or name.
    project
        Project name/ID (required for name-based lookup).
    state
        New state name.
    priority
        New priority: urgent, high, medium, low, none.
    assignee
        New assignee name or 'me'.
    labels
        Comma-separated labels to set.
    clear_labels
        Remove all labels.
    name
        New title.
    estimate
        Story point estimate.
    description
        New description (plain text).
    image
        Image file path to embed in the description (repeatable). Uploaded as
        an attachment and appended to the existing description, or to the new
        --description if one is given.
    force
        Upload images even if an attachment with the same file name already exists.
    module
        Module name or UUID to add the item to.
    start_date
        New start date (YYYY-MM-DD).
    target_date
        New target end date (YYYY-MM-DD).
    blocked_by
        Work item that now blocks this one, by identifier (ABC-123), UUID, or
        name (repeatable, one per flag). Adds relations, never removes them.
    unblocked_by
        Work item that no longer blocks this one (repeatable, one per flag).
        Only removes existing blocked-by relations; never prompts.
    """
    from plane.models.work_items import UpdateWorkItem

    start_date = _validate_date(start_date, "--start-date")
    target_date = _validate_date(target_date, "--target-date")

    try:
        client = get_client()
        workspace = get_workspace()

        # Resolve the work item
        if project:
            project_id = await _resolve_project_id_async(project)
            item_data = await resolve_work_item_async(issue, client, workspace, project_id)
        else:
            item_data, project_id = await resolve_work_item_across_projects_async(
                issue, client, workspace
            )

        item_id = item_data["id"]
        update_data = UpdateWorkItem()

        if name:
            update_data.name = name

        # Embed images: upload each, then append <img> tags — to the new
        # --description when given, otherwise to the existing one.
        if image:
            from planecli.commands.attachments import embed_html, upload_embed_image

            parts = [f"<p>{description}</p>"] if description else [
                item_data.get("description_html") or ""
            ]
            for path in image:
                asset_id = await upload_embed_image(
                    client, workspace, project_id, item_id, path, force=force
                )
                parts.append(embed_html(asset_id))
            update_data.description_html = "".join(parts)

        if description and not image:
            update_data.description_html = f"<p>{description}</p>"

        if priority:
            priority_map = {"0": "none", "1": "urgent", "2": "high", "3": "medium", "4": "low"}
            update_data.priority = priority_map.get(priority, priority.lower())

        # Resolve assignee, state, labels, and estimate in parallel
        parallel_tasks = []
        task_keys = []

        if assignee:
            parallel_tasks.append(resolve_user_async(assignee, client, workspace))
            task_keys.append("assignee")
        if state:
            parallel_tasks.append(
                resolve_state_async(state, client, workspace, project_id)
            )
            task_keys.append("state")
        if estimate is not None:
            parallel_tasks.append(
                resolve_estimate_point_async(str(estimate), workspace, project_id)
            )
            task_keys.append("estimate")
        if labels and not clear_labels:
            label_names = [ln.strip() for ln in labels.split(",") if ln.strip()]
            for ln in label_names:
                parallel_tasks.append(
                    resolve_label_async(ln, client, workspace, project_id)
                )
                task_keys.append("label")

        if parallel_tasks:
            results = await asyncio.gather(*parallel_tasks)
            label_ids = []
            for key, result in zip(task_keys, results):
                if key == "assignee":
                    update_data.assignees = [result["id"]]
                elif key == "state":
                    update_data.state = result["id"]
                elif key == "estimate":
                    update_data.estimate_point = result["id"]
                elif key == "label":
                    label_ids.append(result["id"])
            if labels and not clear_labels and label_ids:
                update_data.labels = label_ids

        if clear_labels:
            update_data.labels = []

        if start_date is not None:
            update_data.start_date = start_date

        if target_date is not None:
            update_data.target_date = target_date

        updated = await run_sdk(
            client.work_items.update, workspace, project_id, item_id, update_data
        )
        data = _enrich_work_item(updated.model_dump())

        # Verify the server actually applied the dates (ADR-0007: Plane silently
        # ignores unknown/mis-spelled fields with a 200, so a missing echo means
        # the write was dropped, not applied).
        for flag, asked, got in (
            ("--start-date", start_date, data.get("start_date")),
            ("--target-date", target_date, data.get("target_date")),
        ):
            if asked is not None and got != asked:
                from planecli.exceptions import APIError

                raise APIError(
                    f"The server did not apply {flag}: asked {asked!r}, got {got!r}."
                )

        # Relations are applied after the field update and verified by
        # re-reading them (ADR-0007); _apply_block_relations raises APIError
        # when the server silently ignores the change.
        if blocked_by or unblocked_by:
            await _apply_block_relations(
                client=client,
                workspace=workspace,
                project_id=project_id,
                item_id=item_id,
                blocked_by=blocked_by,
                unblocked_by=unblocked_by,
                project_flag=project,
            )

        # Add to module if specified (same additive semantics as create)
        if module:
            module_data = await resolve_module_async(module, client, workspace, project_id)
            await run_sdk(
                client.modules.add_work_items,
                workspace, project_id, module_data["id"], [item_id],
            )

        # Invalidate work items cache for this project
        from planecli.cache import invalidate_resource

        await invalidate_resource("work_items", workspace, project_id)
    except PlaneError as e:
        raise handle_api_error(e)

    output_single(data, WI_FIELDS, title="Work Item Updated", as_json=json)


@wi_app.command
async def delete(
    issue: str,
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
) -> None:
    """Delete a work item.

    Parameters
    ----------
    issue
        Work item identifier (ABC-123), UUID, or name.
    project
        Project name/ID (required for name-based lookup).
    """
    from planecli.formatters import console

    try:
        client = get_client()
        workspace = get_workspace()

        if project:
            project_id = await _resolve_project_id_async(project)
            item_data = await resolve_work_item_async(issue, client, workspace, project_id)
        else:
            item_data, project_id = await resolve_work_item_across_projects_async(
                issue, client, workspace
            )

        item_id = item_data["id"]
        item_name = item_data.get("name", item_id)

        await run_sdk(client.work_items.delete, workspace, project_id, item_id)

        # Invalidate work items cache for this project
        from planecli.cache import invalidate_resource

        await invalidate_resource("work_items", workspace, project_id)
    except PlaneError as e:
        raise handle_api_error(e)

    console.print(f"[green]Work item '{item_name}' deleted.[/]")


@wi_app.command
async def search(
    query: str,
    *,
    project: Annotated[str | None, Parameter(alias="-p")] = None,
    sort: str = "created",
    limit: Annotated[int, Parameter(alias="-l")] = 20,
    json: bool = False,
) -> None:
    """Search work items by text.

    Parameters
    ----------
    query
        Search query string.
    project
        Limit search to a specific project.
    sort
        Sort by: created (default), updated.
    limit
        Maximum results to show.
    """
    try:
        client = get_client()
        workspace = get_workspace()

        result = await run_sdk(client.work_items.search, workspace, query)
        results_data = result.model_dump() if hasattr(result, "model_dump") else result

        # Extract work items from search results
        items = []
        if isinstance(results_data, dict):
            items = results_data.get("results", [])
        elif isinstance(results_data, list):
            items = results_data

        data = [_enrich_work_item(i if isinstance(i, dict) else i.model_dump()) for i in items]
    except PlaneError as e:
        raise handle_api_error(e)

    data = data[:limit]
    output(data, WI_COLUMNS, title=f"Search Results: '{query}'", as_json=json)


@wi_app.command
async def assign(
    issue: str,
    *,
    assignee: Annotated[str, Parameter(name=["--assignee", "--assign"])] = "me",
    project: Annotated[str | None, Parameter(alias="-p")] = None,
) -> None:
    """Assign a work item (defaults to yourself).

    Parameters
    ----------
    issue
        Work item identifier (ABC-123), UUID, or name.
    assignee
        Assignee name, email, or 'me' (default).
    project
        Project name/ID (required for name-based lookup).
    """
    from plane.models.work_items import UpdateWorkItem

    from planecli.formatters import console

    try:
        client = get_client()
        workspace = get_workspace()

        if project:
            project_id = await _resolve_project_id_async(project)
            item_data = await resolve_work_item_async(issue, client, workspace, project_id)
        else:
            item_data, project_id = await resolve_work_item_across_projects_async(
                issue, client, workspace
            )

        item_id = item_data["id"]
        user = await resolve_user_async(assignee, client, workspace)

        update_data = UpdateWorkItem(assignees=[user["id"]])
        await run_sdk(
            client.work_items.update, workspace, project_id, item_id, update_data
        )
    except PlaneError as e:
        raise handle_api_error(e)

    user_name = user.get("display_name") or user.get("first_name", "user")
    console.print(f"[green]Work item assigned to {user_name}.[/]")
