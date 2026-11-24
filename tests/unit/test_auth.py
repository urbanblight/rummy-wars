"""Unit tests for IAP identity checks and the write-restriction hooks in ``auth``."""

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, g
from werkzeug.exceptions import Unauthorized

import auth


@pytest.fixture
def flask_app():
    """A minimal Flask app with the auth hooks and one read and one write route."""
    app = Flask(__name__)
    auth.init_app(app)

    @app.route("/read")
    def read():
        return "ok"

    @app.route("/add", methods=["GET", "POST"])
    def add():
        return "ok"

    @app.route("/write", methods=["POST"])
    def write():
        return "ok"

    return app


def fake_google(verify):
    """Patch the lazily imported google modules so no network or certificates are needed."""
    return patch.dict("sys.modules", {
        "google.auth.transport": MagicMock(),
        "google.auth.transport.requests": MagicMock(),
        "google.oauth2": MagicMock(id_token=MagicMock(verify_token=verify)),
        "google.oauth2.id_token": MagicMock(verify_token=verify),
    })


def test_current_user_requires_token_and_audience(flask_app):
    with flask_app.test_request_context("/read", headers={"X-Goog-IAP-JWT-Assertion": "t"}):
        with patch.dict("os.environ", {}, clear=True), fake_google(MagicMock()), pytest.raises(Unauthorized):
            auth.current_user()
    with flask_app.test_request_context("/read"):
        with patch.dict("os.environ", {"EXPECTED_AUDIENCE": "aud"}), fake_google(MagicMock()), pytest.raises(Unauthorized):
            auth.current_user()


def test_current_user_returns_verified_email_and_rejects_bad_tokens(flask_app):
    headers = {"X-Goog-IAP-JWT-Assertion": "token"}
    with flask_app.test_request_context("/read", headers=headers), patch.dict("os.environ", {"EXPECTED_AUDIENCE": "aud"}):
        verify = MagicMock(return_value={"email": "a@example.com"})
        with fake_google(verify):
            assert auth.current_user() == "a@example.com"
        assert verify.call_args.kwargs["audience"] == "aud"
        with fake_google(MagicMock(side_effect=ValueError("bad"))), pytest.raises(Unauthorized):
            auth.current_user()


def test_is_admin_in_local_dev_and_behind_iap(flask_app):
    with flask_app.test_request_context("/read"), patch.dict("os.environ", {}, clear=True):
        assert auth.is_admin() is True
    with flask_app.test_request_context("/read"), \
            patch.dict("os.environ", {"K_SERVICE": "x", "ADMIN": " Admin@Example.com "}), \
            patch("auth.current_user", return_value="admin@example.com"):
        assert auth.is_admin() is True
        assert g.is_admin is True
    with flask_app.test_request_context("/read"), \
            patch.dict("os.environ", {"K_SERVICE": "x", "ADMIN": "admin@example.com"}), \
            patch("auth.current_user", return_value="other@example.com"):
        assert auth.is_admin() is False
    with flask_app.test_request_context("/read"), patch.dict("os.environ", {"K_SERVICE": "x"}, clear=True):
        assert auth.is_admin() is False


def test_emails_splits_on_commas_and_semicolons():
    with patch.dict("os.environ", {"COMMISH": "A@x.com; b@x.com,, "}):
        assert auth._emails("COMMISH") == {"a@x.com", "b@x.com"}
    with patch.dict("os.environ", {}, clear=True):
        assert auth._emails("COMMISH") == set()


def test_can_edit_team_for_admin_commish_manager_and_strangers(flask_app):
    base = {"K_SERVICE": "x", "ADMIN": "admin@example.com", "COMMISH": "boss@example.com"}

    def check(email, row=None, env=base):
        with flask_app.test_request_context("/read"), patch.dict("os.environ", env), \
                patch("auth.current_user", return_value=email), patch("db.connect") as connect:
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = row
            first = auth.can_edit_team(1)
            return first, auth.can_edit_team(1)  # second call is served from the request cache

    assert check("admin@example.com") == (True, True)
    assert check("Boss@example.com") == (True, True)
    assert check("m@example.com", ("M@example.com",)) == (True, True)
    assert check("x@example.com", ("m@example.com",)) == (False, False)
    assert check("x@example.com", None) == (False, False)
    assert check("x@example.com", (None,)) == (False, False)


def test_write_restrictions(flask_app):
    client = flask_app.test_client()
    with patch.dict("os.environ", {"K_SERVICE": "x", "ADMIN": "admin@example.com"}), \
            patch("auth.current_user", return_value="other@example.com"):
        assert client.get("/read").status_code == 200
        assert client.post("/write").status_code == 403
        assert client.get("/add").status_code == 403
    with patch.dict("os.environ", {"K_SERVICE": "x", "ADMIN": "admin@example.com"}), \
            patch("auth.current_user", return_value="admin@example.com"):
        assert client.post("/write").status_code == 200
        assert client.get("/add").status_code == 200


def test_get_managed_team_id_finds_the_users_team_and_caches_it(flask_app):
    with flask_app.test_request_context("/read"), patch("auth.current_user", return_value=" M@Example.com "), \
            patch("db.connect") as connect:
        execute = connect.return_value.__enter__.return_value.execute
        execute.return_value.fetchone.return_value = (7,)
        assert auth.get_managed_team_id() == 7
        assert auth.get_managed_team_id() == 7
        assert execute.call_count == 1
        assert execute.call_args.args[1] == ("m@example.com",)


def test_get_managed_team_id_is_none_without_a_team_or_identity(flask_app):
    with flask_app.test_request_context("/read"), patch("auth.current_user", return_value="x@example.com"), \
            patch("db.connect") as connect:
        connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
        assert auth.get_managed_team_id() is None
    with flask_app.test_request_context("/read"), patch("auth.current_user", side_effect=Unauthorized()):
        assert auth.get_managed_team_id() is None
