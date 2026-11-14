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


def init_app(app):
    @app.before_request
    def restrict_writes():
        if request.endpoint in OPEN_ENDPOINTS or request.endpoint in TEAM_ENDPOINTS:
            return None
        if not is_admin() and (request.method not in SAFE_METHODS or request.path == "/add"):
            abort(403)

    @app.context_processor
    def inject_admin():
        return {"is_admin": is_admin(), "can_edit_team": can_edit_team}