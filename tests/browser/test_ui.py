"""Browser-level tests for the roster upload experience.

These tests execute the page JavaScript in Chromium. The Flask application is
served on an ephemeral local port, while MLB rule evaluation is stubbed so the
browser tests remain deterministic and do not make external requests.
"""

import threading
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright
from werkzeug.serving import make_server

import app as app_module

PROJECT_ROOT = Path(__file__).parents[2]
SAMPLE_CSV = PROJECT_ROOT / "tests/fixtures/cbs-roster-export.csv"


@pytest.fixture
def page():
    """Create and close one isolated Chromium page for a browser test."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        browser_page = browser.new_page()
        yield browser_page
        browser.close()


@pytest.fixture
def live_server(monkeypatch):
    """Serve the Flask app on a local ephemeral port for one browser test."""
    monkeypatch.setattr(
        app_module.utils,
        "validate_league_rules",
        lambda roster, max_minors=20, max_il=8: ([], []),
    )

    server = make_server("127.0.0.1", 0, app_module.app)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()

    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server_thread.join()


def test_loading_screen_starts_hidden(live_server, page: Page):
    """The loading overlay should not appear before a roster is submitted."""
    page.goto(live_server)

    expect(page.locator("#loading-screen")).to_be_hidden()
    expect(page.locator("#roster")).to_have_attribute("required", "")
    expect(page.get_by_role("button", name="Evaluate roster")).to_be_enabled()


def test_file_selection_updates_filename(live_server, page: Page):
    """Selecting a CSV should show its filename beside the upload control."""
    page.goto(live_server)
    page.set_input_files("#roster", str(SAMPLE_CSV))

    expect(page.locator("#file-name")).to_have_text(SAMPLE_CSV.name)


def test_submit_shows_loading_screen(live_server, page: Page):
    """Submitting a CSV should show loading feedback immediately."""
    page.goto(live_server)
    page.evaluate(
        "document.querySelector('form').addEventListener('submit', "
        "event => event.preventDefault())"
    )
    page.set_input_files("#roster", str(SAMPLE_CSV))
    page.get_by_role("button", name="Evaluate roster").click()

    expect(page.locator("#loading-screen")).to_be_visible()
    expect(page.get_by_role("button", name="Evaluate roster")).to_be_disabled()


def test_submit_renders_results(live_server, page: Page):
    """A successful browser submission should render the roster results."""
    page.goto(live_server)
    page.set_input_files("#roster", str(SAMPLE_CSV))
    page.get_by_role("button", name="Evaluate roster").click()

    expect(page.get_by_role("heading", name="Roster report")).to_be_visible()
    expect(page.get_by_role("heading", name="Full roster", level=2)).to_be_visible()
    expect(page.get_by_role("heading", name="Batters", level=3)).to_be_visible()
    expect(page.get_by_role("heading", name="Pitchers", level=3)).to_be_visible()
    expect(page.locator(".roster-section")).to_have_attribute(
        "aria-labelledby", "roster-title"
    )
    expect(page.get_by_text("Yainer Diaz")).to_be_visible()
    expect(page.locator("details.roster-accordion")).to_have_count(6)
    reserves = page.locator(
        'details[data-player-type="Batter"][data-status="Reserves"]'
    )
    expect(reserves.locator("summary")).to_have_text("Reserve batters (4)")
    expect(reserves.locator("tbody tr").first).to_be_hidden()
    reserves.locator("summary").click()
    expect(reserves.locator("tbody tr").first).to_be_visible()
    expect(page.locator("th").filter(has_text="Status")).to_have_count(0)

def test_team_status_accordions_expand_on_click(live_server, page: Page, monkeypatch):
    """Non-active roster groups start collapsed and expand when their summary is clicked."""
    import models

    team = {"id": 7, "name": "The Outfield", "abbrev": "YL", "manager": "Matt B", "rank": 1, "total_points": 150.5}
    snapshot = {"season_year": 2026, "as_of_date": "2026-12-31"}
    players = {
        "Batter": [
            models.Player(name="Active Batter", roster_slot="C", eligible_positions=["C"], team="HOU", status="Active", player_type="Batter"),
            models.Player(name="Reserve Batter", roster_slot="Reserves", eligible_positions=["1B"], team="HOU", status="Reserves", player_type="Batter"),
            models.Player(name="Injured Batter", roster_slot="IL", eligible_positions=["OF"], team="HOU", status="Injured", player_type="Batter"),
            models.Player(name="Minor Leaguer", roster_slot="Minors", eligible_positions=["SS"], team="HOU", status="Minors", player_type="Batter"),
        ],
        "Pitcher": [
            models.Player(name="Active Pitcher", roster_slot="P", eligible_positions=["P"], team="HOU", status="Active", player_type="Pitcher"),
            models.Player(name="Reserve Pitcher", roster_slot="Reserves", eligible_positions=["P"], team="HOU", status="Reserves", player_type="Pitcher"),
            models.Player(name="Injured Pitcher", roster_slot="IL", eligible_positions=["P"], team="HOU", status="Injured", player_type="Pitcher"),
            models.Player(name="Minor League Pitcher", roster_slot="Minors", eligible_positions=["P"], team="HOU", status="Minors", player_type="Pitcher"),
        ],
    }
    monkeypatch.setattr(app_module.league, "get_team", lambda _team_id: team)
    monkeypatch.setattr(app_module.league, "league_teams", lambda: (snapshot, [team]))
    monkeypatch.setattr(
        app_module.zscores,
        "load_team_roster",
        lambda _team_id: ({"effective_date": "2026-09-28", "period_id": 12}, players),
    )
    page.goto(f"{live_server}/teams/7")

    expect(page.get_by_text("Active Batter")).to_be_visible()
    expect(page.get_by_text("Active Pitcher")).to_be_visible()
    batter_section = page.locator('section[aria-labelledby="batters-title"]')
    pitcher_section = page.locator('section[aria-labelledby="pitchers-title"]')
    expect(batter_section.locator("details")).to_have_count(3)
    expect(pitcher_section.locator("details")).to_have_count(3)
    for player_type, status, title, player in (
        ("Batter", "Reserves", "Reserve batters", "Reserve Batter"),
        ("Batter", "Injured", "Injured batters", "Injured Batter"),
        ("Batter", "Minors", "Minor league batters", "Minor Leaguer"),
        ("Pitcher", "Reserves", "Reserve pitchers", "Reserve Pitcher"),
        ("Pitcher", "Injured", "Injured pitchers", "Injured Pitcher"),
        ("Pitcher", "Minors", "Minor league pitchers", "Minor League Pitcher"),
    ):
        accordion = page.locator(
            f'details[data-player-type="{player_type}"][data-status="{status}"]'
        )
        expect(accordion.locator("summary")).to_have_text(f"{title} (1)")
        expect(accordion).not_to_have_attribute("open", "")
        expect(accordion.locator("tbody").get_by_text(player, exact=True)).to_be_hidden()
        accordion.locator("summary").click()
        expect(accordion.locator("tbody").get_by_text(player, exact=True)).to_be_visible()


def test_notification_icon_opens_the_players_messages(live_server, page: Page, monkeypatch):
    """A player named in a violation or warning gets an icon that shows the messages when clicked."""
    import models

    monkeypatch.setattr(
        app_module.utils,
        "validate_league_rules",
        lambda roster, max_minors=20, max_il=8: (
            [models.PlayerNotice("Yainer Diaz is placed in an Injured slot and was activated 2026-06-01", "Yainer Diaz")],
            [
                models.PlayerNotice("Yainer Diaz may also need a second look", "Yainer Diaz"),
                "Exceeded something that is not about one player",
            ],
        ),
    )
    page.goto(live_server)
    page.set_input_files("#roster", str(SAMPLE_CSV))
    page.get_by_role("button", name="Evaluate roster").click()

    expect(page.locator(".note-icon")).to_have_count(0)
    page.get_by_role("button", name="Details for Yainer Diaz").click()
    icon = page.locator("#player-modal .note-icon")
    expect(icon).to_have_text("🚨⚠️ Notifications")
    expect(page.locator("#note-modal")).to_be_hidden()

    icon.click()

    expect(page.locator("#note-modal")).to_be_visible()
    expect(page.locator("#note-modal-title")).to_have_text("Yainer Diaz")
    expect(page.locator("#note-modal-list .violation")).to_have_text(
        "Yainer Diaz is placed in an Injured slot and was activated 2026-06-01"
    )
    expect(page.locator("#note-modal-list .warning")).to_have_text("Yainer Diaz may also need a second look")

    page.locator(".note-close").click()
    expect(page.locator("#note-modal")).to_be_hidden()
