"""
match_routes.py — v6

Key changes from v5:
- Round Robin results: ALL pending matches are editable — round-based locking
  removed entirely for round_robin. Knockout keeps round-gated flow.
- Manual schedule editing: HARD validation (not soft warnings) via
  scheduler.validate_manual_schedule(). Setting a valid time also sets
  match.manual_locked = True. Clearing also clears the lock.
- Re-schedule protection: locked matches are never touched by the scheduler.
"""

from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from models import db, Tournament, Match, Team, StandingEntry, RESULT_MODE_WINNER

match_bp = Blueprint('match', __name__)


# ─────────────────────────────────────────────────────────────────────────────
# Guards
# ─────────────────────────────────────────────────────────────────────────────

def _get_match_or_404(tournament_id, match_id):
    match = Match.query.get_or_404(match_id)
    if match.tournament_id != tournament_id:
        abort(404)
    return match


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures view
# ─────────────────────────────────────────────────────────────────────────────

@match_bp.route('/tournament/<int:tournament_id>/fixtures')
def fixtures(tournament_id):
    tournament    = Tournament.query.get_or_404(tournament_id)
    round_filter  = request.args.get('round', type=int)
    status_filter = request.args.get('status', '')
    search_query  = request.args.get('q', '').strip().lower()

    q = Match.query.filter_by(tournament_id=tournament_id)
    if round_filter:
        q = q.filter_by(round_number=round_filter)
    if status_filter in ['pending', 'scheduled', 'completed']:
        q = q.filter_by(status=status_filter)

    all_matches = q.order_by(Match.round_number, Match.match_number).all()

    if search_query:
        all_matches = [
            m for m in all_matches
            if search_query in (m.team1.name.lower() if m.team1 else '')
            or search_query in (m.team2.name.lower() if m.team2 else '')
        ]

    all_rounds = [
        r[0] for r in
        db.session.query(Match.round_number)
        .filter_by(tournament_id=tournament_id)
        .distinct()
        .order_by(Match.round_number)
        .all()
    ]

    return render_template(
        'fixtures.html',
        tournament=tournament,
        matches=all_matches,
        all_rounds=all_rounds,
        round_filter=round_filter,
        status_filter=status_filter,
        search_query=search_query,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Results page — format-aware result entry
# ─────────────────────────────────────────────────────────────────────────────

@match_bp.route('/tournament/<int:tournament_id>/results')
def results_page(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    if tournament.format == 'round_robin':
        # Round Robin: ALL pending/scheduled non-bye matches are editable.
        # No round-based locking whatsoever.
        pending_matches = Match.query.filter(
            Match.tournament_id == tournament_id,
            Match.status.in_(['pending', 'scheduled']),
            Match.is_bye == False,
        ).order_by(Match.round_number, Match.match_number).all()

        # For RR, we present all pending in one flat list.
        editable_matches = pending_matches
        locked_matches   = []

    else:
        # Knockout: only current round is editable.
        current_round = tournament.current_round

        editable_matches = Match.query.filter(
            Match.tournament_id == tournament_id,
            Match.round_number == current_round,
            Match.status.in_(['pending', 'scheduled']),
            Match.is_bye == False,
        ).order_by(Match.match_number).all()

        # Matches in future rounds — shown as locked
        locked_matches = Match.query.filter(
            Match.tournament_id == tournament_id,
            Match.round_number > current_round,
            Match.status.in_(['pending', 'scheduled']),
            Match.is_bye == False,
        ).order_by(Match.round_number, Match.match_number).all()

        pending_matches = editable_matches + locked_matches

    completed_matches = []
    if tournament.format == 'round_robin':
        completed_matches = Match.query.filter_by(
            tournament_id=tournament_id,
            status='completed',
            is_bye=False,
        ).order_by(Match.round_number.desc(), Match.match_number).limit(20).all()

    return render_template(
        'results.html',
        tournament=tournament,
        editable_matches=editable_matches,
        locked_matches=locked_matches,
        pending_matches=pending_matches,
        completed_matches=completed_matches,
        current_round=tournament.current_round,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Submit a result
# ─────────────────────────────────────────────────────────────────────────────

@match_bp.route('/tournament/<int:tournament_id>/match/<int:match_id>/update', methods=['POST'])
def update_result(tournament_id, match_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    match      = _get_match_or_404(tournament_id, match_id)

    if match.status == 'completed':
        flash('This match is already completed. Use Edit to correct it.', 'warning')
        return redirect(url_for('match.results_page', tournament_id=tournament_id))

    # Knockout: only current round is submittable (guard against URL hacking)
    if tournament.format == 'knockout' and match.round_number != tournament.current_round:
        flash('This match cannot be updated yet — complete the current round first.', 'warning')
        return redirect(url_for('match.results_page', tournament_id=tournament_id))

    try:
        if tournament.is_winner_only():
            _apply_winner_only_result(tournament, match)
        else:
            _apply_score_based_result(tournament, match)

        if tournament.format == 'round_robin':
            _update_standings(tournament, match)
            _check_round_robin_complete(tournament)
        else:
            _handle_knockout_progression(tournament, match)

        db.session.commit()
        flash('Match result saved!', 'success')

    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Unexpected error: {str(e)}', 'danger')

    return redirect(url_for('match.results_page', tournament_id=tournament_id))


# ─────────────────────────────────────────────────────────────────────────────
# Edit a completed result (round-robin only)
# ─────────────────────────────────────────────────────────────────────────────

@match_bp.route('/tournament/<int:tournament_id>/match/<int:match_id>/edit', methods=['POST'])
def edit_result(tournament_id, match_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    match      = _get_match_or_404(tournament_id, match_id)

    if match.status != 'completed':
        flash('Can only edit completed matches.', 'warning')
        return redirect(url_for('match.results_page', tournament_id=tournament_id))

    if tournament.format == 'knockout':
        flash('Knockout results cannot be edited after bracket progression.', 'warning')
        return redirect(url_for('match.results_page', tournament_id=tournament_id))

    try:
        _reverse_standings(tournament, match)

        if tournament.is_winner_only():
            _apply_winner_only_result(tournament, match)
        else:
            _apply_score_based_result(tournament, match)

        _update_standings(tournament, match)
        _check_round_robin_complete(tournament)
        db.session.commit()
        flash('Result corrected and standings recalculated.', 'success')

    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'danger')
    except Exception as e:
        db.session.rollback()
        flash(f'Unexpected error: {str(e)}', 'danger')

    return redirect(url_for('match.results_page', tournament_id=tournament_id))


# ─────────────────────────────────────────────────────────────────────────────
# Manual schedule edit — HARD validation, sets manual_locked = True
# ─────────────────────────────────────────────────────────────────────────────

@match_bp.route('/tournament/<int:tournament_id>/match/<int:match_id>/set-schedule', methods=['POST'])
def set_match_schedule(tournament_id, match_id):
    """
    Manually set or clear the scheduled_at for a single match.

    Setting a valid datetime:
      - Runs hard validation via scheduler.validate_manual_schedule().
      - Sets match.manual_locked = True so the auto-scheduler will not
        overwrite this value on re-runs.

    Clearing:
      - Removes scheduled_at, resets status to 'pending', clears manual_locked.
    """
    from scheduler import validate_manual_schedule

    tournament = Tournament.query.get_or_404(tournament_id)
    match      = _get_match_or_404(tournament_id, match_id)

    if match.status == 'completed':
        flash('Cannot reschedule a completed match.', 'warning')
        return redirect(url_for('match.fixtures', tournament_id=tournament_id))

    raw_dt = request.form.get('scheduled_at', '').strip()

    if not raw_dt:
        # Clear scheduling and lock
        match.scheduled_at  = None
        match.manual_locked = False
        if match.status == 'scheduled':
            match.status = 'pending'
        db.session.commit()
        flash('Schedule cleared for this match.', 'info')
        return redirect(url_for('match.fixtures', tournament_id=tournament_id))

    try:
        new_dt = datetime.fromisoformat(raw_dt)
    except ValueError:
        flash('Invalid date/time format.', 'danger')
        return redirect(url_for('match.fixtures', tournament_id=tournament_id))

    # Hard validation — reject if any rule is violated
    is_valid, error_msg = validate_manual_schedule(tournament, match, new_dt)
    if not is_valid:
        flash(f'Schedule rejected: {error_msg}', 'danger')
        return redirect(url_for('match.fixtures', tournament_id=tournament_id))

    match.scheduled_at  = new_dt
    match.status        = 'scheduled'
    match.manual_locked = True   # protect from auto-scheduler overwrites
    db.session.commit()
    flash(
        f'Match scheduled for {new_dt.strftime("%a %d %b %Y, %I:%M %p")} '
        f'(🔒 locked from auto-reschedule).',
        'success',
    )
    return redirect(url_for('match.fixtures', tournament_id=tournament_id))


# ─────────────────────────────────────────────────────────────────────────────
# Result application helpers
# ─────────────────────────────────────────────────────────────────────────────

def _apply_score_based_result(tournament, match):
    raw1 = request.form.get('score1', '').strip()
    raw2 = request.form.get('score2', '').strip()

    if raw1 == '' or raw2 == '':
        raise ValueError('Both scores are required. Enter 0 for a shutout.')

    try:
        score1, score2 = int(raw1), int(raw2)
    except ValueError:
        raise ValueError('Scores must be whole numbers.')

    if score1 < 0 or score2 < 0:
        raise ValueError('Scores cannot be negative.')

    if tournament.format == 'knockout' and score1 == score2:
        raise ValueError('Knockout matches cannot end in a draw. Enter different scores.')

    match.team1_score = score1
    match.team2_score = score2
    match.result_type = None

    if score1 > score2:
        match.winner_id = match.team1_id
    elif score2 > score1:
        match.winner_id = match.team2_id
    else:
        match.winner_id = None  # draw (round-robin only)

    match.status = 'completed'


def _apply_winner_only_result(tournament, match):
    """
    Parse result for winner_only sports.
    Valid outcome types are sport-specific (from tournament.allowed_outcomes()).
    """
    result_type = request.form.get('result_type', '').strip()
    winner_raw  = request.form.get('winner_team_id', '').strip()

    allowed = tournament.allowed_outcomes()
    if result_type not in allowed:
        raise ValueError(
            f'Invalid result type for {tournament.sport_type}. '
            f'Allowed: {", ".join(allowed)}.'
        )

    if tournament.format == 'knockout' and result_type != 'win':
        raise ValueError('Knockout matches must have a winner.')

    if result_type == 'win':
        if not winner_raw:
            raise ValueError('Please select the winning team.')
        try:
            winner_id = int(winner_raw)
        except ValueError:
            raise ValueError('Invalid winner selection.')
        if winner_id not in (match.team1_id, match.team2_id):
            raise ValueError('Winner must be one of the two competing teams.')
        match.winner_id = winner_id

    elif result_type == 'tie':
        match.winner_id = None

    else:  # no_result
        match.winner_id = None

    match.team1_score = None
    match.team2_score = None
    match.result_type = result_type
    match.status      = 'completed'


# ─────────────────────────────────────────────────────────────────────────────
# Standings helpers
# ─────────────────────────────────────────────────────────────────────────────

def _update_standings(tournament, match):
    e1 = StandingEntry.query.filter_by(
        tournament_id=tournament.id, team_id=match.team1_id).first()
    e2 = StandingEntry.query.filter_by(
        tournament_id=tournament.id, team_id=match.team2_id).first()
    if not e1 or not e2:
        return

    if match.result_type == 'no_result':
        e1.played += 1; e2.played += 1
        e1.no_results += 1; e2.no_results += 1
        return

    e1.played += 1; e2.played += 1

    if tournament.is_score_based() and match.team1_score is not None:
        e1.score_for     += match.team1_score
        e1.score_against += match.team2_score
        e2.score_for     += match.team2_score
        e2.score_against += match.team1_score

    if match.winner_id == match.team1_id:
        e1.wins += 1;   e1.points += tournament.points_win
        e2.losses += 1; e2.points += tournament.points_loss
    elif match.winner_id == match.team2_id:
        e2.wins += 1;   e2.points += tournament.points_win
        e1.losses += 1; e1.points += tournament.points_loss
    else:
        # Draw / Tie
        e1.draws += 1;  e1.points += tournament.points_draw
        e2.draws += 1;  e2.points += tournament.points_draw


def _reverse_standings(tournament, match):
    e1 = StandingEntry.query.filter_by(
        tournament_id=tournament.id, team_id=match.team1_id).first()
    e2 = StandingEntry.query.filter_by(
        tournament_id=tournament.id, team_id=match.team2_id).first()
    if not e1 or not e2:
        return

    if match.result_type == 'no_result':
        e1.played     = max(0, e1.played - 1)
        e2.played     = max(0, e2.played - 1)
        e1.no_results = max(0, e1.no_results - 1)
        e2.no_results = max(0, e2.no_results - 1)
        return

    e1.played = max(0, e1.played - 1)
    e2.played = max(0, e2.played - 1)

    if tournament.is_score_based() and match.team1_score is not None:
        e1.score_for     = max(0, e1.score_for     - match.team1_score)
        e1.score_against = max(0, e1.score_against - match.team2_score)
        e2.score_for     = max(0, e2.score_for     - match.team2_score)
        e2.score_against = max(0, e2.score_against - match.team1_score)

    if match.winner_id == match.team1_id:
        e1.wins   = max(0, e1.wins   - 1)
        e1.points = max(0, e1.points - tournament.points_win)
        e2.losses = max(0, e2.losses - 1)
    elif match.winner_id == match.team2_id:
        e2.wins   = max(0, e2.wins   - 1)
        e2.points = max(0, e2.points - tournament.points_win)
        e1.losses = max(0, e1.losses - 1)
    else:
        e1.draws  = max(0, e1.draws  - 1)
        e1.points = max(0, e1.points - tournament.points_draw)
        e2.draws  = max(0, e2.draws  - 1)
        e2.points = max(0, e2.points - tournament.points_draw)


# ─────────────────────────────────────────────────────────────────────────────
# Tournament completion helpers
# ─────────────────────────────────────────────────────────────────────────────

def _check_round_robin_complete(tournament):
    pending = Match.query.filter(
        Match.tournament_id == tournament.id,
        Match.status.in_(['pending', 'scheduled']),
        Match.is_bye == False,
    ).count()
    if pending == 0:
        tournament.status = 'completed'


def _handle_knockout_progression(tournament, completed_match):
    current_round = tournament.current_round

    pending_in_round = Match.query.filter(
        Match.tournament_id == tournament.id,
        Match.round_number == current_round,
        Match.status.in_(['pending', 'scheduled']),
        Match.is_bye == False,
    ).count()

    if pending_in_round > 0:
        return

    round_matches = Match.query.filter_by(
        tournament_id=tournament.id, round_number=current_round).all()
    winners = [m.winner for m in round_matches if m.winner_id]

    if len(winners) == 1:
        tournament.status = 'completed'
        return

    next_round = current_round + 1
    tournament.current_round = next_round

    i, match_num = 0, 1
    while i < len(winners) - 1:
        db.session.add(Match(
            tournament_id=tournament.id,
            round_number=next_round,
            match_number=match_num,
            team1_id=winners[i].id,
            team2_id=winners[i + 1].id,
            status='pending',
        ))
        i += 2
        match_num += 1

    if i < len(winners):
        db.session.add(Match(
            tournament_id=tournament.id,
            round_number=next_round,
            match_number=match_num,
            team1_id=winners[i].id,
            is_bye=True,
            winner_id=winners[i].id,
            status='completed',
        ))
