"""IAP identity: verifies the signed header and restricts writes to the ADMIN user
(lineup uploads also to COMMISH users and team managers).

Cloud Run serves the app behind Identity-Aware Proxy, which adds a signed
``X-Goog-IAP-JWT-Assertion`` header. Only the user whose email equals ``ADMIN`` may
change data; everyone else gets a read-only app. Outside Cloud Run (no ``K_SERVICE``)
the local developer is treated as admin.
"""

import os

from dotenv import load_dotenv
from flask import abort, g, request

load_dotenv()

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# Authenticated by its own check (the Cloud Scheduler invoker) in routes/backup.py.
OPEN_ENDPOINTS = ("backup.run_backup",)
# Writable by team managers and commissioners too; the view checks ``can_edit_team``.
TEAM_ENDPOINTS = ("upload_lineup",)


def local_dev():
    """True when not running on Cloud Run, where there is no IAP and the developer is trusted."""
    return not os.environ.get("K_SERVICE")


def current_user():
    """Return the verified email of the IAP user, aborting with 401 if it can't be verified."""
    from google.auth.transport import requests as grequests
    from google.oauth2 import id_token

    token = request.headers.get("X-Goog-IAP-JWT-Assertion")
    audience = os.environ.get("EXPECTED_AUDIENCE")
    if not token or not audience:
        abort(401)
    try:
        claims = id_token.verify_token(
            token, grequests.Request(), audience=audience, certs_url=IAP_CERTS_URL
        )
    except ValueError:
        abort(401)
    return claims["email"]


def is_admin():
    """True if the requesting user is the ADMIN (cached per request)."""
    if "is_admin" not in g:
        if local_dev():
            g.is_admin = True
        else:
            admin = (os.environ.get("ADMIN") or "").strip().lower()
            g.is_admin = bool(admin) and current_user().strip().lower() == admin
    return g.is_admin


def _emails(name):
    """Lower-cased addresses in a comma- or semicolon-separated environment variable."""
    raw = (os.environ.get(name) or "").replace(";", ",")
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


def can_edit_team(team_id):
    """True for the ADMIN, a COMMISH, or the manager of the team (cached per request)."""
    if is_admin():
        return True
    key = f"can_edit_team_{team_id}"
    if key not in g:
        try:
            from . import db
        except ImportError:  # pragma: no cover - supports direct module imports
            import db

        email = current_user().strip().lower()
        allowed = email in _emails("COMMISH")
        if not allowed:
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT m.email FROM teams t JOIN managers m ON m.id = t.manager_id WHERE t.id = %s",
                    (team_id,),
                ).fetchone()
            allowed = bool(row and row[0] and row[0].strip().lower() == email)
        setattr(g, key, allowed)
    return getattr(g, key)


def get_managed_team_id():
    """The team id that the current user manages, or None if they don't manage a team (cached per request)."""
    if "managed_team_id" not in g:
        try:
            from . import db
        except ImportError:  # pragma: no cover - supports direct module imports
            import db

        team_id = None
        try:
            email = current_user().strip().lower()
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT t.id FROM teams t JOIN managers m ON m.id = t.manager_id WHERE m.email = %s",
                    (email,),
                ).fetchone()
            team_id = row[0] if row else None
        except Exception:
            pass
        g.managed_team_id = team_id
    return g.managed_team_id


def init_app(app):
    """Register the write-restriction hook and template context on a Flask app.

    Args:
        app: The Flask application to protect.
    """
    @app.before_request
    def restrict_writes():
        """Reject unsafe requests (and ``/add``) from non-admins, except open and team endpoints."""
        if request.endpoint in OPEN_ENDPOINTS or request.endpoint in TEAM_ENDPOINTS:
            return None
        if not is_admin() and (
            (request.method not in SAFE_METHODS and request.endpoint not in ("evaluate", "evaluate_team"))
            or request.path == "/add"
        ):
            abort(403)

    @app.context_processor
    def inject_admin():
        """Expose ``is_admin`` and ``can_edit_team`` to templates so they can hide write controls."""
        return {"is_admin": is_admin(), "can_edit_team": can_edit_team}