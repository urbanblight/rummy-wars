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
    expect(page.get_by_text("Yainer Diaz")).to_be_visible()
