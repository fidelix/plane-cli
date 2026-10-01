"""Tests for initiative commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from planecli.commands.initiatives import (
    INITIATIVE_STATES,
    _enrich_initiative,
    _normalize_state,
)
from planecli.exceptions import APIError, ValidationError


@pytest.mark.parametrize("state", INITIATIVE_STATES)
def test_normalize_state_canonical_values(state: str) -> None:
    assert _normalize_state(state) == state


@pytest.mark.parametrize(
    ("value", "expected"),
    [("active", "ACTIVE"), ("  Planned ", "PLANNED"), ("completed", "COMPLETED")],
)
def test_normalize_state_case_insensitive(value: str, expected: str) -> None:
    assert _normalize_state(value) == expected


@pytest.mark.parametrize("value", ["done", "archived", ""])
def test_normalize_state_invalid_raises(value: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _normalize_state(value)
    assert "active" in (exc.value.hint or "")


def test_enrich_initiative_lead_fallback() -> None:
    data = _enrich_initiative(
        {"lead": "lead-uuid-123456", "description_html": "<p>Hi</p>"},
        {"lead-uuid-123456": "Patrick"},
    )
    assert data["lead_name"] == "Patrick"
    assert data["description_text"] == "Hi"

    data = _enrich_initiative({"lead": "ghost-uuid-9999"})
    assert data["lead_name"] == "ghost-uu"
    assert data["description_text"] == ""


class TestInitiativeCommands:
    def _item(self):
        return {
            "id": "init-1",
            "name": "Q1 Launch",
            "state": "ACTIVE",
            "start_date": "2026-10-01",
            "end_date": "2026-12-31",
            "description_html": "<p>Big launch.</p>",
            "lead": "user-1",
        }

    @patch("planecli.cache.invalidate_resource", new_callable=AsyncMock)
    @patch("planecli.cache.cached_list_members", new_callable=AsyncMock)
    @patch("planecli.commands.initiatives.output_single")
    @patch("planecli.api.raw.api_post", new_callable=AsyncMock)
    @patch("planecli.commands.initiatives.get_workspace", return_value="ws")
    @patch("planecli.commands.initiatives.get_client")
    async def test_create_happy_path(
        self, mock_client, mock_ws, mock_post, mock_out, mock_members, mock_inv
    ):
        from planecli.commands.initiatives import create

        mock_client.return_value = MagicMock()
        mock_members.return_value = []
        mock_post.return_value = self._item()

        await create(
            "Q1 Launch",
            description="Big launch.",
            state="active",
            start_date="2026-10-01",
            end_date="2026-12-31",
        )

        assert mock_post.call_count == 1
        url, payload = mock_post.call_args[0][0], mock_post.call_args[0][1]
        assert url.endswith("/workspaces/ws/initiatives/")
        assert payload == {
            "name": "Q1 Launch",
            "description_html": "<p>Big launch.</p>",
            "start_date": "2026-10-01",
            "end_date": "2026-12-31",
            "state": "ACTIVE",
        }
        mock_inv.assert_awaited_once_with("initiatives", "ws")
        data = mock_out.call_args[0][0]
        assert data["description_text"] == "Big launch."

    @patch("planecli.commands.initiatives.output_single")
    @patch("planecli.api.raw.api_post", new_callable=AsyncMock)
    @patch("planecli.commands.initiatives.get_workspace", return_value="ws")
    @patch("planecli.commands.initiatives.get_client")
    async def test_create_invalid_state_rejected(
        self, mock_client, mock_ws, mock_post, mock_out
    ):
        from planecli.commands.initiatives import create

        mock_client.return_value = MagicMock()

        with pytest.raises(ValidationError):
            await create("Q1 Launch", state="bogus")
        mock_post.assert_not_awaited()

    @patch("planecli.cache.invalidate_resource", new_callable=AsyncMock)
    @patch("planecli.cache.cached_list_members", new_callable=AsyncMock)
    @patch("planecli.commands.initiatives.output_single")
    @patch("planecli.api.raw.api_patch", new_callable=AsyncMock)
    @patch(
        "planecli.commands.initiatives.resolve_initiative_async",
        new_callable=AsyncMock,
    )
    @patch("planecli.commands.initiatives.get_workspace", return_value="ws")
    @patch("planecli.commands.initiatives.get_client")
    async def test_update_happy_path(
        self,
        mock_client,
        mock_ws,
        mock_resolve,
        mock_patch,
        mock_out,
        mock_members,
        mock_inv,
    ):
        from planecli.commands.initiatives import update

        mock_client.return_value = MagicMock()
        mock_resolve.return_value = self._item()
        mock_members.return_value = []
        updated = dict(self._item(), state="COMPLETED")
        mock_patch.return_value = updated

        await update("Q1 Launch", state="completed")

        url, payload = mock_patch.call_args[0][0], mock_patch.call_args[0][1]
        assert url.endswith("/workspaces/ws/initiatives/init-1/")
        assert payload == {"state": "COMPLETED"}
        assert mock_out.call_args[0][0]["state"] == "COMPLETED"

    @patch("planecli.commands.initiatives.output_single")
    @patch("planecli.api.raw.api_patch", new_callable=AsyncMock)
    @patch(
        "planecli.commands.initiatives.resolve_initiative_async",
        new_callable=AsyncMock,
    )
    @patch("planecli.commands.initiatives.get_workspace", return_value="ws")
    @patch("planecli.commands.initiatives.get_client")
    async def test_update_silent_noop_raises(
        self, mock_client, mock_ws, mock_resolve, mock_patch, mock_out
    ):
        from planecli.commands.initiatives import update

        mock_client.return_value = MagicMock()
        mock_resolve.return_value = self._item()
        mock_patch.return_value = dict(self._item(), state="ACTIVE")  # ignored change

        with pytest.raises(APIError):
            await update("Q1 Launch", state="completed")

    @patch("planecli.cache.invalidate_resource", new_callable=AsyncMock)
    @patch("planecli.commands.initiatives.output")
    @patch("planecli.cache.cached_list_initiatives", new_callable=AsyncMock)
    @patch("planecli.commands.initiatives.get_workspace", return_value="ws")
    @patch("planecli.commands.initiatives.get_client")
    async def test_list_sorts_and_limits(
        self, mock_client, mock_ws, mock_cached, mock_out, mock_inv
    ):
        from planecli.commands.initiatives import list_

        mock_client.return_value = MagicMock()
        mock_cached.return_value = [
            {"id": "a", "name": "Old", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "b", "name": "New", "created_at": "2026-06-01T00:00:00Z"},
        ]

        await list_(limit=1)

        data = mock_out.call_args[0][0]
        assert [r["name"] for r in data] == ["New"]

    @patch("planecli.commands.initiatives.output_single")
    @patch("planecli.cache.cached_list_members", new_callable=AsyncMock)
    @patch(
        "planecli.commands.initiatives.resolve_initiative_async",
        new_callable=AsyncMock,
    )
    @patch("planecli.commands.initiatives.get_workspace", return_value="ws")
    @patch("planecli.commands.initiatives.get_client")
    async def test_show_enriches_lead(
        self, mock_client, mock_ws, mock_resolve, mock_members, mock_out
    ):
        from planecli.commands.initiatives import show

        mock_client.return_value = MagicMock()
        mock_resolve.return_value = self._item()
        mock_members.return_value = [
            {
                "id": "user-1",
                "first_name": "Patrick",
                "last_name": "Alves",
                "display_name": "Patrick",
            }
        ]

        await show("Q1 Launch", json=True)

        data = mock_out.call_args[0][0]
        assert data["lead_name"] == "Patrick Alves"

    @patch("planecli.api.raw.api_delete", new_callable=AsyncMock)
    @patch(
        "planecli.commands.initiatives.resolve_initiative_async",
        new_callable=AsyncMock,
    )
    @patch("planecli.cache.invalidate_resource", new_callable=AsyncMock)
    @patch("planecli.commands.initiatives.get_workspace", return_value="ws")
    @patch("planecli.commands.initiatives.get_client")
    async def test_delete_happy_path(
        self, mock_client, mock_ws, mock_inv, mock_resolve, mock_delete
    ):
        from planecli.commands.initiatives import delete

        mock_client.return_value = MagicMock()
        mock_resolve.return_value = self._item()
        mock_delete.return_value = None

        await delete("Q1 Launch")

        url = mock_delete.call_args[0][0]
        assert url.endswith("/workspaces/ws/initiatives/init-1/")
        mock_inv.assert_awaited_once_with("initiatives", "ws")


class TestResolveInitiative:
    @patch("planecli.api.raw.api_get", new_callable=AsyncMock)
    async def test_uuid_fetches_directly(self, mock_get):
        from planecli.utils.resolve import resolve_initiative_async

        uid = "12345678-1234-1234-1234-123456789012"
        mock_get.return_value = {"id": uid, "name": "Q1"}
        result = await resolve_initiative_async(uid, MagicMock(), "ws")
        assert result["id"] == uid
        assert f"/initiatives/{uid}/" in mock_get.call_args[0][0]

    @patch("planecli.cache.cached_list_initiatives", new_callable=AsyncMock)
    async def test_name_fuzzy_matches(self, mock_cached):
        from planecli.utils.resolve import resolve_initiative_async

        mock_cached.return_value = [{"id": "i1", "name": "Q1 Product Launch"}]
        result = await resolve_initiative_async("product launch", MagicMock(), "ws")
        assert result["id"] == "i1"

    @patch("planecli.cache.cached_list_initiatives", new_callable=AsyncMock)
    async def test_name_not_found(self, mock_cached):
        from planecli.exceptions import ResourceNotFoundError
        from planecli.utils.resolve import resolve_initiative_async

        mock_cached.return_value = [{"id": "i1", "name": "Something else"}]
        with pytest.raises(ResourceNotFoundError):
            await resolve_initiative_async("zzz-no-match", MagicMock(), "ws")
