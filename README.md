# rummy-wars

UI and CLI tool to check compliance with roster requirements for the Rummy Wars fantasy baseball league on the CBS Fantasy Baseball platform.

<p align="center">
  <img src="https://raw.githubusercontent.com/urbanblight/rummy-wars/main/docs/assets/rw_homepage.png" alt="A screenshot of the FantasyRummyWars.com homepage" width="300">
</p>

To learn about using the version of this app deployed in production at https://fantasyrummywars.com, please visit our UI end-user documentation in the wiki [here](https://github.com/urbanblight/rummy-wars/wiki/Using-FantasyRummyWars.com).

To learn about using the CLI tool, launching the UI locally, or developing and deploying FantasyRummyWars.com, please visit our developer documentation in the wiki [here](https://github.com/urbanblight/rummy-wars/wiki/How-to-Develop-and-Deploy-FantasyRummyWars.com).

To access the Rummy Wars fantasy baseball league on the CBS Fantasy Baseball platform, click [here](https://rummywars.baseball.cbssports.com/).

## Refreshing the stats database

The player Z-scores, standings gain points (SGP) and above-replacement values shown on the results page are calculated ahead of time and stored in `data/stats.db` (not tracked by git). Rebuild them whenever the source files change:

```bash
source .venv/bin/activate
python scripts/refresh.py [--year 2026] [--data-dir data] [--db data/stats.db]
```

`refresh.py` runs these steps in order and stops at the first failure:

1. `import_teams.py` loads teams and managers from `data/teams.csv`.
2. `import_standings.py` loads every `data/standings/<year>.csv` as that season's full-year snapshot. Re-importing replaces the snapshot.
3. `import_cbs_stats.py` loads the player stats for `--year`. It first recalculates the SGP factors (`calculate_sgp.py`) from the imported standings, then stores the Z-scores, SGP, above-replacement values and ranks.

Each script can also be run on its own, for example `import_standings.py data/standings/2026.csv --as-of 2026-06-01` for an in-season standings snapshot (`refresh.py` only imports full-year files).

### Requirements

- The virtual environment from `scripts/setup.sh`.
- `data/teams.csv`, the CBS team export (team name, abbreviation, manager, manager email).
- `data/<year>/stats-batting.csv` and `data/<year>/stats-pitching.csv`, the CBS player-pool exports.
- `data/standings/<year>.csv`, the CBS standings export for each full season. SGP needs at least one.
- A `teams_history` row for every team in each standings season, since standings team names are matched against it (ignoring case, punctuation and a leading "The").
- `.env` settings:
  - `BATTING_SCORING_CATEGORIES` and `PITCHING_SCORING_CATEGORIES`, using the standings category names (`HR,R,SB,BA,OBP,SLG,RBI` and `W+QS,S,ERA,WHIP,Kd9,HD,INN`).
  - `BATTING_ACTIVE_SLOTS` and `PITCHING_ACTIVE_SLOTS`, the lineup size used to estimate a typical team's playing time (default 10).

Without any imported standings the stats import still runs, but it warns and leaves the SGP factors unchanged.
