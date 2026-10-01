"""Tests for release commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from planecli.commands.releases import (
    RELEASE_STATUSES,
    _enrich_release,
    _normalize_status,
)
from planecli.exceptions import APIError, ValidationError


@pytest.mark.parametrize("status", RELEASE_STATUSES)
def test_normalize_status_canonical_values(status: str) -> None:
    assert _normalize_status(status) == status


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Released", "released"), ("  UNRELEASED ", "unreleased")],
)
def test_normalize_status_case_insensitive(value: str, expected: str) -> None:
    assert _normalize_status(value) == expected


@pytest.mark.parametrize("value", ["shipped", "done", ""])
def test_normalize_status_invalid_raises(value: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _normalize_status(value)
    assert "unreleased" in (exc.value.hint or "")


def test_enrich_release_fields() -> None:
    data = _enrich_release(
        {
            "project": "p1",
            "lead": "lead-uuid-1",
            "description": {"description_stripped": "  Notes. "},
        },
        {"lead-uuid-1": "Patrick"},
        "ENG",
    )
    assert data["description_text"] == "Notes."
    assert data["lead_name"] == "Patrick"
    assert data["project_display"] == "ENG"

    data = _enrich_release({"project": None, "lead": None, "description": None})
    assert data["project_display"] == "(workspace)"
    assert data["lead_name"] == ""


class TestReleaseCommands:
    def _item(self):
        return {
            "id": "rel-1",
            "name": "v2.4.0",
            "status": "unreleased",
            "target_date": "2026-12-31",
            "release_date": None,
            "project": "p1",
            "lead": "user-1",
            "description": {"description_stripped": "Spring release."},
        }

    def _mock_scope(self, mock_scope):
        mock_scope.return_value = "p1"

    @patch("planecli.cache.invalidate_resource", new_callable=AsyncMock)
    @patch("planecli.cache.cached_list_members", new_callable=AsyncMock)
    @patch("planecli.cache.cached_list_projects", new_callable=AsyncMock)
    @patch("planecli.commands.releases.output_single")
    @patch("planecli.api.raw.api_post", new_callable=AsyncMock)
    @patch("planecli.commands.releases._resolve_scope", new_callable=AsyncMock)
    @patch("planecli.commands.releases.get_workspace", return_value="ws")
    @patch("planecli.commands.releases.get_client")
    async def test_create_project_scope(
        self,
        mock_client,
        mock_ws,
        mock_scope,
        mock_post,
        mock_out,
        mock_projects,
        mock_members,
        mock_inv,
    ):
        from planecli.commands.releases import create

        mock_client.return_value = MagicMock()
        self._mock_scope(mock_scope)
        mock_members.return_value = []
        mock_projects.return_value = [{"id": "p1", "identifier": "ENG"}]
        mock_post.return_value = self._item()

        await create(
            "v2.4.0",
            project="ENG",
            description="Spring release.",
            status="released",
            target_date="2026-12-31",
        )

        url, payload = mock_post.call_args[0][0], mock_post.call_args[0][1]
        assert url.endswith("/workspaces/ws/projects/p1/releases/")
        assert payload == {
            "name": "v2.4.0",
            "description_html": "<p>Spring release.</p>",
            "status": "released",
            "target_date": "2026-12-31",
        }
        mock_inv.assert_awaited_once_with("releases", "ws", "p1")
        assert mock_out.call_args[0][0]["project_display"] == "ENG"

    @patch("planecli.cache.invalidate_resource", new_callable=AsyncMock)
    @patch("planecli.cache.cached_list_members", new_callable=AsyncMock)
    @patch("planecli.cache.cached_list_projects", new_callable=AsyncMock)
    @patch("planecli.commands.releases.output_single")
    @patch("planecli.api.raw.api_post", new_callable=AsyncMock)
    @patch("planecli.commands.releases._resolve_scope", new_callable=AsyncMock)
    @patch("planecli.commands.releases.get_workspace", return_value="ws")
    @patch("planecli.commands.releases.get_client")
    async def test_create_workspace_scope(
        self,
        mock_client,
        mock_ws,
        mock_scope,
        mock_post,
        mock_out,
        mock_projects,
        mock_members,
        mock_inv,
    ):
        from planecli.commands.releases import create

        mock_client.return_value = MagicMock()
        mock_scope.return_value = None
        mock_members.return_value = []
        mock_post.return_value = dict(self._item(), project=None)

        await create("v2.4.0")

        url, payload = mock_post.call_args[0][0], mock_post.call_args[0][1]
        assert url.endswith("/workspaces/ws/releases/")
        assert "projects" not in url
        assert payload == {"name": "v2.4.0"}
        mock_inv.assert_awaited_once_with("releases", "ws", None)
        assert mock_out.call_args[0][0]["project_display"] == "(workspace)"

    @patch("planecli.commands.releases.output_single")
    @patch("planecli.api.raw.api_post", new_callable=AsyncMock)
    @patch("planecli.commands.releases._resolve_scope", new_callable=AsyncMock)
    @patch("planecli.commands.releases.get_workspace", return_value="ws")
    @patch("planecli.commands.releases.get_client")
    async def test_create_invalid_status_rejected(
        self, mock_client, mock_ws, mock_scope, mock_post, mock_out
    ):
        from planecli.commands.releases import create

        mock_client.return_value = MagicMock()
        mock_scope.return_value = "p1"

        with pytest.raises(ValidationError):
            await create("v2.4.0", project="ENG", status="shipped")
        mock_post.assert_not_awaited()

    @patch("planecli.cache.invalidate_resource", new_callable=AsyncMock)
    @patch("planecli.cache.cached_list_members", new_callable=AsyncMock)
    @patch("planecli.cache.cached_list_projects", new_callable=AsyncMock)
    @patch("planecli.commands.releases.output_single")
    @patch("planecli.api.raw.api_patch", new_callable=AsyncMock)
    @patch("planecli.utils.resolve.resolve_release_async", new_callable=AsyncMock)
    @patch("planecli.commands.releases._resolve_scope", new_callable=AsyncMock)
    @patch("planecli.commands.releases.get_workspace", return_value="ws")
    @patch("planecli.commands.releases.get_client")
    async def test_update_happy_path(
        self,
        mock_client,
        mock_ws,
        mock_scope,
        mock_resolve,
        mock_patch,
        mock_out,
        mock_projects,
        mock_members,
        mock_inv,
    ):
        from planecli.commands.releases import update

        mock_client.return_value = MagicMock()
        self._mock_scope(mock_scope)
        mock_resolve.return_value = self._item()
        mock_members.return_value = []
        mock_projects.return_value = [{"id": "p1", "identifier": "ENG"}]
        mock_patch.return_value = dict(self._item(), status="released")

        await update("v2.4.0", project="ENG", status="released")

        url, payload = mock_patch.call_args[0][0], mock_patch.call_args[0][1]
        assert url.endswith("/workspaces/ws/projects/p1/releases/rel-1/")
        assert payload == {"status": "released"}

    @patch("planecli.commands.releases.output_single")
    @patch("planecli.api.raw.api_patch", new_callable=AsyncMock)
    @patch("planecli.utils.resolve.resolve_release_async", new_callable=AsyncMock)
    @patch("planecli.commands.releases._resolve_scope", new_callable=AsyncMock)
    @patch("planecli.commands.releases.get_workspace", return_value="ws")
    @patch("planecli.commands.releases.get_client")
    async def test_update_silent_noop_raises(
        self, mock_client, mock_ws, mock_scope, mock_resolve, mock_patch, mock_out
    ):
        from planecli.commands.releases import update

        mock_client.return_value = MagicMock()
        self._mock_scope(mock_scope)
        mock_resolve.return_value = self._item()
        mock_patch.return_value = dict(self._item(), status="unreleased")

        with pytest.raises(APIError):
            await update("v2.4.0", project="ENG", status="released")

    @patch("planecli.commands.releases.output")
    @patch("planecli.cache.cached_list_releases", new_callable=AsyncMock)
    @patch("planecli.commands.releases._resolve_scope", new_callable=AsyncMock)
    @patch("planecli.commands.releases.get_workspace", return_value="ws")
    async def test_list_passes_scope_to_cache(
        self, mock_ws, mock_scope, mock_cached, mock_out
    ):
        from planecli.commands.releases import list_

        self._mock_scope(mock_scope)
        mock_cached.return_value = [dict(self._item(), created_at="2026-01-01")]

        await list_(project="ENG")

        mock_cached.assert_awaited_once_with("ws", "p1")
        assert mock_out.call_args[0][0][0]["name"] == "v2.4.0"

    @patch("planecli.api.raw.api_delete", new_callable=AsyncMock)
    @patch("planecli.utils.resolve.resolve_release_async", new_callable=AsyncMock)
    @patch("planecli.cache.invalidate_resource", new_callable=AsyncMock)
    @patch("planecli.commands.releases._resolve_scope", new_callable=AsyncMock)
    @patch("planecli.commands.releases.get_workspace", return_value="ws")
    @patch("planecli.commands.releases.get_client")
    async def test_delete_happy_path(
        self, mock_client, mock_ws, mock_scope, mock_inv, mock_resolve, mock_delete
    ):
        from planecli.commands.releases import delete

        mock_client.return_value = MagicMock()
        self._mock_scope(mock_scope)
        mock_resolve.return_value = self._item()

        await delete("v2.4.0", project="ENG")

        url = mock_delete.call_args[0][0]
        assert url.endswith("/workspaces/ws/projects/p1/releases/rel-1/")
        mock_inv.assert_awaited_once_with("releases", "ws", "p1")


class TestResolveRelease:
    @patch("planecli.api.raw.api_get", new_callable=AsyncMock)
    async def test_uuid_fetches_directly(self, mock_get):
        from planecli.utils.resolve import resolve_release_async

        uid = "12345678-1234-1234-1234-123456789012"
        mock_get.return_value = {"id": uid, "name": "v1"}
        result = await resolve_release_async(uid, MagicMock(), "ws", "p1")
        assert result["id"] == uid
        assert f"/projects/p1/releases/{uid}/" in mock_get.call_args[0][0]

    @patch("planecli.cache.cached_list_releases", new_callable=AsyncMock)
    async def test_name_fuzzy_matches(self, mock_cached):
        from planecli.utils.resolve import resolve_release_async

        mock_cached.return_value = [{"id": "r1", "name": "v2.4.0 Spring"}]
        result = await resolve_release_async("spring", MagicMock(), "ws", "p1")
        assert result["id"] == "r1"
        mock_cached.assert_awaited_once_with("ws", "p1")

    @patch("planecli.cache.cached_list_releases", new_callable=AsyncMock)
    async def test_name_not_found(self, mock_cached):
        from planecli.exceptions import ResourceNotFoundError
        from planecli.utils.resolve import resolve_release_async

        mock_cached.return_value = [{"id": "r1", "name": "v1.0"}]
        with pytest.raises(ResourceNotFoundError):
            await resolve_release_async("zzz-no-match", MagicMock(), "ws", "p1")
