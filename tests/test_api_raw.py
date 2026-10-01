"""Tests for the raw HTTP helpers (ADR-0003 escape hatch)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from planecli.api.raw import (
    api_delete,
    api_get,
    api_patch,
    api_post,
    checked,
    get_all_pages,
    initiative_base,
    release_base,
)
from planecli.exceptions import (
    APIError,
    AuthenticationError,
    ResourceNotFoundError,
    ValidationError,
)


def _resp(status_code: int, payload=None, text: str = ""):
    """Build a fake requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = b"{}" if payload is not None else b""
    resp.json.return_value = payload
    return resp


class TestChecked:
    def test_ok_returns_json(self):
        assert checked(_resp(200, {"id": "1"}), action="x") == {"id": "1"}

    def test_created_returns_json(self):
        assert checked(_resp(201, {"id": "1"}), action="x") == {"id": "1"}

    def test_no_content_returns_none(self):
        assert checked(_resp(204), action="x") is None

    def test_404_raises_not_found(self):
        with pytest.raises(ResourceNotFoundError):
            checked(_resp(404, text="{}"), action="fetch", resource="Release", ref="v1")

    def test_401_raises_auth(self):
        with pytest.raises(AuthenticationError):
            checked(_resp(401, text="unauthorized"), action="x")

    def test_402_raises_plan_error(self):
        with pytest.raises(APIError) as exc:
            checked(_resp(402, text="plan"), action="list releases")
        assert "402" in str(exc.value)

    def test_duplicate_name_raises_validation(self):
        body = '{"name": ["RELEASE_NAME_ALREADY_EXISTS"]}'
        with pytest.raises(ValidationError) as exc:
            checked(
                _resp(400, text=body),
                action="create release",
                resource="Release",
                ref="v1",
            )
        assert "already exists" in str(exc.value)

    def test_other_error_raises_api_error(self):
        with pytest.raises(APIError) as exc:
            checked(_resp(500, text="boom"), action="update release")
        assert "boom" in str(exc.value)


class TestBases:
    @patch("planecli.api.raw.api_base", return_value="https://x/api/v1/workspaces/ws")
    def test_release_base_project(self, _mock):
        assert release_base("ws", "p1").endswith("/projects/p1/releases")

    @patch("planecli.api.raw.api_base", return_value="https://x/api/v1/workspaces/ws")
    def test_release_base_workspace(self, _mock):
        assert release_base("ws", None).endswith("/releases")
        assert "projects" not in release_base("ws", None)

    @patch("planecli.api.raw.api_base", return_value="https://x/api/v1/workspaces/ws")
    def test_initiative_base(self, _mock):
        assert initiative_base("ws").endswith("/initiatives")


class TestWrappers:
    @patch("planecli.api.raw.run_sdk", new_callable=AsyncMock)
    async def test_api_get(self, mock_run):
        import requests

        mock_run.return_value = _resp(200, {"id": "1"})
        result = await api_get("https://x/1/", action="fetch", resource="R", ref="1")
        assert result == {"id": "1"}
        assert mock_run.call_args[0][0] is requests.get

    @patch("planecli.api.raw.run_sdk", new_callable=AsyncMock)
    async def test_api_post(self, mock_run):
        import requests

        mock_run.return_value = _resp(201, {"id": "2"})
        result = await api_post(
            "https://x/", {"name": "n"}, action="create", resource="R", ref="n"
        )
        assert result == {"id": "2"}
        assert mock_run.call_args[0][0] is requests.post
        assert mock_run.call_args[1]["json"] == {"name": "n"}

    @patch("planecli.api.raw.run_sdk", new_callable=AsyncMock)
    async def test_api_patch(self, mock_run):
        import requests

        mock_run.return_value = _resp(200, {"id": "3"})
        result = await api_patch(
            "https://x/3/", {"name": "m"}, action="update", resource="R", ref="m"
        )
        assert result == {"id": "3"}
        assert mock_run.call_args[0][0] is requests.patch

    @patch("planecli.api.raw.run_sdk", new_callable=AsyncMock)
    async def test_api_delete(self, mock_run):
        import requests

        mock_run.return_value = _resp(204)
        assert await api_delete("https://x/3/", action="delete", resource="R") is None
        assert mock_run.call_args[0][0] is requests.delete

    @patch("planecli.api.raw.run_sdk", new_callable=AsyncMock)
    async def test_get_all_pages(self, mock_run):
        mock_run.side_effect = [
            _resp(
                200,
                {
                    "results": [{"id": "1"}],
                    "next_page_results": True,
                    "next_cursor": "20:1:0",
                },
            ),
            _resp(200, {"results": [{"id": "2"}], "next_page_results": False}),
        ]
        result = await get_all_pages("https://x/", action="list")
        assert [r["id"] for r in result] == ["1", "2"]
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1][1]["params"]["cursor"] == "20:1:0"
