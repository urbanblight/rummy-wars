"""Utility functions for validating MLB and CBS fantasy roster data.

This module contains the logic for translating CBS roster exports into internal
player objects, checking MLB API metadata, and evaluating a roster against the
league's rules for minors and injured-list slots.
"""

from __future__ import annotations

import csv
import datetime
import os
import re
from typing import Optional

import requests
from dotenv import load_dotenv

import logger
import models

load_dotenv()

LOGGER = logger.setup_logger("utils")


def is_milb(mlbam_player: dict) -> bool:
    """Return whether a player is currently assigned to a minor league team.

    The MLB StatsAPI response is inspected for the player's current team and the
    parent organization. If the current team differs from the parent org, the
    player is considered to be in the minor leagues.

    Args:
        mlbam_player: Dictionary-like player payload from the MLB StatsAPI.

    Returns:
        True if the player is on a minor-league team; otherwise False.
    """
    mlbam_player_id = mlbam_player.get("id")
    url = f"https://statsapi.mlb.com/api/v1/people/{mlbam_player_id}?hydrate=currentTeam"
    res = requests.get(url)
    res.raise_for_status()

    current_team = res.json().get("people", [])[0].get("currentTeam", {})
    try:
        current_team_id = current_team.get("id") if current_team.get("id") else current_team.get("parentOrgId")
        parent_org_id = current_team.get("parentOrgId") if current_team.get("parentOrgId") else current_team_id
        return int(current_team_id) != int(parent_org_id)
    except Exception as e:  # noqa: BLE001
        player_name = mlbam_player.get("fullName", "Unknown Player")
        LOGGER.error(
            f"Determining whether the current team for {player_name} "
            f"is the parent organization: {e}"
        )
        return False

def is_il(mlbam_player: dict) -> bool:
    """Determine whether the player is currently on the injured list.

    The function checks the player's current roster status and, if needed,
    reviews transaction history to determine whether the most recent IL placement
    is newer than the most recent activation.

    Args:
        mlbam_player: Dictionary-like MLB player payload from the StatsAPI.

    Returns:
        True if the player is currently on the injured list; otherwise False.
    """
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
                sorted_txns = sorted(
                    txns,
                    key=lambda x: datetime.datetime.strptime(
                        x.get("date", "1900-01-01"), "%Y-%m-%d"
                    ).replace(tzinfo=datetime.timezone.utc),
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
def get_mlb_latest_activation(mlbam_player: dict) -> str:
    """Return the most recent date on which the player was activated from the IL.

    Args:
        mlbam_player: Dictionary-like MLB player payload containing transaction
            history.

    Returns:
        A date string in YYYY-MM-DD format, or None if no activation is found.
    """
    mlbam_player_id = mlbam_player.get("id")
    url = f"https://statsapi.mlb.com/api/v1/people/{mlbam_player_id}?hydrate=transactions,currentTeam"
    res = requests.get(url)
    res.raise_for_status()

    # Parse and sort transactions chronologically (newest first)
    txns = res.json().get("people", [])[0].get("transactions", [])
    sorted_txns = sorted(
        txns,
        key=lambda x: datetime.datetime.strptime(
            x.get("date", "1900-01-01"), "%Y-%m-%d"
        ).replace(tzinfo=datetime.timezone.utc),
        reverse=True
    )
    last_il_activation = None

    for txn in sorted_txns:
        desc = txn.get("description", "").lower()
        # Capture the most recent IL Activation
        if not last_il_activation and ("activated" in desc and "injured list" in desc):
            last_il_activation = txn

    return last_il_activation.get("date")
    
def get_mlb_latest_callup(mlbam_id: str) -> str:
    """Return the most recent MLB promotion or recall date for a player.

    Args:
        mlbam_id: MLBAM player identifier used to fetch transaction history.

    Returns:
        The effective date of the most recent call-up or recall, or None if no
        qualifying transaction is found.
    """
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
    """Fetch career hitting and pitching totals for a player.

    The MLB StatsAPI returns season and career stat groupings. This helper reads
    the career stat split for the player and normalizes the relevant values into a
    small dictionary for rule checking.

    Args:
        player: Dictionary containing MLB player metadata and ID.

    Returns:
        A dictionary with at-bats under the "ab" key and innings pitched under
        the "ip" key.
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
    """Look up an MLB player record using the CBS roster player's name.

    A direct name match is not always exact, so the function queries the MLB
    StatsAPI's people search endpoint and selects the most likely result.

    Args:
        cbs_name: Raw player name string from CBS, such as "Shohei Ohtani".

    Returns:
        The matching MLB player dictionary, or None if no plausible result is
        returned.
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
    """Parse a CBS player string into name, positions, and team.

    CBS roster lines often follow the pattern:
        "Name Pos1,Pos2 | TEAM"

    Example:
        "Caleb Durbin 2B,3B | BOS" -> ("Caleb Durbin", ["2B", "3B"], "BOS")

    Args:
        player_str: Raw CBS roster text representing one player.

    Returns:
        A tuple containing the player name, a list of positions, and a team code.
        If parsing fails, the original text is returned with an empty position
        list and the team set to "UNKNOWN".
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
    """Parse a CBS fantasy baseball CSV export into a TeamRoster model.

    The CSV format includes section headers such as "Pitchers" and "Batters" as
    well as player rows that describe status, roster slot, team, and stats. This
    function normalizes that structure into the application's internal dataclasses.

    Args:
        file_path: File system path to the imported CBS CSV export.

    Returns:
        A TeamRoster object containing the parsed Player entries.
    """
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

def validate_league_rules(
    roster: models.TeamRoster,
    max_minors: Optional[int] = None,
    max_il: Optional[int] = None,
    max_reserves: Optional[int] = None,
) -> tuple[list[str], list[str]]:
    """Check a roster against the league's roster rules.

    The function inspects status counts first, then cross-references reserve, injured, and
    minor-league players with MLB stats and transaction data to flag players who
    are slotted incorrectly according to league rules.

    Args:
        roster: Team roster to validate.
        max_minors: Maximum Minors players; defaults to MAX_MINORS or 20.
        max_il: Maximum Injured-list players; defaults to MAX_IL or 8.
        max_reserves: Maximum Reserve players; defaults to MAX_RESERVES or 7.

    Returns:
        A tuple containing human-readable violation and warning messages.
    """

    max_minors = max_minors if max_minors is not None else int(os.getenv("MAX_MINORS", "20"))
    max_il = max_il if max_il is not None else int(os.getenv("MAX_IL", "8"))
    max_reserves = max_reserves if max_reserves is not None else int(
        os.getenv("MAX_RESERVES", "7")
    )

    violations = []
    warnings = []
    # Bench limits
    bench_count = roster.count_by_status("Reserves")
    if bench_count > max_reserves:
        violations.append(f"Exceeded Reserve Slot Limit: {bench_count}/{max_reserves}")
    else:
            LOGGER.info(f"Does not exceed reserve roster limit: {bench_count}")
    
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
            latest_activation_date = get_mlb_latest_activation(mlbam_player)
            il_warning = f"{player.name} is placed in an Injured slot and was activated {latest_activation_date}"
            warnings.append(il_warning)

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
                                    milb_warning = f"{player.name} is in a Minors slot but has more than 130 AB ({stats['ab']}). Most recent call up was {call_up_date}"
                                    warnings.append(milb_warning)
                                else:
                                    LOGGER.debug(f"{player.name} All-Time MLB AB: {stats['ab']}")
                            except Exception as e:  # noqa: BLE001
                                raise models.RummyWarsBaseError(f"Unable to determine total MLB AB for {player.name}: {e}")
                        elif player.player_type == 'Pitcher':
                            try:
                                if stats['ip'] > 50:
                                    call_up_date = get_mlb_latest_callup(mlbam_player_id)
                                    milb_warning = f"{player.name} is in a Minors slot but has more than 50 IP ({stats['ip']}). Most recent call up was {call_up_date}"
                                    warnings.append(milb_warning)
                                else:
                                    LOGGER.debug(f"{player.name} All-Time MLB IP: {stats['ip']}")
                            except Exception as e: # noqa: BLE001
                                    raise models.RummyWarsBaseError(f"Unable to determine total MLB IP for {player.name}: {e}")
                        else:
                            unexpected_position_warning = f"{player.name} is identified as neither a pitcher nor a batter but rather a {player.player_type}"
                            warnings.append(unexpected_position_warning)
                    else:
                        LOGGER.debug(f"{player.name} found MLB API but no stats found")
            else:
                pass # LOGGER.debug(f"No player found in MLB API for {player.name}")
        except Exception as e:  # noqa: BLE001
            LOGGER.warning(f"Unable to get MLB data for CBS name \"{player.name}\": {e}")
         
    return violations, warnings