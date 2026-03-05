from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from models import db, Tournament, Team

team_bp = Blueprint('team', __name__)

MIN_TEAMS = 2
MAX_TEAMS = 32


def _normalize(name):
    """Return the DB-level name key: stripped and lowercased."""
    return name.strip().lower()


def _get_team_or_404(tournament_id, team_id):
    team = Team.query.get_or_404(team_id)
    if team.tournament_id != tournament_id:
        abort(404)
    return team


@team_bp.route('/tournament/<int:tournament_id>/teams', methods=['GET', 'POST'])
def manage_teams(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    teams = Team.query.filter_by(
        tournament_id=tournament_id
    ).order_by(Team.created_at).all()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            name = request.form.get('team_name', '').strip()
            if not name:
                flash('Team name cannot be empty.', 'danger')
            elif len(name) > 100:
                flash('Team name must be 100 characters or fewer.', 'danger')
            elif not tournament.is_setup():
                flash('Cannot add teams after fixtures are generated.', 'warning')
            elif len(teams) >= MAX_TEAMS:
                flash(f'Maximum {MAX_TEAMS} teams allowed.', 'danger')
            else:
                key = _normalize(name)
                # Python-side check (fast UX feedback before DB round-trip)
                existing_keys = [_normalize(t.name) for t in teams]
                if key in existing_keys:
                    flash(f'A team named "{name}" already exists (case-insensitive).', 'danger')
                else:
                    try:
                        db.session.add(Team(
                            name=name,
                            name_key=key,
                            tournament_id=tournament_id,
                        ))
                        db.session.commit()
                        flash(f'Team "{name}" added.', 'success')
                    except Exception:
                        db.session.rollback()
                        flash(f'Team name "{name}" already exists.', 'danger')

        return redirect(url_for('team.manage_teams', tournament_id=tournament_id))

    n = len(teams)
    rr_match_count = (n * (n - 1)) // 2 if n >= 2 else 0

    return render_template(
        'teams.html',
        tournament=tournament,
        teams=teams,
        rr_match_count=rr_match_count,
        min_teams=MIN_TEAMS,
        max_teams=MAX_TEAMS,
    )


@team_bp.route('/tournament/<int:tournament_id>/teams/<int:team_id>/edit', methods=['POST'])
def edit_team(tournament_id, team_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    if not tournament.is_setup():
        flash('Cannot edit teams after fixtures are generated.', 'warning')
        return redirect(url_for('team.manage_teams', tournament_id=tournament_id))

    team     = _get_team_or_404(tournament_id, team_id)
    new_name = request.form.get('new_name', '').strip()

    if not new_name:
        flash('Team name cannot be empty.', 'danger')
    elif len(new_name) > 100:
        flash('Team name must be 100 characters or fewer.', 'danger')
    else:
        new_key   = _normalize(new_name)
        all_teams = Team.query.filter_by(tournament_id=tournament_id).all()
        existing  = [_normalize(t.name) for t in all_teams if t.id != team_id]
        if new_key in existing:
            flash(f'A team named "{new_name}" already exists.', 'danger')
        else:
            try:
                team.name     = new_name
                team.name_key = new_key
                db.session.commit()
                flash('Team name updated.', 'success')
            except Exception:
                db.session.rollback()
                flash('Could not update team name — it may already be taken.', 'danger')

    return redirect(url_for('team.manage_teams', tournament_id=tournament_id))


@team_bp.route('/tournament/<int:tournament_id>/teams/<int:team_id>/delete', methods=['POST'])
def delete_team(tournament_id, team_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    if not tournament.is_setup():
        flash('Cannot remove teams after fixtures are generated.', 'warning')
        return redirect(url_for('team.manage_teams', tournament_id=tournament_id))

    team = _get_team_or_404(tournament_id, team_id)
    name = team.name
    db.session.delete(team)
    db.session.commit()
    flash(f'Team "{name}" removed.', 'info')
    return redirect(url_for('team.manage_teams', tournament_id=tournament_id))
