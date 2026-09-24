import csv
import datetime
import re

import requests

import logger
import models

LOGGER = logger.setup_logger("utils")

def is_milb(mlbam_player: dict) -> bool:
    mlbam_player_id = mlbam_player.get("id")
    url = f"https://statsapi.mlb.com/api/v1/people/{mlbam_player_id}?hydrate=currentTeam"
    res = requests.get(url)
    res.raise_for_status()

    current_team =res.json().get("people", [])[0].get("currentTeam", [])
    try:
        current_team_id = current_team.get("id") if current_team.get("id") else current_team.get("parentOrgId")
        parent_org_id = current_team.get("parentOrgId") if current_team.get("parentOrgId") else current_team_id
        return int(current_team_id) != int(parent_org_id)
    except Exception as e:  # noqa: BLE001
        LOGGER.error(f"Determining whether the current team for {mlbam_player.name} is the parent organization: {e}")
        return False

def is_il(mlbam_player: dict) -> bool:
    mlbam_player_id = mlbam_player.get("id")
    url = f"https://statsapi.mlb.com/api/v1/people/{mlbam_player_id}?hydrate=transactions,currentTeam"
    res = requests.get(url)
    res.raise_for_status()

    person = res.json().get("people", [])[0]
    person_id = person.get("id")
    current_team = person.get("currentTeam", {})
    team_id = current_team.get("id")
    mlb_player_name = mlbam_player.get("fullName", "Unknown Player")
    
    if not team_id:
        LOGGER.info(f"{mlb_player_name} is a free agent or otherwise inactive.")
        return False
    else:
        roster_url = f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster?rosterType=40Man"
        roster_res = requests.get(roster_url)
        roster_res.raise_for_status()
        
        roster_entries = roster_res.json().get("roster", [])
        
        # Find the player in the roster array
        player_roster_entry = next(
            (item for item in roster_entries if item.get("person", {}).get("id") == person_id), 
            None
        )
        if player_roster_entry:
            # The roster entry contains the status object (e.g., code "D10", description "Injured 10-Day")
            status_info = player_roster_entry.get("status", {})
            status_desc = status_info.get("description", "Active")
            status_code = status_info.get("code", "")
            if status_code.startswith("D") or "Injured" in status_desc:
                return True
            else:
                # Parse and sort transactions chronologically (newest first)
                txns = person.get("transactions", [])
                LOGGER.debug(f"Number of transactions for {mlb_player_name}: {len(txns)}")
                sorted_txns = sorted(
                    txns,
                    key=lambda x: datetime.datetime.strptime(
                        x.get("date", "1900-01-01"), "%Y-%m-%d"
                    ).replace(tzinfo=datetime.UTC),
                    reverse=True
                )
                last_il_placement = None
                last_il_activation = None
                latest_rehab_assignment = None

                for txn in sorted_txns:
                    desc = txn.get("description", "").lower()
                    # Capture the most recent IL Placement
                    if not last_il_placement and ("placed" in desc and "injured list" in desc):
                        last_il_placement = txn

                    # Capture the most recent IL Activation
                    if not last_il_activation and ("activated" in desc and "injured list" in desc):
                        last_il_activation = txn

                    # Capture any recent Rehab Assignment
                    if not latest_rehab_assignment and ("rehab assignment" in desc):
                        latest_rehab_assignment = txn

                    # Once we have both placement and activation, we can stop scanning
                    if last_il_placement and last_il_activation:
                        break

                # Determine current IL status
                placement_date = last_il_placement.get("date") if last_il_placement else "1900-01-01"
                activation_date = last_il_activation.get("date") if last_il_activation else "1900-01-01"

                # Player is currently on IL if their most recent IL placement is newer than their last activation
                return placement_date > activation_date
        else:
            return False

def get_mlb_latest_callup(mlbam_id: str) -> str:
    url = f"https://statsapi.mlb.com/api/v1/transactions?playerId={mlbam_id}&startDate=2026-01-01"
    res = requests.get(url)
    res.raise_for_status()

    txns = res.json().get("transactions", [])

    # Filter for call-ups / recalls (typeCode 'CU' or 'R')
    call_ups = [
        t for t in txns
        if t.get("typeCode") in ["CU", "R", "SE"] or "recalled" in t.get("description", "").lower()
    ]
    if not call_ups:
        return None

    # Take the most recent transaction
    latest_call_up = call_ups[-1] if call_ups else None
    
    return latest_call_up.get("effectiveDate") if latest_call_up else '0000-00-00'

def get_mlb_career_totals(player: dict) -> dict:
    """Fetches career total AB and IP for a player via MLB StatsAPI.

    :param mlbam_id: Player's official MLBAM ID

    """
    mlbam_player_id = player.get("id")
    url = f"https://statsapi.mlb.com/api/v1/people/{mlbam_player_id}?hydrate=stats(group=[hitting,pitching],type=[career])"
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

def get_mlbam_player_by_name(cbs_name: str) -> dict | None:
    """Searches MLB StatsAPI for a player name and returns their mlbam_id.

    :param cbs_name: Raw player name string from CBS (e.g., "Shohei Ohtani")
    :return: MLBAM Player object,
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
    return max(people, key=lambda person: getattr(person, "birthDate", datetime.datetime.min)) # noqa: DTZ901

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
    
    # Minors limits
    minors_count = roster.count_by_status("Minors")
    if minors_count > max_minors:
        violations.append(f"Exceeded Minors Slot Limit: {minors_count}/{max_minors}")
    else:
        LOGGER.info(f"Does not exceed MiLB roster limit: {minors_count}")
        
    # Injured Reserve limits
    il_count = roster.count_by_status("Injured")
    if il_count > max_il:
        violations.append(f"Exceeded Injured Reserve Limit: {il_count}/{max_il}")
    else:
        LOGGER.info(f"Does not exceed IL roster limit: {il_count}")

    # BEGIN CHECKING DATA OUTSIDE CSV

    # Check if player with injured status is on the IL
    for player in roster.get_by_status("Injured"):
        mlbam_player = get_mlbam_player_by_name(player.name)
        if is_il(mlbam_player):
            LOGGER.debug(f"{player.name} is placed in an Injured slot and is on the IL")
        else:
            LOGGER.warning(f"{player.name} is placed in an Injured slot but is not on the IL")

    # Check if Minors players have few enough MLB ABs or IP to be slotted in MiLB slot
    for player in roster.get_by_status("Minors"):
        try:
            mlbam_player = get_mlbam_player_by_name(player.name)
            if mlbam_player:
                mlbam_player_id = mlbam_player.get("id")
                if is_milb(mlbam_player):
                    LOGGER.debug(f"{player.name} is currently on an MiLB team.")
                else: 
                    stats = get_mlb_career_totals(mlbam_player) if mlbam_player_id else None
                    if stats:
                        if player.player_type == 'Batter':
                            try:
                                if stats['ab'] > 130:
                                    call_up_date = get_mlb_latest_callup(mlbam_player_id)
                                    LOGGER.warning(f"{player.name} is in a Minors slot but has more than 130 AB ({stats['ab']}). Most recent call up was {call_up_date}")
                                else:
                                    LOGGER.debug(f"{player.name} All-Time MLB AB: {stats['ab']}")
                            except Exception as e:  # noqa: BLE001
                                raise models.RummyWarsBaseError(f"Unable to determine total MLB AB for {player.name}: {e}")
                        elif player.player_type == 'Pitcher':
                            try:
                                if stats['ip'] > 50:
                                    call_up_date = get_mlb_latest_callup(mlbam_player_id)
                                    LOGGER.warning(f"{player.name} is in a Minors slot but has more than 50 IP ({stats['ip']}). Most recent call up was {call_up_date}")
                                else:
                                    LOGGER.debug(f"{player.name} All-Time MLB IP: {stats['ip']}")
                            except Exception as e: # noqa: BLE001
                                    raise models.RummyWarsBaseError(f"Unable to determine total MLB IP for {player.name}: {e}")
                        else:
                            LOGGER.warning(f"{player.name} is identified as neither a pitcher nor a batter but rather a {player.player_type}")
                    else:
                        LOGGER.debug(f"{player.name} found MLB API but no stats found")
            else:
                pass # LOGGER.debug(f"No player found in MLB API for {player.name}")
        except Exception as e:  # noqa: BLE001
            LOGGER.warning(f"Unable to get MLB data for CBS name \"{player.name}\": {e}")
         
    return violations