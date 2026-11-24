import prospects


def row(**values):
    base = {c.strip("`"): None for c in prospects.COLUMNS}
    return {**base, **values}


def test_build_merges_sources_and_report():
    rows = [
        row(source="fangraphs", list_type="batting", player_id="m1", rank=3, org_rank=1, name="Jesús Made",
            team="MIL", pos="SS", age=19.4, fv="65", hit="40 / 60", spd="50 / 45"),
        row(source="bp", list_type="midseason", player_id="m1", rank=1, change=2, name="Jesús Made",
            team="MIL", pos="SS", sec_pos="3B, 2B", level="AA", eta=2027),
    ]
    [p] = prospects.build_prospects(rows, "Batter")
    assert (p.fangraphs_rank, p.bp_rank, p.bp_change, p.org_rank, p.fv) == (3, 1, 2, 1, "65")
    assert p.eligible_positions == ["SS", "3B", "2B"]
    assert p.report == [["Hit", "40 / 60"], ["Spd", "50 / 45"]]
    assert (p.level, p.eta) == ("AA", "2027")


def test_sort_puts_missing_last_and_fv_plus_above_base():
    a = prospects.Prospect(name="A", fv="45", org_rank=2)
    b = prospects.Prospect(name="B", fv="45+", fangraphs_rank=9, org_rank=1)
    c = prospects.Prospect(name="C", bp_rank=4, org_rank=3)
    assert [p.name for p in prospects.sort_prospects([a, b, c], "rank", False)] == ["C", "B", "A"]
    assert [p.name for p in prospects.sort_prospects([a, b, c], "fv", True)] == ["B", "A", "C"]
    assert [p.name for p in prospects.sort_prospects([a, b, c], "org", True)] == ["C", "A", "B"]


def test_fantasy_teams_none_without_lineups():
    class Conn:
        def __init__(self, rows=None):
            self.rows = rows

        def execute(self, sql):
            if self.rows is None:
                raise prospects.db.SchemaError[0]("q", {}, Exception("no table"))
            return type("R", (), {"fetchall": lambda _self: self.rows})()

    assert prospects.fantasy_teams(Conn()) is None
    assert prospects.fantasy_teams(Conn([("a", 1, "X"), ("b", 2, "Y")])) == {"a": (1, "X"), "b": (2, "Y")}


def test_sort_by_team_treats_blank_as_lowest():
    a = prospects.Prospect(name="A", fantasy_team="Zed")
    b = prospects.Prospect(name="B")
    c = prospects.Prospect(name="C", fantasy_team="Alpha")
    assert [p.name for p in prospects.sort_prospects([a, b, c], "team", False)] == ["B", "C", "A"]
    assert [p.name for p in prospects.sort_prospects([a, b, c], "team", True)] == ["A", "C", "B"]


def test_mlb_rank_is_merged_and_sortable():
    a = prospects.Prospect(name="A", mlb_rank=5)
    b = prospects.Prospect(name="B", mlb_rank=2)
    c = prospects.Prospect(name="C")
    assert [p.name for p in prospects.sort_prospects([a, b, c], "mlb", False)] == ["B", "A", "C"]
