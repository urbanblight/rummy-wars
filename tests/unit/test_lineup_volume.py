import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import import_lineups  # noqa: E402
import zscores  # noqa: E402


def lineup_row(team_id, name, player_type, slot, status="Active", effective_date="2026-09-28", period_id=None, order=1):
    return (team_id, period_id, effective_date, order, player_type, status, slot, name, name.lower(), "X", "TB", None, None, None, None)


def make_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.executescript(import_lineups.SCHEMA)
    conn.executemany(f"INSERT INTO lineups VALUES ({', '.join('?' * 15)})", rows)
    return conn


def test_latest_active_lineups_keeps_the_newest_snapshot_of_each_team_and_only_active_players():
    conn = make_conn([
        lineup_row(1, "Old Guy", "Batter", "C", effective_date="2026-06-01", order=1),
        lineup_row(1, "New Guy", "Batter", "C", effective_date="2026-09-28", order=1),
        lineup_row(1, "Bench Guy", "Batter", "C", status="Reserves", effective_date="2026-09-28", order=2),
        lineup_row(2, "Offseason Guy", "Batter", "1B", effective_date="2026-09-28", order=1),
        lineup_row(2, "Period Guy", "Batter", "1B", effective_date="2026-09-28", period_id=3, order=1),
    ])

    lineups = zscores.latest_active_lineups(conn)

    assert sorted(lineups["norm_name"]) == ["new guy", "period guy"]


def test_latest_active_lineups_is_none_without_the_table_or_rows():
    assert zscores.latest_active_lineups(sqlite3.connect(":memory:")) is None
    assert zscores.latest_active_lineups(make_conn([])) is None


def test_pitcher_slots_are_per_team_averages_of_the_real_lineups():
    rows = [lineup_row(1, f"sp{i}", "Pitcher", "SP", order=10 + i) for i in range(6)]
    rows += [lineup_row(2, f"sp{i}", "Pitcher", "SP", order=10 + i) for i in range(4)]
    rows += [lineup_row(1, "rp", "Pitcher", "RP", order=20), lineup_row(2, "rp", "Pitcher", "RP", order=20)]
    rows += [lineup_row(1, "bat", "Batter", "C", order=30)]
    lineups = zscores.latest_active_lineups(make_conn(rows))

    assert zscores.active_pitcher_slots(lineups) == {"SP": 5.0, "RP": 1.0}


def test_pitcher_slots_fall_back_to_the_settings_without_lineups(monkeypatch):
    monkeypatch.setenv("PITCHING_ACTIVE_SLOTS", "10")
    monkeypatch.setenv("MAX_ACTIVE_SP", "6")
    monkeypatch.setenv("MAX_ACTIVE_RP", "6")
    assert zscores.active_pitcher_slots(None) == {"SP": 5, "RP": 5}


def test_active_lineup_volume_sums_active_players_per_team_and_averages_the_teams():
    stats = pd.DataFrame({
        "team_id": [1, 1, 1, 2, 2, None],
        "norm_name": ["a", "b", "bench", "c", "d", "free"],
        "AB": [500, 400, 300, 200, 100, 600],
    })
    lineups = pd.DataFrame({
        "team_id": [1, 1, 2, 2], "norm_name": ["a", "b", "c", "d"],
        "player_type": ["Batter"] * 4, "slot": ["C", "1B", "C", "1B"],
    })

    assert zscores.active_lineup_volume(stats, lineups, "Batter") == pytest.approx((900 + 300) / 2)
    assert zscores.active_lineup_volume(stats, lineups, "Pitcher") is None


def test_batter_volume_uses_active_lineups_scaled_by_how_much_of_the_season_they_cover():
    conn = make_conn([
        lineup_row(1, "bat", "Batter", "C", order=1),
        lineup_row(1, "arm", "Pitcher", "SP", order=2),
    ])
    conn.execute("CREATE TABLE standings_categories (season_year, as_of_date, category, value)")
    conn.execute("INSERT INTO standings_categories VALUES (2026, '2026-12-31', 'INN', 200.0)")
    pd.DataFrame({"team_id": [1], "norm_name": ["arm"], "INNs": [160.0]}).to_sql("pitching", conn, index=False)
    batting = pd.DataFrame({"team_id": [1, 1], "norm_name": ["bat", "other"], "AB": [500.0, 450.0]})

    # The active pitcher covers 160 of the 200 real innings, so a lineup's AB is scaled by 1.25.
    assert zscores.roster_coverage(conn, zscores.latest_active_lineups(conn)) == pytest.approx(1.25)
    assert zscores.team_volume(conn, batting, "Batter") == pytest.approx(500 * 1.25)
    # Pitchers use the real standings innings directly.
    assert zscores.team_volume(conn, pd.DataFrame({"team_id": [1], "norm_name": ["arm"], "INNs": [160.0]}), "Pitcher") == 200.0


def test_volume_falls_back_to_most_used_players_without_lineups():
    batting = pd.DataFrame({"team_id": [1, 1, 1], "norm_name": ["a", "b", "c"], "AB": [500.0, 400.0, 300.0]})
    assert zscores.team_volume(sqlite3.connect(":memory:"), batting, "Batter") == 1200.0


def test_slot_levels_round_fractional_per_team_slots_to_a_league_rank(monkeypatch):
    teams = pd.DataFrame({"team_id": [1, 2] * 5, "z_total": [float(v) for v in range(10, 0, -1)], "positions": ["RP"] * 10})
    eligible = teams["positions"].map(lambda p: [p])
    candidates = pd.Series(True, index=teams.index)

    # 2 teams * 2.4 slots = rank 5 (value 6.0); the window of 3 centered on it averages ranks 4-6.
    monkeypatch.setattr(zscores, "REPLACEMENT_POOL", 3)
    levels = zscores.slot_replacement_levels(teams, "z_total", eligible, [], {"RP": 2.4}, candidates)
    assert levels["RP"]["z_total"] == pytest.approx(6.0)
