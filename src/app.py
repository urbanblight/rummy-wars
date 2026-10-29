from __future__ import annotations

import datetime
import logging
import os
import tempfile
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from xml.etree import ElementTree

import requests
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

try:
    from . import league, projections, utils, zscores
except ImportError:  # pragma: no cover - supports direct module imports
    import league
    import projections
    import utils
    import zscores

app = Flask(__name__)


@app.template_filter("stat_value")
def format_stat_value(value: float | None, key: str) -> str:
    """Format a scoring stat for display, e.g. BA as ".275", ERA/WHIP/Kd9 to 2 decimals, INN to 1."""
    if value is None:
        return "-"
    if key in ("BA", "OBP", "SLG"):
        return f"{value:.3f}".removeprefix("0")
    if key in ("ERA", "WHIP", "Kd9"):
        return f"{value:.2f}"
    if key == "INN":
        return f"{value:.1f}"
    return str(int(value)) if float(value).is_integer() else str(value)


@app.template_filter("signed")
def format_signed(value: float) -> str:
    """Two decimals with an explicit sign, using a true minus."""
    return f"{value:+.2f}".replace("-", "\u2212")


load_dotenv(dotenv_path='.env', override=False)
load_dotenv(dotenv_path='.env.secrets', override=False)
app.json.sort_keys = False  # keep scoring categories in league order
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "rummy-wars-key")
MODE = os.environ.get("MODE", "inseason")

LOGGER = logging.getLogger(__name__)


@app.context_processor
def inject_current_year():
    return {"current_year": datetime.datetime.now(tz=datetime.timezone.utc).date().year}


def notices_by_player(violations: list[str], warnings: list[str]) -> dict[str, list[dict]]:
    """Group the violations and warnings that are about a player by that player's name.

    Messages that are not about one player (such as slot limits) are left out.
    """
    grouped: dict[str, list[dict]] = {}
    for kind, messages in (("violation", violations), ("warning", warnings)):
        for message in messages:
            name = getattr(message, "player", None)
            if name:
                grouped.setdefault(name, []).append({"kind": kind, "message": str(message)})
    return grouped


def evaluate_upload(file_storage):
    """Parse and evaluate an uploaded CBS roster without writing it to disk."""
    csv_bytes = file_storage.stream.read()
    with tempfile.NamedTemporaryFile(suffix=".csv") as temporary_file:
        temporary_file.write(csv_bytes)
        temporary_file.flush()
        roster = utils.parse_cbs_roster_csv(temporary_file.name)
        violations, warnings = utils.validate_league_rules(roster)
    zscores.evaluate_roster(roster)
    return roster, violations, warnings


NEWS_RSS_URL = "https://news.google.com/rss/search"
NEWS_LIMIT = 10


def _published_at(value: str) -> datetime.datetime:
    """Parse an RSS pubDate; unparseable dates sort last."""
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def fetch_player_news(name: str) -> list[dict]:
    """Return recent Google News items for a player."""
    query = f'"{name}" MLB -"player prop" when:7d'
    url = f"{NEWS_RSS_URL}?{urlencode({'q': query})}&hl=en-US&gl=US&ceid=US:en"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    items = []
    for item in ElementTree.fromstring(response.content).iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link.startswith(("http://", "https://")):
            continue
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": link,
            "source": (item.findtext("source") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
        })
    items.sort(key=lambda entry: _published_at(entry["published"]), reverse=True)
    return items[:NEWS_LIMIT]


@app.get("/news")
def player_news():
    name = request.args.get("name", "").strip()
    if not name or len(name) > 100:
        abort(400)
    try:
        return jsonify(fetch_player_news(name))
    except (requests.RequestException, ElementTree.ParseError):
        LOGGER.exception("News lookup failed for %s", name)
        return jsonify({"error": "News is unavailable"}), 502


@app.get("/")
def index():
    offseason = MODE == "offseason"
    return render_template("index.html", offseason=offseason)


@app.get("/teams")
def teams():
    snapshot, league_teams = league.league_teams()
    return render_template("teams.html", snapshot=snapshot, teams=league_teams)


@app.get("/teams/<int:team_id>")
def team(team_id: int):
    found = league.get_team(team_id)
    if found is None:
        abort(404)
    snapshot, _teams = league.league_teams()
    lineup, players = zscores.load_team_roster(team_id)
    sources = projections.systems()
    source = next((s for s in sources if s["key"] == request.args.get("stats")), None)
    if source:
        for player_type, group in players.items():
            projections.apply_stats(group, player_type, source["system"], source["season"])
    return render_template(
        "team.html", team=found, snapshot=snapshot, lineup=lineup,
        batters=players["Batter"], pitchers=players["Pitcher"], offseason=MODE == "offseason",
        stat_sources=sources, stat_source=source["key"] if source else "",
    )


@app.post("/teams/<int:team_id>/evaluate")
def evaluate_team(team_id: int):
    """Evaluate a team's most recent imported lineup like an uploaded roster."""
    found = league.get_team(team_id)
    if found is None:
        abort(404)
    loaded = zscores.load_team_lineup(team_id)
    if loaded is None:
        flash("No lineup has been imported for this team yet.", "error")
        return redirect(url_for("team", team_id=team_id))
    lineup, roster = loaded

    try:
        violations, warnings = utils.validate_league_rules(roster)
    except Exception:
        LOGGER.exception("Lineup evaluation failed for team %s", team_id)
        flash("The lineup could not be evaluated. Check the server log for details.", "error")
        return redirect(url_for("team", team_id=team_id))

    period = "offseason" if lineup["period_id"] is None else f"period {lineup['period_id']}"
    return render_template(
        "results.html",
        filename=f"{found['name']} lineup as of {lineup['effective_date']} ({period})",
        roster=roster,
        violations=violations,
        warnings=warnings,
        notices=notices_by_player(violations, warnings),
        back_url=url_for("team", team_id=team_id),
        back_label="Back to team",
    )


@app.post("/evaluate")
def evaluate():
    upload = request.files.get("roster")
    if upload is None or not upload.filename:
        flash("Choose a CBS roster CSV to evaluate.", "error")
        return redirect(url_for("index"))

    if not upload.filename.lower().endswith(".csv"):
        flash("Roster exports must be CSV files.", "error")
        return redirect(url_for("index"))

    try:
        roster, violations, warnings = evaluate_upload(upload)
    except (UnicodeDecodeError, OSError, ValueError) as error:
        LOGGER.warning("Unable to process roster upload: %s", error)
        flash("That file could not be read as a CBS roster CSV.", "error")
        return redirect(url_for("index"))
    except Exception:
        LOGGER.exception("Roster evaluation failed")
        flash("The roster could not be evaluated. Check the server log for details.", "error")
        return redirect(url_for("index"))

    return render_template(
        "results.html",
        filename=upload.filename,
        roster=roster,
        violations=violations,
        warnings=warnings,
        notices=notices_by_player(violations, warnings),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or "8080") # Default to Docker default
    app.run(host="127.0.0.1", port=port)
