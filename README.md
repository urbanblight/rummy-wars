# rummy-wars

UI and CLI tool to check compliance with roster requirements for the Rummy Wars fantasy baseball league on the CBS Fantasy Baseball platform.

<p align="center">
  <img src="https://raw.githubusercontent.com/urbanblight/rummy-wars/main/docs/assets/rw_homepage.png" alt="A screenshot of the FantasyRummyWars.com homepage" width="300">
</p>

To learn about using the version of this app deployed in production at https://fantasyrummywars.com, please visit our UI end-user documentation in the wiki [here](https://github.com/urbanblight/rummy-wars/wiki/Using-FantasyRummyWars.com).

To learn about using the CLI tool, launching the UI locally, or developing and deploying FantasyRummyWars.com, please visit our developer documentation in the wiki [here](https://github.com/urbanblight/rummy-wars/wiki/How-to-Develop-and-Deploy-FantasyRummyWars.com).

To access the Rummy Wars fantasy baseball league on the CBS Fantasy Baseball platform, click [here](https://rummywars.baseball.cbssports.com/).

## Refreshing the stats database

The player Z-scores, standings gain points (SGP) and above-replacement values shown on the results page are calculated ahead of time and stored in a MySQL database (8.0.13 or newer), configured by the `DATABASE_URL` environment variable (for example `mysql+pymysql://user:password@host:3306/rummy_wars`; put it in `.env`, which is not tracked by git). The Teams pages (`/teams`, listed in the order of the latest imported standings, and `/teams/<id>` for each team's players) read the same database. Rebuild it whenever the source files change:

```bash
source .venv/bin/activate
python scripts/refresh.py [--year 2026] [--data-dir data]
```

`refresh.py` runs these steps in order and stops at the first failure:

1. `import_teams.py` loads teams and managers from `data/teams.csv`.
2. `import_players.py` upserts the canonical player ID map (SFBB `PLAYERIDMAP`) into the `players` table, from `data/player-id-map.csv` if present, otherwise downloaded. Safe to rerun; lineups and stats store each player's `player_id` from it instead of relying on names.
3. `import_standings.py` loads every `data/standings/<year>.csv` as that season's full-year snapshot. Re-importing replaces the snapshot.
4. `import_lineups.py` loads every roster-overview file in `data/lineups` (skipped if there are none).
5. `import_cbs_stats.py` loads the player stats for `--year`. It first recalculates the SGP factors (`calculate_sgp.py`) from the imported standings, then stores the Z-scores, SGP, above-replacement values and ranks.

Lineups can also be imported on their own with `python scripts/import_lineups.py [path ...]` (default `data/lineups`). The CBS roster-overview files must be named `roster-overview-<team-id>-<date>.csv` for the offseason lineup, or `roster-overview-<team-id>-<period-id>-<date>.csv` in season, with the date as `YYYYMMDD`. Each player becomes a row of the `lineups` table (team, period, effective date, status, slot, name, positions, MLB team, CBS ranks and roster/start percentages), and re-importing a lineup replaces it. Import teams and players first. CBS exports carry only names, so each is matched to a `player_id` by name and MLB team; names missing from the ID map (mostly deep minor leaguers) get a NULL `player_id` and a warning.

Each script can also be run on its own, for example `import_standings.py data/standings/2026.csv --as-of 2026-06-01` for an in-season standings snapshot (`refresh.py` only imports full-year files).

### Importing projections

Projection systems (ZiPS, Steamer, ...) are imported separately from `refresh.py`, into the `batting_projections` and `pitching_projections` tables:

```bash
source .venv/bin/activate
python scripts/import_players.py                      # once, so projections can be linked to player ids
python scripts/import_projections.py                  # every file in data/projections
python scripts/import_projections.py data/projections/ZIPS-2027-batting.csv
python scripts/import_rankings.py                     # dynasty rankings in data/rankings (<source>-<season>-dynasty.csv)
```

- Put FanGraphs-style CSV exports in `data/projections`, named `<system>-<season>-<batting|pitching>.csv`, for example `ZIPS-2027-batting.csv`. The system, season and table come from the file name; other `.csv` files in the folder are skipped with a warning.
- Each row stores `system`, `season`, the projected stats and a canonical `player_id`, matched from the export's FanGraphs `PlayerId` (then `MLBAMID`) against the `players` table. Players missing from the ID map (mostly prospects) get a NULL `player_id` and are counted in a warning.
- On a team page (`/teams/<id>`), the Stats column header has a dropdown: Season to Date (the `batting` / `pitching` tables) or any imported system and season, such as ZIPS 2027, which shows that projection's latest snapshot for each player (`?stats=ZIPS-2027`). Players a system doesn't project, or that have no `player_id`, show no stats.
- Every run inserts its rows under a new `snapshot` (UTC timestamp), so rerunning or importing a revised file keeps the earlier projections instead of replacing them. To read the latest, filter on the newest snapshot:

```sql
SELECT * FROM batting_projections
WHERE `system` = 'ZIPS' AND season = 2027
  AND snapshot = (SELECT MAX(snapshot) FROM batting_projections WHERE `system` = 'ZIPS' AND season = 2027);
```


### Requirements

- The virtual environment from `scripts/setup.sh`.
- `data/teams.csv`, the CBS team export (team name, abbreviation, manager, manager email).
- `data/<year>/stats-batting.csv` and `data/<year>/stats-pitching.csv`, the CBS player-pool exports.
- `data/standings/<year>.csv`, the CBS standings export for each full season. SGP needs at least one.
- A `teams_history` row for every team in each standings season, since standings team names are matched against it (ignoring case, punctuation and a leading "The").
- `.env` settings:
  - `DATABASE_URL`, the MySQL connection string. `DATABASE_URL` is read from the environment, so it can also be exported in the shell; when deploying, `scripts/deploy.sh` loads it from a Secret Manager secret named `DATABASE_URL`.
  - `BATTING_SCORING_CATEGORIES` and `PITCHING_SCORING_CATEGORIES`, using the standings category names (`HR,R,SB,BA,OBP,SLG,RBI` and `W+QS,S,ERA,WHIP,Kd9,HD,INN`).
  - `BATTING_ACTIVE_SLOTS` and `PITCHING_ACTIVE_SLOTS`, the lineup size used to estimate a typical team's playing time (default 10).
  - `MAX_ACTIVE_SP` and `MAX_ACTIVE_RP`, the most pitcher slots a team can give to starters or relievers. Pitcher slots are taken from the imported lineups when available, otherwise split evenly within these limits. A pitcher role's replacement level is never lower than the players just outside the league's active slots for that role.

Without any imported standings the stats import still runs, but it warns and leaves the SGP factors unchanged.

### Running the database tests

Tests that touch the database run against a throwaway MySQL schema named by `TEST_DATABASE_URL` (the schema name must contain `test`; all its tables are dropped before each test) and are skipped when it is not set:

```bash
docker run -d --name rw-mysql-test -e MYSQL_ROOT_PASSWORD=test -e MYSQL_DATABASE=rummy_wars_test -p 33306:3306 mysql:8.4
export TEST_DATABASE_URL=mysql+pymysql://root:test@127.0.0.1:33306/rummy_wars_test
pytest tests
```
