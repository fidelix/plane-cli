---
status: accepted
date: 2026-11-23
decision-makers: PlaneCLI maintainers
---

# ADR-0009: Work item search uses the advanced-search endpoint

## Context and Problem Statement

`planecli wi search <text>` printed an empty list for every query, including text that
plainly existed in work item titles, while `wi list` showed the same items. Two independent
defects combined:

1. **Wrong response key.** The command called `client.work_items.search()` and read
   `results` from the payload. The SDK model for that endpoint (`WorkItemSearch`) declares
   `issues`, so `results` was never present and the CLI always built an empty list — even
   when the API had data.
2. **Dead endpoint.** Even reading `issues` would not have helped: on current Plane
   servers `GET /workspaces/{slug}/work-items/search/?q=...` answers
   `{"issues": []}` for *every* query. The same is true of the older `issues/search/`
   path, with or without a `project_id`. The endpoint is effectively retired.

The command also accepted `-p/--project` but never sent it anywhere, so the flag could not
narrow anything, and the `--sort updated` option was a no-op.

The endpoint that does work is `POST /workspaces/{slug}/work-items/advanced-search/`,
which the SDK exposes as `client.work_items.advanced_search()` with an
`AdvancedSearchWorkItem(query=..., filters=..., limit=...)` body. It supports a
`project_id` filter inside `filters.and`, honours `limit`, and returns newest-first.

## Considered Options

- **A. Use `advanced-search` for text queries.** Send `query`, an optional `project_id`
  filter, and `limit`; enrich the hits with state names from the cached states list.
- **B. Keep `work-items/search/` and fix only the response key.** Cheapest patch, but the
  endpoint returns nothing, so search would stay broken.
- **C. Fetch every project's work items and filter locally.** Reuses `wi list` data, but
  costs one paginated crawl per project per search and still cannot sort by relevance.

## Decision Outcome

Chosen option: **A**, because it is the only option backed by an endpoint that returns
results, and it moves both the text match and the project filter to the server.

The contract:

- `wi search` calls `client.work_items.advanced_search`, never the legacy
  `work_items.search`, and never reads `results` (the search payload has no such key).
- `-p/--project` is resolved with `resolve_project_async` (name, identifier, or UUID) and
  sent as `{"and": [{"project_id": <uuid>}]}`; without `-p` the filter is omitted and the
  search spans the workspace, matching the README example and the flag's documented
  meaning.
- `--limit` is sent to the API. The API answers with at most that many hits, newest first.
- Search results carry only `id`, `name`, `sequence_id`, `project_identifier`,
  `project_id`, `state_id`, `priority`, and the two date fields — no `created_at`,
  `updated_at`, assignees, or labels. The table therefore shows ID, title, priority,
  state, and project; the full record is one `wi show` away. The JSON payload keeps the
  raw hit fields.
- State names come from `cached_list_states` per project present in the hits. That is
  secondary enrichment: a failed states fetch leaves the State column blank rather than
  hiding the results or leaking a raw UUID (see [ADR-0006](0006-secondary-enrichment-degrades-to-null.md)).
- `--sort updated` raises `ValidationError` (exit code 5) instead of being silently
  ignored: the endpoint has no timestamps and always returns newest-first. `created`
  (the default) is the only supported value.

## Confirmation

`tests/test_commands/test_work_items.py::TestWiSearch` covers the response being consumed
from `advanced_search`, the project filter reaching the request body, the workspace-wide
path leaving the filter unset, graceful degradation when the states fetch fails, and the
rejection of `updated`/non-positive limits. The `NOTE:` comment at the call site records
why the legacy endpoint is not used.

## Consequences

**Positive:**
- `wi search` returns the work items the API actually matches, scoped by `-p`.
- The project filter no longer costs a client-side pass over every item.

**Negative:**
- Search output is narrower than `wi list` (no dates, assignees, or labels) because the
  endpoint does not return those fields.
- `--sort updated` is rejected; time-ordered views stay with `wi list --sort updated`.
- The endpoint is version-dependent: on a Plane deployment without
  `advanced-search` the command will surface an API error rather than falling back.
