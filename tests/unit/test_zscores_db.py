"""Database-backed tests for ``zscores``: scoring pipeline, roster evaluation, free agents and id caches."""

import importlib
import time

import pandas as pd
import pytest

import db
import zscores


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    """Every cache starts empty and scoring categories are fixed, whatever ``.env`` says."""
    for name in ("_CBS_IDS", "_BP_RANKS", "_PROSPECTS", "_EXTERNAL_IDS"):
        monkeypatch.setattr(zscores, name, None)
    for prefix in ("BATTING", "PITCHING"):
        monkeypatch.delenv(f"{prefix}_SCORING_STATS", raising=False)
        monkeypatch.delenv(f"{prefix}_SCORING_CATEGORIES", raising=False)


def _no_database(monkeypatch):
    monkeypatch.setattr(db, "connect_or_none", lambda: None)


def _batting_frame():
    return pd.DataFrame({
        "name": ["Ann Aaron", "Bob Bell", "Cy Cole", "Dan Dunn", "Eli Ellis"],
        "player_id": ["p1", "p2", "p3", "p4", "p5"],
        "positions": ["C", "1B", "C", "C", "1B,DH"],
        "team": ["NYY", "BOS", "NYY", "BOS", "NYY"],
        "team_id": [1.0, 2.0, None, None, None],
        "AB": [500, 450, 400, 0, 300],
        "HR": [30, 20, 25, 0, 10],
        "rank": [1, 2, 3, 9999, 4],
    })


def _pitching_frame():
    return pd.DataFrame({
        "name": ["Pat Pitch", "Rob Reliever", "Sam Starter", "Tom Tired"],
        "player_id": ["q1", "q2", "q3", "q4"],
        "positions": ["SP", "RP", "SP", "RP"],
        "team": ["NYY", "BOS", "NYY", "BOS"],
        "team_id": [1.0, 2.0, None, None],
        "W": [10, 3, 8, 0], "QS": [15, 0, 12, 0], "K": [200, 80, 150, 0],
        "ERA": [3.0, 2.5, 4.0, 0.0], "INNs": [180.0, 70.0, 150.0, 0.0],
        "GS": [30, 0, 28, 0], "APP": [30, 60, 28, 0],
        "rank": [1, 2, 3, 9999],
    })


def _write(conn, df, table):
    conn.write_frame(df, table)
    conn.commit()


@pytest.fixture
def league(conn, monkeypatch):
    """A scored batting and pitching table, SGP factors and a players table in the test database."""
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    monkeypatch.setenv("PITCHING_SCORING_CATEGORIES", "W+QS,Kd9,ERA,INN")
    _write(conn, _batting_frame(), "batting")
    _write(conn, _pitching_frame(), "pitching")
    _write(conn, pd.DataFrame({
        "category": ["HR", "W+QS", "Kd9", "ERA", "INN"], "factor": [5.0, 5.0, 0.5, 0.1, 20.0],
    }), "sgp_factors")
    _write(conn, pd.DataFrame({
        "player_id": ["p1", "p2", "q1"], "name": ["Ann Aaron", "Bob Bell", "Pat Pitch"],
        "cbs_name": ["Aaron, Ann", "Bell, Bob", "Pitch, Pat"], "team": ["NYY", "BOS", "NYY"],
        "pos": ["C", "1B", "P"], "active": [1, 1, 1], "cbs_id": ["100", "", None],
        "fangraphs_id": ["fg1", None, None], "mlb_id": ["m1", None, None], "bp_id": [None, None, "bp3"],
    }), "players")
    zscores.save_z_scores()
    return conn


def test_module_imports_as_a_package_member():
    # Running as ``src.zscores`` takes the relative-import branch instead of the script fallback.
    module = importlib.import_module("src.zscores")
    assert module.PlayerIndex.__name__ == "PlayerIndex"


def test_add_category_derives_combined_and_per_nine_stats():
    df = pd.DataFrame({"W": [1, 2], "QS": [3, 4], "K": [18, 9], "INNs": [18.0, 0.0]})
    assert zscores._add_category(df, "W+QS", {}).tolist() == [4, 6]
    kd9 = zscores._add_category(df, "Kd9", {})
    assert kd9[0] == 9.0 and pd.isna(kd9[1])


def test_slug_and_columns_are_identifiers():
    assert zscores._slug("W+QS") == "W_QS"


def test_active_lineup_volume_is_none_without_matching_players():
    lineups = pd.DataFrame({"team_id": [1], "player_id": ["zz"], "player_type": ["Batter"], "slot": ["C"]})
    df = pd.DataFrame({"team_id": [1], "player_id": ["p1"], "AB": [500]})
    assert zscores.active_lineup_volume(df, lineups, "Batter") is None
    assert zscores.active_lineup_volume(df, lineups, "Pitcher") is None
    lineups["player_id"] = ["p1"]
    assert zscores.active_lineup_volume(df, lineups, "Batter") == 500.0


def test_roster_coverage_defaults_to_one_without_the_tables(conn):
    lineups = pd.DataFrame({"team_id": [1], "player_id": ["q1"], "player_type": ["Pitcher"], "slot": ["SP"]})
    assert zscores.roster_coverage(conn, lineups) == 1.0


def test_roster_coverage_scales_active_innings_to_the_season_total(conn):
    conn.execute("CREATE TABLE standings_categories (season_year INT, as_of_date VARCHAR(10), category VARCHAR(64), value DOUBLE)")
    conn.execute("INSERT INTO standings_categories VALUES (2026, '2026-12-31', 'INN', 1000.0)")
    _write(conn, pd.DataFrame({"team_id": [1], "player_id": ["q1"], "INNs": [500.0]}), "pitching")
    lineups = pd.DataFrame({"team_id": [1], "player_id": ["q1"], "player_type": ["Pitcher"], "slot": ["SP"]})
    assert zscores.roster_coverage(conn, lineups) == pytest.approx(2.0)


def test_latest_active_lineups_and_slot_counts_and_team_volume(conn):
    assert zscores.latest_active_lineups(conn) is None
    _create_lineups(conn)
    conn.executemany(
        "INSERT INTO lineups (team_id, period_id, effective_date, slot_order, player_type, status, slot, player_id,"
        " name, positions, mlb_team) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'N', 'C', 'NYY')",
        [
            (1, 1, "2026-04-01", 1, "Batter", "Active", "C", "p1"),   # superseded by the later lineup
            (1, 2, "2026-04-02", 1, "Batter", "Active", "C", "p1"),
            (1, 2, "2026-04-02", 2, "Pitcher", "Active", "SP", "q1"),
            (1, 2, "2026-04-02", 3, "Pitcher", "Reserves", "BN", "q3"),
        ],
    )
    conn.commit()
    lineups = zscores.latest_active_lineups(conn)
    assert sorted(lineups["slot"]) == ["C", "SP"]
    assert zscores.lineup_slot_counts(lineups, "Pitcher") == {"SP": 1.0}
    assert zscores.lineup_slot_counts(None, "Pitcher") == {}
    assert zscores.active_pitcher_slots(lineups) == zscores.active_pitcher_slots()

    # Batter volume comes from the active lineup (500 AB) scaled by coverage (1.0 without standings).
    _write(conn, _batting_frame(), "batting")
    df = conn.read_sql("SELECT * FROM batting")
    assert zscores.team_volume(conn, df, "Batter") == pytest.approx(500.0)


def test_active_pitcher_slots_use_real_lineups_when_both_roles_exist():
    lineups = pd.DataFrame({
        "team_id": [1, 1, 1], "player_id": ["a", "b", "c"], "player_type": ["Pitcher"] * 3, "slot": ["SP", "SP", "RP"],
    })
    assert zscores.active_pitcher_slots(lineups) == {"SP": 2.0, "RP": 1.0}


def _create_lineups(conn, with_captured_at=True):
    conn.execute(
        "CREATE TABLE lineups (team_id INT, period_id INT, effective_date VARCHAR(10), slot_order INT,"
        " player_type VARCHAR(16), status VARCHAR(16), slot VARCHAR(16), player_id VARCHAR(32), name VARCHAR(255),"
        " positions VARCHAR(64), mlb_team VARCHAR(16)" + (", captured_at VARCHAR(32)" if with_captured_at else "") + ")"
    )


def test_pitcher_roles_without_usage_columns_use_cbs_eligibility():
    df = pd.DataFrame({"positions": ["SP,RP", "DH"]})
    assert zscores.pitcher_roles(df).tolist() == [["SP", "RP"], []]


def test_score_frame_is_unchanged_without_scoring_categories(conn):
    df = _batting_frame()
    assert zscores.score_frame(conn, df, "Batter", {}) is df


def test_score_frame_adds_every_score_family(league, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    df = zscores.score_frame(league, _batting_frame(), "Batter", {"HR": 5.0})
    for column in ("z_HR", "z_total_rank", "sgp_HR", "sgp_total", "z_total_above_replacement", "sgp_total_above_replacement_rank"):
        assert column in df
    # Stale score columns from an earlier run are replaced rather than duplicated.
    again = zscores.score_frame(league, df, "Batter", {"HR": 5.0})
    assert list(again.columns) == list(df.columns)


def test_save_z_scores_writes_scores_for_configured_player_types_only(conn, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    _write(conn, _batting_frame(), "batting")
    _write(conn, _pitching_frame(), "pitching")
    zscores.save_z_scores()
    assert "z_HR" in db.column_names(conn, "batting")
    assert "z_total" not in db.column_names(conn, "pitching")
    assert zscores.load_sgp_factors(conn) == {}  # no sgp_factors table: tolerated


def test_save_z_scores_scores_pitchers_too(league):
    assert "sgp_total_above_replacement" in db.column_names(league, "pitching")
    assert zscores.load_sgp_factors(league)["HR"] == 5.0


def test_score_context_rules(monkeypatch):
    assert zscores._score_context([], "Batter") is None
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    empty = zscores._score_context([], "Batter")
    assert empty.has_sgp is False  # nothing stored, so SGP cannot be assumed
    assert empty.has_z_repl is False

    mismatched = [db.Row((1,), ["z_other"])]
    assert zscores._score_context(mismatched, "Batter") is None


def test_int_or_none():
    assert zscores._int_or_none(None) is None
    assert zscores._int_or_none(float("nan")) is None
    assert zscores._int_or_none(3.0) == 3


def test_scoring_stat_handles_missing_values():
    row = db.Row((None, 5, None, 90), ["W", "QS", "INNs", "K"])
    assert zscores.scoring_stat(row, "W+QS", {}) is None
    assert zscores.scoring_stat(row, "Kd9", {}) is None
    assert zscores.scoring_stat(db.Row((3.0, 9), ["INNs", "K"]), "Kd9", {}) == 27.0


def test_attach_scores_without_a_z_score_leaves_ranks_empty():
    from models import Player

    row = db.Row((5, None), ["rank", "z_total"])
    context = zscores.ScoreContext(["HR"], {}, False, False, False)
    player = Player("X", "", [], "NYY", "", "Batter")
    zscores.attach_scores(player, row, context, stats=False)
    assert player.cbs_rank == 5 and player.z_total is None and player.scoring_stats == {}


def test_evaluate_roster_attaches_scores_and_resolves_names(league):
    from models import Player, TeamRoster

    by_id = Player("Ann Aaron", "C", ["C"], "NYY", "Active", "Batter", player_id="p1")
    by_name = Player("Bob Bell", "1B", ["1B"], "BOS", "Active", "Batter")
    unknown = Player("Nobody", "1B", ["1B"], "BOS", "Active", "Batter")
    pitcher = Player("Pat Pitch", "SP", ["SP"], "NYY", "Active", "Pitcher", player_id="q1")
    roster = TeamRoster(players=[by_id, by_name, unknown, pitcher])
    zscores.evaluate_roster(roster)

    assert by_id.z_total is not None and by_id.sgp_total is not None and by_id.cbs_rank == 1
    assert by_id.scoring_stats == {"HR": 30}
    assert by_name.player_id == "p2" and by_name.z_total is not None
    assert unknown.player_id is None and unknown.z_total is None
    assert set(pitcher.z_scores) == {"W+QS", "Kd9", "ERA", "INN"}


def test_evaluate_roster_skips_unusable_tables(conn, monkeypatch):
    from models import Player, TeamRoster

    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    batter = Player("Ann Aaron", "C", ["C"], "NYY", "Active", "Batter")
    pitcher = Player("Pat Pitch", "SP", ["SP"], "NYY", "Active", "Pitcher", player_id="q1")  # no categories set
    roster = TeamRoster(players=[batter, pitcher])

    zscores.evaluate_roster(roster)  # no batting table at all
    assert batter.z_total is None

    # Stats imported before player ids existed.
    _write(conn, pd.DataFrame({"name": ["Ann Aaron"], "HR": [1], "rank": [1], "z_HR": [0.0], "z_total": [0.0]}), "batting")
    zscores.evaluate_roster(roster)
    assert batter.z_total is None

    # Stats imported with different scoring categories.
    _write(conn, pd.DataFrame({"player_id": ["p1"], "team": ["NYY"], "rank": [1], "z_SB": [0.0]}), "batting")
    zscores.evaluate_roster(roster)
    assert batter.z_total is None


def test_evaluate_roster_without_a_players_table_resolves_nothing(conn, monkeypatch):
    from models import Player, TeamRoster

    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    _write(conn, _batting_frame(), "batting")
    zscores.save_z_scores()
    player = Player("Ann Aaron", "C", ["C"], "NYY", "Active", "Batter")
    zscores.evaluate_roster(TeamRoster(players=[player]))
    assert player.player_id is None


def test_database_readers_return_empty_results_without_a_database(monkeypatch):
    from models import Player, TeamRoster

    _no_database(monkeypatch)
    player = Player("Ann Aaron", "C", ["C"], "NYY", "Active", "Batter")
    zscores.evaluate_roster(TeamRoster(players=[player]))
    assert player.z_total is None
    assert zscores.load_team_lineup(1) is None
    assert zscores.load_players(None) == {"Batter": [], "Pitcher": []}
    assert zscores.load_free_agents("Batter") == []
    assert zscores.bp_ranks() == {}
    assert zscores.prospect_ranks() == {}
    assert zscores.cbs_ids() == {}
    assert zscores.external_ids() == {}


def test_team_lineup_comes_back_scored_in_lineup_order(league):
    assert zscores.load_team_lineup(1) is None  # no lineups table yet
    _create_lineups(league)
    assert zscores.load_team_lineup(1) is None  # table exists, team has no rows
    league.executemany(
        "INSERT INTO lineups VALUES (1, 3, '2026-05-01', %s, %s, %s, %s, %s, %s, %s, 'NYY', '2026-05-01T10:00')",
        [
            (2, "Pitcher", "Active", "SP", "q1", "Pat Pitch", "SP"),
            (1, "Batter", "Active", "C", "p1", "Ann Aaron", "C, 1B"),
            (3, "Batter", "Reserves", "BN", None, "Unknown Guy", "OF"),
        ],
    )
    league.commit()

    lineup, roster = zscores.load_team_lineup(1)
    assert lineup == {"effective_date": "2026-05-01", "period_id": 3, "captured_at": "2026-05-01T10:00"}
    assert [p.name for p in roster.players] == ["Ann Aaron", "Pat Pitch", "Unknown Guy"]
    assert roster.players[0].eligible_positions == ["C", "1B"] and roster.players[0].z_total is not None
    assert roster.players[2].z_total is None

    lineup, players = zscores.load_team_roster(1)
    assert [p.name for p in players["Batter"]] == ["Ann Aaron", "Unknown Guy"]
    assert [p.name for p in players["Pitcher"]] == ["Pat Pitch"]


def test_team_lineup_without_captured_at_column(league):
    _create_lineups(league, with_captured_at=False)
    league.execute(
        "INSERT INTO lineups VALUES (1, NULL, '2026-01-01', 1, 'Batter', 'Active', 'C', 'p1', 'Ann Aaron', 'C', 'NYY')"
    )
    league.commit()
    lineup, _roster = zscores.load_team_lineup(1)
    assert lineup["captured_at"] is None and lineup["period_id"] is None


def test_team_roster_falls_back_to_the_stats_tables_without_a_lineup(league):
    lineup, players = zscores.load_team_roster(1)
    assert lineup is None
    assert [p.name for p in players["Batter"]] == ["Ann Aaron"]
    assert [p.name for p in players["Pitcher"]] == ["Pat Pitch"]


def test_load_players_lists_scored_free_agents_best_value_first(league):
    free = zscores.load_players(None)
    names = [p.name for p in free["Batter"]]
    assert "Dan Dunn" not in names  # no at-bats
    assert set(names) == {"Cy Cole", "Eli Ellis"}
    assert len(zscores.load_players(None, limit=1)["Batter"]) == 1
    assert [p.name for p in zscores.load_players(2)["Batter"]] == ["Bob Bell"]


def test_load_players_skips_tables_that_are_missing_or_unscored(conn, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    assert zscores.load_players(1) == {"Batter": [], "Pitcher": []}


def test_free_agent_sort_validates_direction_and_key():
    assert zscores.free_agent_sort("Batter", None, None) == ("cbs", False)
    assert zscores.free_agent_sort("Batter", "vr", "") == ("vr", True)
    assert zscores.free_agent_sort("Batter", "vr", "asc") == ("vr", False)
    with pytest.raises(ValueError):
        zscores.free_agent_sort("Batter", "vr", "sideways")
    with pytest.raises(ValueError):
        zscores.free_agent_sort("Batter", "bogus", None)


def test_free_agent_sort_expressions(monkeypatch):
    expression = zscores._free_agent_sort_expression
    assert expression("Pitcher", "name") == "name"
    assert expression("Pitcher", "rank") == "sgp_total_above_replacement_rank"
    assert expression("Pitcher", "vr") == "sgp_total_above_replacement"
    assert expression("Pitcher", "stat:W+QS") == "(W + QS)"
    assert expression("Pitcher", "stat:Kd9") == "(K * 9 / NULLIF(INNs, 0))"
    assert expression("Pitcher", "stat:INN") == "`INNs`"
    assert expression("Batter", "stat:BA") == "`AVG`"


def test_load_free_agents_sorts_by_derived_stats_and_value(league):
    def names(player_type, **kwargs):
        return [p.name for p in zscores.load_free_agents(player_type, **kwargs)]

    assert names("Pitcher", sort="stat:W+QS") == ["Sam Starter"]
    assert names("Pitcher", sort="stat:Kd9") == ["Sam Starter"]
    assert names("Pitcher", sort="stat:ERA") == ["Sam Starter"]
    assert names("Batter", sort="rank") and names("Batter", sort="vr", direction="asc")
    assert names("Batter", sort="stat:HR") == ["Cy Cole", "Eli Ellis"]
    assert zscores.load_free_agents("Bench") == []


def test_load_free_agents_by_bp_rank_handles_unranked_and_empty_pages(league, monkeypatch):
    monkeypatch.setattr(zscores, "bp_ranks", lambda: {"p5": (1, None)})
    assert [p.name for p in zscores.load_free_agents("Batter", sort="bp")] == ["Eli Ellis", "Cy Cole"]
    assert zscores.load_free_agents("Batter", sort="bp", offset=10) == []


def test_load_free_agents_without_the_stats_tables(conn, monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    assert zscores.load_free_agents("Batter") == []
    assert zscores.load_free_agents("Batter", sort="bp") == []


def test_bp_ranks_reads_the_latest_snapshot_and_caches(conn, monkeypatch):
    assert zscores.bp_ranks() == {}  # no table
    monkeypatch.setattr(zscores, "_BP_RANKS", None)
    conn.execute(
        "CREATE TABLE rankings_dynasty (source VARCHAR(16), season INT, snapshot VARCHAR(16), player_id VARCHAR(32),"
        " `rank` INT, `change` INT)"
    )
    conn.executemany("INSERT INTO rankings_dynasty VALUES ('bp', %s, %s, %s, %s, %s)", [
        (2026, "2026-01", "p1", 9, None), (2026, "2026-02", "p1", 3, 2), (2026, "2026-02", "p2", 4, None),
    ])
    conn.commit()
    monkeypatch.setattr(zscores, "_BP_RANKS", (time.monotonic() - zscores.CBS_IDS_TTL - 1, {}))
    assert zscores.bp_ranks() == {"p1": (3, 2), "p2": (4, None)}
    _no_database(monkeypatch)
    assert zscores.bp_ranks() == {"p1": (3, 2), "p2": (4, None)}  # served from the cache


def test_prospect_ranks_keep_the_latest_lists_and_ranked_players(conn, monkeypatch):
    assert zscores.prospect_ranks() == {}  # no table
    monkeypatch.setattr(zscores, "_PROSPECTS", None)
    conn.execute(
        "CREATE TABLE rankings_prospects (source VARCHAR(16), season INT, list_type VARCHAR(32), snapshot VARCHAR(16),"
        " player_id VARCHAR(32), `rank` INT, `change` INT, org_rank INT, fv VARCHAR(8), level VARCHAR(8), eta INT)"
    )
    conn.executemany("INSERT INTO rankings_prospects VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", [
        ("bp", 2026, "batting", "2026-01", "p1", 50, 1, 2, "60", "AA", 2027),       # superseded
        ("bp", 2026, "batting", "2026-02", "p1", 5, 3, 1, "70", "AAA", 2026),
        ("fangraphs", 2026, "top_100", "2026-02", "p1", None, None, None, "55", "A", None),
        ("fangraphs", 2026, "top_100", "2026-02", "p2", 400, None, None, "40", "R", 2030),  # too deep to show
        ("xyz", 2026, "pitching", "2026-02", "p3", 100, None, None, None, None, None),
    ])
    conn.commit()
    monkeypatch.setattr(zscores, "_PROSPECTS", (time.monotonic() - zscores.CBS_IDS_TTL - 1, {}))
    found = zscores.prospect_ranks()
    assert set(found) == {"p1", "p3"}
    assert [e["list"] for e in found["p1"]] == ["Baseball Prospectus", "FanGraphs Top 100"]  # ranked entry first
    assert found["p1"][0] == {
        "list": "Baseball Prospectus", "rank": 5, "change": 3, "org_rank": 1, "fv": "70", "level": "AAA", "eta": 2026,
    }
    assert found["p3"][0]["list"] == "xyz" and found["p3"][0]["change"] is None
    _no_database(monkeypatch)
    assert zscores.prospect_ranks() is found


def test_cbs_and_external_ids_are_read_from_players_and_cached(league, monkeypatch):
    assert zscores.cbs_ids() == {"p1": "100"}
    assert zscores.external_ids() == {
        "p1": {"fangraphs": "fg1", "mlb": "m1"}, "p2": {}, "q1": {"bp": "bp3"},
    }
    _no_database(monkeypatch)
    assert zscores.cbs_ids() == {"p1": "100"}
    assert zscores.external_ids()["q1"] == {"bp": "bp3"}


def test_id_caches_tolerate_a_missing_players_table(conn):
    assert zscores.cbs_ids() == {}
    assert zscores.external_ids() == {}


def test_value_rank_puts_ranked_players_first():
    from models import Player

    ranked = Player("A", "", [], "NYY", "", "Batter", sgp_above_replacement={"total": (1.0, 4)})
    unranked = Player("B", "", [], "NYY", "", "Batter")
    assert sorted([unranked, ranked], key=zscores._value_rank) == [ranked, unranked]


def test_free_agent_sort_rejects_categories_the_league_does_not_score(monkeypatch):
    monkeypatch.setenv("BATTING_SCORING_CATEGORIES", "HR")
    assert zscores.free_agent_sort("Batter", "stat:HR", "desc") == ("stat:HR", True)
    with pytest.raises(ValueError):
        zscores.free_agent_sort("Batter", "stat:SB", None)


def test_load_free_agents_can_filter_by_whole_position(league):
    assert [p.name for p in zscores.load_free_agents("Batter", position="C")] == ["Cy Cole"]
    assert [p.name for p in zscores.load_free_agents("Batter", position="1B")] == ["Eli Ellis"]
    assert zscores.load_free_agents("Batter", position="S") == []
