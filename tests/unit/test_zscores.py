import pytest
import pandas as pd

import zscores


def test_compute_z_scores_inverts_lower_is_better():
    df = pd.DataFrame({"rank": [1, 2, 3], "HR": [10, 20, 30], "ERA": [2.0, 3.0, 4.0]})
    out = zscores.compute_z_scores(df, ["HR", "ERA"], {})
    assert out["z_HR"].tolist() == [-1.0, 0.0, 1.0]
    assert out["z_ERA"].tolist() == [1.0, 0.0, -1.0]
    assert out["z_total"].tolist() == [0.0, 0.0, 0.0]
    assert out["z_HR_rank"].tolist() == [3, 2, 1]
    assert out["z_ERA_rank"].tolist() == [1, 2, 3]


def test_no_playing_time_gets_no_scores_or_rank():
    df = pd.DataFrame({"rank": [1, 2, 3], "AB": [100, 200, 0], "HR": [10, 20, 0]})
    out = zscores.compute_z_scores(df, ["HR"], {}, "AB")
    assert out["z_HR"].isna().tolist() == [False, False, True]
    assert out["z_HR_rank"].tolist()[:2] == [2, 1]


def test_z_column_is_sqlite_safe():
    assert zscores.z_column("Kd9") == "z_Kd9"
    assert zscores.z_column("W+QS") == "z_W_QS"


def test_normalize_name_strips_accents_and_punctuation():
    assert zscores.normalize_name("José Ramírez-Jr.") == "jose ramirez jr"


def test_ratio_categories_are_weighted_by_playing_time():
    df = pd.DataFrame({"rank": [1, 2, 3, 4], "AB": [600, 600, 30, 30], "BA": [0.3, 0.2, 0.3, 0.2], "HR": [30, 15, 1, 1]})
    plain = zscores.compute_z_scores(df, ["BA", "HR"], {})
    weighted = zscores.compute_z_scores(df, ["BA", "HR"], {}, "AB")
    assert weighted["z_HR"].tolist() == plain["z_HR"].tolist()
    # Same ratio, fewer AB: lower z whether the ratio is good or bad.
    assert weighted["z_BA"][2] < weighted["z_BA"][0]
    assert weighted["z_BA"][3] < weighted["z_BA"][1]
    assert weighted["z_BA"][3] < plain["z_BA"][3]


def test_compute_sgp_scales_counting_stats_by_factor_and_inverts_lower_is_better():
    df = pd.DataFrame({
        "rank": [1, 2, 3], "HR": [10, 20, 30], "ERA": [2.0, 3.0, 4.0],
        "AB": [100, 100, 100], "team_id": [1, 1, 2],
    })
    out = zscores.compute_sgp(df, ["HR", "ERA"], {}, {"HR": 5.0, "ERA": 0.5}, "AB", 200)
    # HR: (value - 20) / 5. ERA is a rate: (3.0 - value) * AB / team AB / 0.5.
    assert out["sgp_HR"].tolist() == [-2.0, 0.0, 2.0]
    assert out["sgp_ERA"].tolist() == [1.0, 0.0, -1.0]
    assert out["sgp_total"].tolist() == [-1.0, 0.0, 1.0]
    assert out["sgp_total_rank"].tolist() == [3, 2, 1]


def test_compute_sgp_scales_rate_stats_by_share_of_team_volume():
    df = pd.DataFrame({"rank": [1, 2], "OBP": [0.4, 0.2], "AB": [600, 100]})
    out = zscores.compute_sgp(df, ["OBP"], {}, {"OBP": 0.01}, "AB", 6000)
    # Pool mean .300: (.4 - .3) * 600 / 6000 / .01 = 1.0; (.2 - .3) * 100 / 6000 / .01 = -1/6.
    assert out["sgp_OBP"].tolist() == pytest.approx([1.0, -1 / 6])


def test_compute_sgp_leaves_out_players_without_playing_time_and_missing_factors():
    df = pd.DataFrame({"rank": [1, 2], "HR": [10, 20], "AB": [0, 100]})
    out = zscores.compute_sgp(df, ["HR"], {}, {"HR": 5.0}, "AB", 100)
    assert pd.isna(out.loc[0, "sgp_total"])
    assert out.loc[1, "sgp_total_rank"] == 1
    assert "sgp_HR" not in zscores.compute_sgp(df, ["HR"], {}, {}, "AB", 100)


def test_sgp_column_is_sqlite_safe():
    assert zscores.sgp_column("W+QS") == "sgp_W_QS"


def test_lineup_volume_sums_only_the_top_slots_of_each_team():
    df = pd.DataFrame({
        "team_id": [1, 1, 1, 2, 2, None],
        "AB": [600, 500, 50, 400, 300, 700],
    })
    # Team 1: 600 + 500; team 2: 400 + 300. Free agents (no team) are ignored.
    assert zscores.lineup_volume(df, "AB", 2) == pytest.approx((1100 + 700) / 2)


def test_team_volume_uses_standings_innings_for_pitchers(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE standings_categories (season_year, as_of_date, category, value)")
    conn.executemany("INSERT INTO standings_categories VALUES (?, ?, 'INN', ?)", [
        (2025, "2025-12-31", 1000.0), (2026, "2026-12-31", 1200.0), (2026, "2026-12-31", 1300.0),
        (2026, "2026-06-01", 5.0),
    ])
    df = pd.DataFrame({"team_id": [1], "INNs": [900.0]})

    assert zscores.team_volume(conn, df, "Pitcher") == pytest.approx(1250.0)
    # Batters always use the lineup's AB.
    assert zscores.team_volume(conn, pd.DataFrame({"team_id": [1], "AB": [500]}), "Batter") == 500


def test_team_volume_falls_back_to_lineup_without_standings(tmp_path):
    import sqlite3

    df = pd.DataFrame({"team_id": [1, 1], "INNs": [100.0, 50.0]})
    conn = sqlite3.connect(tmp_path / "t.db")
    assert zscores.team_volume(conn, df, "Pitcher") == pytest.approx(150.0)


def test_eligible_positions_merges_dh_into_ut_and_keeps_outfield_positions_distinct():
    assert zscores.eligible_positions("DH", "Batter") == ["UT"]
    assert zscores.eligible_positions("LF,CF,RF", "Batter") == ["LF", "CF", "RF"]
    assert zscores.eligible_positions("DH,UT", "Batter") == ["UT"]
    assert zscores.eligible_positions("DH,SP", "Pitcher") == ["SP"]
    assert zscores.eligible_positions(None, "Batter") == []


def test_compute_above_replacement_uses_top_free_agents_and_cheapest_eligible_position(monkeypatch):
    monkeypatch.setattr(zscores, "REPLACEMENT_POOL", 3)
    df = pd.DataFrame({
        "positions": ["C", "1B", "C,1B", "C", "C", "C", "C", "1B", "SS"],
        "team_id": [1, 2, 3, None, None, None, None, None, None],
        "z_total": [5.0, 5.0, 5.0, 3.0, 2.0, 1.0, -9.0, 4.0, 1.0],
    })
    out = zscores.compute_above_replacement(df, "Batter", "z_total")
    # C replacement: mean of best 3 FA catchers = 2.0. 1B: only one FA, 4.0. SS: 1.0.
    assert out["z_total_above_replacement"].tolist()[:3] == [3.0, 1.0, 3.0]
    assert out.loc[8, "z_total_above_replacement"] == 0.0
    assert out.loc[0, "z_total_above_replacement_rank"] == 1


def test_compute_above_replacement_is_nan_without_free_agents_at_the_position():
    df = pd.DataFrame({"positions": ["C", "1B"], "team_id": [1, None], "z_total": [5.0, 2.0]})
    out = zscores.compute_above_replacement(df, "Batter", "z_total")
    assert pd.isna(out.loc[0, "z_total_above_replacement"])
    assert out.loc[1, "z_total_above_replacement"] == 0.0


def test_category_replacement_comes_from_the_same_players_as_the_total_and_adds_up():
    # Free agents: A is best overall but poor in SB, B is a pure base stealer ranked lower overall.
    df = pd.DataFrame({
        "positions": ["C"] * 4,
        "team_id": [1, None, None, None],
        "z_HR": [3.0, 5.0, 0.0, -9.0],
        "z_SB": [1.0, 0.0, 6.0, 0.0],
        "z_total": [4.0, 5.0, 6.0, -9.0],
    })
    out = zscores.compute_above_replacement(df, "Batter", "z_total", ["z_HR", "z_SB"])
    # Replacement players are the 5 best free agents (all 3 here); each category averages the same three.
    assert out.loc[0, "z_total_above_replacement"] == pytest.approx(4.0 - (5.0 + 6.0 - 9.0) / 3)
    assert out.loc[0, "z_HR_above_replacement"] == pytest.approx(3.0 - (5.0 + 0.0 - 9.0) / 3)
    assert out.loc[0, "z_SB_above_replacement"] == pytest.approx(1.0 - (0.0 + 6.0 + 0.0) / 3)
    parts = out.loc[0, ["z_HR_above_replacement", "z_SB_above_replacement"]].sum()
    assert parts == pytest.approx(out.loc[0, "z_total_above_replacement"])


def test_a_multi_position_player_uses_one_position_for_every_category():
    df = pd.DataFrame({
        "positions": ["C,1B", "C", "1B"],
        "team_id": [1, None, None],
        "z_HR": [0.0, 1.0, 4.0],
        "z_total": [0.0, 1.0, 4.0],
    })
    out = zscores.compute_above_replacement(df, "Batter", "z_total", ["z_HR"])
    # Lowest replacement total is C (1.0), so the HR baseline is also the catcher's.
    assert out.loc[0, "z_total_above_replacement"] == -1.0
    assert out.loc[0, "z_HR_above_replacement"] == -1.0


def test_z_total_is_the_average_of_the_category_z_scores():
    df = pd.DataFrame({"rank": [1, 2, 3], "HR": [10, 20, 30], "SB": [10, 20, 30]})
    out = zscores.compute_z_scores(df, ["HR", "SB"], {})
    assert out["z_total"].tolist() == [-1.0, 0.0, 1.0]


def test_replacement_level_averages_the_five_best_free_agents():
    df = pd.DataFrame({
        "positions": ["C"] * 7,
        "team_id": [1, None, None, None, None, None, None],
        "z_total": [9.0, 6.0, 5.0, 4.0, 3.0, 2.0, -50.0],
    })
    out = zscores.compute_above_replacement(df, "Batter", "z_total")
    assert zscores.REPLACEMENT_POOL == 5
    assert out.loc[0, "z_total_above_replacement"] == 9.0 - 4.0


def test_scoring_stat_derives_category_values_from_a_stats_row():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 6 AS W, 4 AS QS, 90 AS K, 60.0 AS INNs, .27 AS AVG, 12 AS HR").fetchone()

    assert zscores.scoring_stat(row, "W+QS", {}) == 10
    assert zscores.scoring_stat(row, "Kd9", {}) == 13.5
    assert zscores.scoring_stat(row, "BA", {"BA": "AVG"}) == .27
    assert zscores.scoring_stat(row, "INN", {"INN": "INNs"}) == 60.0
    assert zscores.scoring_stat(row, "HR", {}) == 12


def test_pool_is_the_top_250_ranked_players_of_each_table():
    assert zscores.POOL_SIZE == 250
    in_pool = [10, 20] * 125
    df = pd.DataFrame({"rank": list(range(1, 301)), "HR": in_pool + [1000] * 50})
    out = zscores.compute_z_scores(df, ["HR"], {})
    # The 50 players beyond the pool do not move its average (15) or spread.
    assert out.loc[0, "z_HR"] < 0 < out.loc[1, "z_HR"]
    assert out.loc[0, "z_HR"] == -out.loc[1, "z_HR"]
    assert out.loc[299, "z_HR"] > 100
