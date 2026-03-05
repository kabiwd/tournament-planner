"""
scheduler.py — Round-Robin Match Scheduler (v6)
================================================

IPL-style daily spread model:
  Weekdays (Mon–Fri): at most 1 match, default slot 7:30 PM
  Weekends (Sat–Sun): at most 2 matches, slots 3:30 PM and 7:30 PM

Algorithm:
----------
1. Build all valid (date, slot) pairs in [start_date, end_date], ordered
   chronologically. Each date's capacity: 1 slot for weekdays, 2 for weekends.

2. Seed state from all already-completed and already-scheduled matches so that
   rest-gap constraints are correct even when re-running after partial results.
   Manually-locked matches are seeded but not touched.

3. Walk pending (non-locked) matches in round/match order. For each, try every
   slot until one satisfies:
     a. Slot not already occupied
     b. Neither team already has a match on that date
     c. Neither team played within the last min_rest_days days

4. Assign and update tracking, or leave unscheduled.

5. Return (scheduled_count, unscheduled_count).

Side effects: modifies match.scheduled_at and match.status in place.
Caller commits.
"""

from datetime import date, datetime, timedelta

DEFAULT_DURATION_DAYS  = 60
DEFAULT_MIN_REST_DAYS  = 2

# Per-weekday slot config.
# Key: weekday int (0=Mon … 6=Sun)
# Value: list of "HH:MM" strings
DAILY_SLOTS = {
    0: ["19:30"],           # Monday
    1: ["19:30"],           # Tuesday
    2: ["19:30"],           # Wednesday
    3: ["19:30"],           # Thursday
    4: ["19:30"],           # Friday
    5: ["15:30", "19:30"],  # Saturday
    6: ["15:30", "19:30"],  # Sunday
}

# All allowed slot times across the whole week (for validation)
ALL_ALLOWED_TIMES = {"15:30", "19:30"}


def schedule_round_robin(matches, start_date, end_date=None,
                         min_rest_days=None):
    """
    Assign scheduled_at (and set status='scheduled') for unscheduled, non-locked
    pending matches.

    Matches with manual_locked=True are seeded into state tracking but never
    reassigned — their existing scheduled_at is preserved.

    Parameters
    ----------
    matches       : ALL Match objects for the tournament (including completed,
                    scheduled, locked), ordered by round_number, match_number.
    start_date    : date — first allowed scheduling date.
    end_date      : date — last allowed date (default start + 60 days).
    min_rest_days : int  — minimum gap between a team's matches (default 2).

    Returns
    -------
    (scheduled_count, unscheduled_count)
    """
    if end_date is None:
        end_date = start_date + timedelta(days=DEFAULT_DURATION_DAYS)
    if min_rest_days is None:
        min_rest_days = DEFAULT_MIN_REST_DAYS

    slots = _generate_slots(start_date, end_date)
    if not slots:
        pending = sum(
            1 for m in matches
            if m.status == 'pending' and not m.is_bye and not m.manual_locked
        )
        return 0, pending

    # ── Seed state from completed + scheduled (including locked) matches ─────
    team_last_date: dict = {}   # team_id → most recent match date
    team_dates: dict     = {}   # team_id → set of dates with a match
    slot_fill: dict      = {}   # datetime → bool

    for m in matches:
        if m.is_bye:
            continue
        if m.status == 'completed' and m.team1_id and m.team2_id:
            d = m.scheduled_at.date() if m.scheduled_at else None
            if d:
                _record_match(m.team1_id, m.team2_id, d, m.scheduled_at,
                              team_last_date, team_dates, slot_fill)
        elif m.status == 'scheduled' and m.scheduled_at:
            d = m.scheduled_at.date()
            _record_match(m.team1_id, m.team2_id, d, m.scheduled_at,
                          team_last_date, team_dates, slot_fill)

    # ── Schedule pending, non-locked matches ─────────────────────────────────
    scheduled_count   = 0
    unscheduled_count = 0

    for m in matches:
        if m.is_bye or m.status in ('completed', 'scheduled'):
            continue
        if m.manual_locked:
            # Respect lock: don't touch it even if pending
            continue
        if m.status != 'pending':
            continue
        if m.team1_id is None or m.team2_id is None:
            unscheduled_count += 1
            continue

        t1, t2   = m.team1_id, m.team2_id
        assigned = False

        for (slot_date, slot_time_str) in slots:
            slot_dt = _to_datetime(slot_date, slot_time_str)

            if slot_fill.get(slot_dt):
                continue
            if slot_date in team_dates.get(t1, set()):
                continue
            if slot_date in team_dates.get(t2, set()):
                continue
            if not _rest_ok(t1, slot_date, team_last_date, min_rest_days):
                continue
            if not _rest_ok(t2, slot_date, team_last_date, min_rest_days):
                continue

            m.scheduled_at = slot_dt
            m.status       = 'scheduled'
            _record_match(t1, t2, slot_date, slot_dt,
                          team_last_date, team_dates, slot_fill)
            scheduled_count += 1
            assigned = True
            break

        if not assigned:
            unscheduled_count += 1

    return scheduled_count, unscheduled_count


def clear_pending_schedule(matches):
    """
    Reset scheduled_at and status back to 'pending' for all matches that are
    currently 'scheduled' (time assigned, no result) AND NOT manually locked.

    Completed matches are never touched.
    Manually-locked matches are never touched.
    Does NOT commit.
    """
    for m in matches:
        if m.status == 'scheduled' and not m.is_bye and not m.manual_locked:
            m.scheduled_at = None
            m.status       = 'pending'


def validate_manual_schedule(tournament, match, new_dt):
    """
    Hard-validate a manually proposed datetime for a match.

    Returns (is_valid: bool, error_message: str | None).

    Rules enforced:
      1. Date must be within tournament scheduling window (if configured).
      2. Time must be one of the allowed slot times (15:30 or 19:30).
      3. Weekday capacity: weekdays allow only 19:30; weekends allow 15:30 and 19:30.
      4. Neither team may already have a match on that date (in tournament).
      5. Minimum rest gap respected for both teams.
    """
    from models import Match as MatchModel
    import re

    proposed_date = new_dt.date()
    proposed_time = new_dt.strftime("%H:%M")
    weekday       = proposed_date.weekday()

    # Rule 1: Must be within tournament window if configured
    if tournament.sched_start_date and proposed_date < tournament.sched_start_date:
        return False, (
            f"Date {proposed_date} is before the tournament start date "
            f"({tournament.sched_start_date})."
        )
    if tournament.sched_end_date and proposed_date > tournament.sched_end_date:
        return False, (
            f"Date {proposed_date} is after the tournament end date "
            f"({tournament.sched_end_date})."
        )

    # Rule 2: Must be an allowed slot time
    if proposed_time not in ALL_ALLOWED_TIMES:
        return False, (
            f"Time {proposed_time} is not an allowed slot. "
            f"Allowed: {', '.join(sorted(ALL_ALLOWED_TIMES))}."
        )

    # Rule 3: Weekday capacity — weekdays only allow 19:30
    allowed_for_day = DAILY_SLOTS.get(weekday, [])
    if proposed_time not in allowed_for_day:
        day_name = proposed_date.strftime("%A")
        return False, (
            f"{day_name} only allows: {', '.join(allowed_for_day)}. "
            f"You chose {proposed_time}."
        )

    # Rule 4: Same-team same-day conflict
    other_matches = MatchModel.query.filter(
        MatchModel.tournament_id == tournament.id,
        MatchModel.id != match.id,
        MatchModel.scheduled_at.isnot(None),
    ).all()

    for m in other_matches:
        if m.scheduled_at.date() == proposed_date:
            teams_in_m = {m.team1_id, m.team2_id}
            teams_in_new = {match.team1_id, match.team2_id}
            if teams_in_m & teams_in_new:
                # Find which team
                overlap = teams_in_m & teams_in_new
                return False, (
                    f"A team already has a match scheduled on {proposed_date}. "
                    f"Same-day conflicts are not allowed."
                )

    # Rule 5: Minimum rest gap
    min_rest = tournament.sched_min_rest or DEFAULT_MIN_REST_DAYS
    for team_id in (match.team1_id, match.team2_id):
        for m in other_matches:
            if m.team1_id != team_id and m.team2_id != team_id:
                continue
            if not m.scheduled_at:
                continue
            other_date = m.scheduled_at.date()
            gap = abs((proposed_date - other_date).days)
            if gap > 0 and gap < min_rest:
                return False, (
                    f"Rest gap violation: one team has a match on {other_date} "
                    f"({gap} day(s) away). Minimum rest required: {min_rest} days."
                )

    return True, None


# ── Private helpers ───────────────────────────────────────────────────────────

def _generate_slots(start: date, end: date) -> list:
    """Generate (date, time_str) pairs using IPL-style daily capacity rules."""
    slots = []
    cur = start
    while cur <= end:
        for t in sorted(DAILY_SLOTS.get(cur.weekday(), [])):
            slots.append((cur, t))
        cur += timedelta(days=1)
    return slots


def _to_datetime(d: date, time_str: str) -> datetime:
    h, m = map(int, time_str.split(":"))
    return datetime(d.year, d.month, d.day, h, m)


def _rest_ok(team_id: int, slot_date: date,
             team_last_date: dict, min_rest_days: int) -> bool:
    last = team_last_date.get(team_id)
    if last is None:
        return True
    return (slot_date - last).days >= min_rest_days


def _record_match(t1, t2, d: date, dt: datetime,
                  team_last_date, team_dates, slot_fill):
    """Update tracking dicts after a match is assigned to a slot."""
    slot_fill[dt] = True
    for tid in (t1, t2):
        team_dates.setdefault(tid, set()).add(d)
        prev = team_last_date.get(tid)
        if prev is None or d > prev:
            team_last_date[tid] = d
