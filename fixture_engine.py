"""
Fixture Generation Logic
========================

Round Robin (Berger Algorithm):
- For N teams, generates N-1 rounds (if N is even) or N rounds (if N is odd)
- Each team plays every other team exactly once
- For odd N, a dummy "BYE" is added to make it even, that match becomes a bye

Knockout:
- Teams are paired: [0 vs N-1, 1 vs N-2, ...]
- If odd number of teams, last team gets a bye (advances automatically)
- Winners advance to next round until one champion remains
"""

import math
import random


def generate_round_robin_fixtures(teams):
    """
    Generate round-robin fixtures using the Berger/Circle algorithm.
    
    Returns: list of rounds, each round is a list of (team1, team2) tuples
             team2 can be None to indicate a BYE
    """
    team_list = list(teams)
    n = len(team_list)
    
    # Add dummy team for BYE if odd number of teams
    has_bye = False
    if n % 2 != 0:
        team_list.append(None)  # None = BYE
        n += 1
        has_bye = True
    
    total_rounds = n - 1
    half = n // 2
    
    rounds = []
    
    # Fixed team at position 0, rotate the rest
    fixed = team_list[0]
    rotating = team_list[1:]
    
    for round_num in range(total_rounds):
        round_matches = []
        
        # Build the full list for this round
        round_teams = [fixed] + rotating
        
        for i in range(half):
            team1 = round_teams[i]
            team2 = round_teams[n - 1 - i]
            
            # Skip if both are bye (shouldn't happen but safety check)
            if team1 is None and team2 is None:
                continue
            
            round_matches.append((team1, team2))
        
        rounds.append(round_matches)
        
        # Rotate: move last element to front of rotating list
        rotating = [rotating[-1]] + rotating[:-1]
    
    return rounds


def generate_knockout_fixtures(teams):
    """
    Generate first-round knockout fixtures.
    
    Pairs teams for elimination. If odd teams, last team gets a BYE.
    Returns: list of (team1, team2) tuples (team2 can be None for BYE)
    """
    team_list = list(teams)
    random.shuffle(team_list)  # Random seeding
    
    matches = []
    i = 0
    
    while i < len(team_list) - 1:
        matches.append((team_list[i], team_list[i + 1]))
        i += 2
    
    # If odd team left, they get a bye
    if i < len(team_list):
        matches.append((team_list[i], None))
    
    return matches


def get_knockout_rounds_needed(num_teams):
    """Calculate total rounds needed for knockout with N teams."""
    return math.ceil(math.log2(num_teams))


def advance_knockout_round(current_round_matches):
    """
    Given completed matches from current round,
    pair up winners for next round.
    
    Returns: list of (winner1, winner2) pairs
    """
    winners = []
    
    for match in current_round_matches:
        if match.winner_id:
            winners.append(match.winner)
        elif match.is_bye and match.team1_id:
            winners.append(match.team1)  # BYE team advances
    
    # Pair winners sequentially
    next_round = []
    i = 0
    while i < len(winners) - 1:
        next_round.append((winners[i], winners[i + 1]))
        i += 2
    
    # Handle odd winner (gets bye in next round)
    if i < len(winners):
        next_round.append((winners[i], None))
    
    return next_round


def validate_teams(team_names):
    """
    Validate team names before fixture generation.
    Returns (is_valid, error_message)
    """
    if not team_names:
        return False, "No teams provided."
    
    if len(team_names) < 2:
        return False, "At least 2 teams are required."
    
    # Check for empty names
    for name in team_names:
        if not name or not name.strip():
            return False, "Team names cannot be empty."
    
    # Check for duplicates (case-insensitive)
    normalized = [name.strip().lower() for name in team_names]
    if len(normalized) != len(set(normalized)):
        return False, "Duplicate team names are not allowed."
    
    return True, None
