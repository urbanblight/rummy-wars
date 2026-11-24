"""Integration tests for the news error path and the lineup upload error branches."""

import io
from unittest.mock import MagicMock, patch

import requests

import app as app_module
from app import app


def post_lineup(client, data, team_id=3):
    return client.post(
        f"/teams/{team_id}/lineup", data=data, content_type="multipart/form-data", follow_redirects=False,
    )


def flashed(client):
    with client.session_transaction() as session:
        return session.get("_flashes", [])


def test_news_failure_returns_502():
    with patch("app.requests.get", side_effect=requests.ConnectionError("down")):
        response = app.test_client().get("/news?name=Someone")
    assert response.status_code == 502
    assert response.get_json() == {"error": "News is unavailable"}


def test_import_lineups_module_is_loaded_from_scripts():
    module = app_module._import_lineups_module()
    assert hasattr(module, "parse_lineup")


def test_import_lineups_module_extends_sys_path_when_needed():
    import sys

    with patch.object(sys, "path", [entry for entry in sys.path if not entry.endswith(("scripts", "src"))]) as path:
        app_module._import_lineups_module()
        assert any(entry.endswith("scripts") for entry in path)


def test_upload_to_unknown_team_is_404():
    with patch("app.league.get_team", return_value=None):
        assert post_lineup(app.test_client(), {}).status_code == 404


def test_upload_without_permission_is_403():
    with patch("app.league.get_team", return_value={"id": 3}), patch("app.auth.can_edit_team", return_value=False):
        assert post_lineup(app.test_client(), {}).status_code == 403


def test_upload_without_a_file_redirects_with_a_message():
    client = app.test_client()
    with patch("app.league.get_team", return_value={"id": 3}), patch("app.auth.can_edit_team", return_value=True):
        assert post_lineup(client, {}).status_code == 302
        assert post_lineup(client, {"lineup": (io.BytesIO(b""), "")}).status_code == 302
    assert flashed(client)[0] == ("error", "Choose a CBS roster CSV to upload.")


def test_upload_with_unreadable_file_or_bad_name_flashes_the_reason():
    client = app.test_client()
    with patch("app.league.get_team", return_value={"id": 3}), patch("app.auth.can_edit_team", return_value=True):
        response = post_lineup(client, {"lineup": (io.BytesIO(b"not a roster"), "roster-overview-20250914.csv")})
    assert response.status_code == 302
    assert flashed(client)[0][1].startswith("Could not read that lineup:")


def test_upload_database_failure_flashes_a_generic_message():
    client = app.test_client()
    importer = MagicMock()
    importer.parse_lineup.return_value = []
    with patch("app.league.get_team", return_value={"id": 3}), patch("app.auth.can_edit_team", return_value=True), \
            patch("app._import_lineups_module", return_value=importer), \
            patch("app.db.connect", side_effect=RuntimeError("boom")):
        response = post_lineup(client, {"lineup": (io.BytesIO(b"x"), "roster-overview-20250914.csv")})
    assert response.status_code == 302
    assert flashed(client)[0] == ("error", "The lineup could not be saved. Check the server log for details.")


def test_index_shows_the_managed_teams_roster():
    with patch("app.auth.get_managed_team_id", return_value=3), \
            patch("app.league.get_team", return_value={"id": 3, "name": "Test Team"}), \
            patch("app.zscores.load_team_roster", return_value=(None, {"Batter": [], "Pitcher": []})) as load:
        response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"Test Team" in response.data
    load.assert_called_once_with(3)
