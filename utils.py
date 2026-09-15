import csv
import re

import requests

import logger
import models

LOGGER = logger.setup_logger("utils")

def get_mlb_career_totals(mlbam_id: int) -> dict:
    """Fetches career total AB and IP for a player via MLB StatsAPI.

    :param mlbam_id: Player's official MLBAM ID

    """
    url = f"https://statsapi.mlb.com/api/v1/people/{mlbam_id}?hydrate=stats(group=[hitting,pitching],type=[career])"
    res = requests.get(url)
    res.raise_for_status()

    result = {"ab": 0, "ip": 0.0}
    stats = res.json().get("people", [])[0].get("stats", [])

    for stat_group in stats:
        group_name = stat_group.get("group", {}).get("displayName")
        splits = stat_group.get("splits", [])

        if not splits:
            continue

        career_data = splits[0].get("stat", {})

        if group_name == "hitting":
            result["ab"] = int(career_data.get("atBats", 0))
        elif group_name == "pitching":
            # IP comes back as a string (e.g., "2450.1")
            result["ip"] = float(career_data.get("inningsPitched", 0.0))

    return result

def get_mlbam_id_by_name(cbs_name: str) -> int | None:
    """Searches MLB StatsAPI for a player name and returns their mlbam_id.

    :param cbs_name: Raw player name string from CBS (e.g., "Shohei Ohtani")
    :return: MLBAM ID as an integer, or None if not found
    """
    clean_name = cbs_name.strip()
    params = {"names": clean_name}

    url = "https://statsapi.mlb.com/api/v1/people/search"
    res = requests.get(url, params=params)
    if res.status_code != 200:
        LOGGER.error(f"HTTP status code: {res.status_code}")
        return None

    people = res.json().get("people", [])
    if not people:
        LOGGER.debug(f"Unable to match CBS name '{cbs_name}' to MLB player")
        return None
    # Exact case-insensitive match check
    for person in people:
        full_name = person.get("fullName", "")
        if full_name.lower() == clean_name.lower():
            return int(person["id"])

    # Fallback to the youngest match returned by MLB search engine
    return int(max(people, key=lambda person: person.birthDate)["id"])

def parse_cbs_player_string(player_str: str):
    """
    Parses 'Name Pos1,Pos2 | TEAM' into (name, [positions], team).
    Example: 'Caleb Durbin 2B,3B | BOS' -> ('Caleb Durbin', ['2B', '3B'], 'BOS')
    """
    # Restrict position characters to upper/lower letters, numbers, and commas (no spaces)
    pattern = r"^(.*?)\s+([A-Za-z0-9,]+)\s*\|\s*([A-Z]{2,3})$"
    match = re.match(pattern, player_str.strip())
    if match:
        name, pos_str, team = match.groups()
        positions = [p.strip() for p in pos_str.split(',') if p.strip()]
        return name.strip(), positions, team.strip()
    return player_str.strip(), [], "UNKNOWN"

def parse_cbs_roster_csv(file_path: str) -> models.TeamRoster:
    """Parses a CBS Sports Fantasy Baseball roster export CSV into a TeamRoster object."""
    roster = models.TeamRoster()
    current_type = "Batter"
    current_status = "Active"
    
    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            
            line_str = ",".join(row).strip()
            
            # State tracking based on CBS structural headers
            if line_str.startswith("Pitchers"):
                current_type = "Pitcher"
                current_status = "Active"
                continue
            elif line_str.startswith("Batters"):
                current_type = "Batter"
                current_status = "Active"
                continue
            elif line_str.startswith("Reserves"):
                current_status = "Reserves"
                continue
            elif line_str.startswith("Injured"):
                current_status = "Injured"
                continue
            elif line_str.startswith("Minors"):
                current_status = "Minors"
                continue
            elif line_str.startswith("Active:"):
                continue  # Skip summary footer

            # Ignore column sub-headers
            if len(row) > 1 and row[1].strip() == "Pos":
                continue
            
            # Parse player rows
            if len(row) >= 3 and row[2].strip():
                slot = row[1].strip()
                player_raw = row[2].strip()
                
                name, positions, team = parse_cbs_player_string(player_raw)
                
                # Extract basic stats
                stats = {}
                if current_type == "Batter" and len(row) >= 13:
                    try:
                        stats = {
                            "BA": float(row[8]) if row[8] != 'N/A' else None,
                            "R": int(row[9]) if row[9] else 0,
                            "HR": int(row[10]) if row[10] else 0,
                            "RBI": int(row[11]) if row[11] else 0,
                            "SB": int(row[12]) if row[12] else 0,
                        }
                    except ValueError:
                        pass
                elif current_type == "Pitcher" and len(row) >= 13:
                    try:
                        stats = {
                            "ERA": float(row[8]) if row[8] != 'N/A' else None,
                            "WHIP": float(row[9]) if row[9] != 'N/A' else None,
                            "W": int(row[10]) if row[10] else 0,
                            "S": int(row[11]) if row[11] else 0,
                            "K": int(row[12]) if row[12] else 0,
                        }
                    except ValueError:
                        pass

                roster.players.append(
                    models.Player(
                        name=name,
                        roster_slot=slot,
                        eligible_positions=positions,
                        team=team,
                        status=current_status,
                        player_type=current_type,
                        stats=stats
                    )
                )
                
    return roster

def validate_league_rules(roster: models.TeamRoster, max_minors: int = 20, max_il: int = 8) -> list[str]:
    violations = []
    
    # Rule 1: Minors limits
    minors_count = roster.count_by_status("Minors")
    if minors_count > max_minors:
        violations.append(f"Exceeded Minors Slot Limit: {minors_count}/{max_minors}")
    else:
        LOGGER.info(f"Does not exceed MiLB roster limit: {minors_count}")
        
    # Rule 2: Injured Reserve limits
    il_count = roster.count_by_status("Injured")
    if il_count > max_il:
        violations.append(f"Exceeded Injured Reserve Limit: {il_count}/{max_il}")
    else:
        LOGGER.info(f"Does not exceed IL roster limit: {il_count}")

    # BEGIN CHECKING DATA OUTSIDE CSV

    # Rule 4: Check if Minors players have few enough MLB ABs or IP to be slotted in MiLB slot

    for player in roster.get_by_status("Minors"):
        try:
            mlbam_id = get_mlbam_id_by_name(player.name)
            if not mlbam_id:
                pass
            else:
                stats = get_mlb_career_totals(mlbam_id) if mlbam_id else None
            if not stats:
                pass
            else:
                if player.player_type == 'Batter':
                    try:
                        if stats['ab'] > 130:
                            LOGGER.warning(f"{player.name} is in a Minors slot but has more than 130 AB ({stats['ab']}).")
                        else:
                            LOGGER.debug(f"{player.name} All-Time MLB AB: {stats['ab']}")
                    except Exception as e:  # noqa: BLE001
                        raise models.RummyWarsBaseError(f"Unable to determine total MLB AB for {player.name}: {e}")
                elif player.player_type == 'Pitcher':
                    try:
                        if stats['ip'] > 50:
                            LOGGER.warning(f"{player.name} is in a Minors slot but has more than 50 IP ({stats['ip']}).")
                        else:
                            LOGGER.debug(f"{player.name} All-Time MLB IP: {stats['ip']}")
                    except Exception as e: # noqa: BLE001
                            raise models.RummyWarsBaseError(f"Unable to determine total MLB IP for {player.name}: {e}")
                else:
                    LOGGER.warning(f"{player.name} is identified as neither a pitcher nor a batter but rather a {player.player_type}")

        except Exception as e:  # noqa: BLE001
            LOGGER.warning(f"Unable to get MLB data for CBS name \"{player.name}\": {e}")
         
    return violations