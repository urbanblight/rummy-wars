import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import import_teams  # noqa: E402


def test_team_ids_follow_cbs_and_skip_three():
    assert [import_teams.cbs_team_id(n) for n in range(1, 8)] == [1, 2, 4, 5, 6, 7, 8]


def test_load_teams_assigns_cbs_ids_in_file_order(tmp_path):
    csv = tmp_path / "teams.csv"
    rows = "\n".join(f",Team {n},T{n},Manager {n},m{n}@x.com,," for n in range(1, 5))
    csv.write_text("Table 1\n,,,,,,\n,TEAM NAME,TEAM ABBREV.,MANAGER,MANAGER EMAIL,,\n" + rows + "\n")

    teams = import_teams.load_teams(csv)

    assert teams["id"].tolist() == [1, 2, 4, 5]
    assert teams["name"].tolist() == ["Team 1", "Team 2", "Team 3", "Team 4"]
