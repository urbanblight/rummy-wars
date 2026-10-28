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
    assert zscores.z_column("K/9") == "z_K_9"
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
