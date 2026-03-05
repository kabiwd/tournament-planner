"""
models.py — Tournament Planner v6

Changes from v5:
- Team.name_key: normalised lowercase field; DB UniqueConstraint enforces
  case-insensitive uniqueness at the data layer (not just Python-side).
- Match.manual_locked: bool flag; when True the scheduler skips this match.
  Prevents re-scheduling from silently destroying manual edits.
- Tournament.name uniqueness removed (was already removed in v5 — kept clean).
- SPORT_WINNER_OUTCOMES extended; no dead fields.
- All dead imports removed.
"""

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


# ─────────────────────────────────────────────────────────────────────────────
# Sport / result-mode constants
# ─────────────────────────────────────────────────────────────────────────────
RESULT_MODE_SCORE  = 'score_based'
RESULT_MODE_WINNER = 'winner_only'

SPORT_RESULT_MODE = {
    'football':   RESULT_MODE_SCORE,
    'basketball': RESULT_MODE_SCORE,
    'cricket':    RESULT_MODE_WINNER,
    'esports':    RESULT_MODE_WINNER,
    'generic':    RESULT_MODE_SCORE,
}

# Valid outcome options per sport in winner_only mode.
# 'win'       — one team won
# 'tie'       — draw / super-over tie  (cricket only)
# 'no_result' — abandoned / rained off (cricket only)
SPORT_WINNER_OUTCOMES = {
    'cricket': ['win', 'tie', 'no_result'],
    'esports': ['win'],
}

VALID_SPORTS = ['football', 'cricket', 'basketball', 'esports', 'generic']


class Tournament(db.Model):
    __tablename__ = 'tournaments'

    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(100), nullable=False)
    format       = db.Column(db.String(20),  nullable=False)   # round_robin | knockout
    sport_type   = db.Column(db.String(20),  default='football')
    result_mode  = db.Column(db.String(20),  default=RESULT_MODE_SCORE)  # derived from sport
    status       = db.Column(db.String(20),  default='setup')            # setup | active | completed
    created_at   = db.Column(db.DateTime,    default=datetime.utcnow)
    current_round= db.Column(db.Integer,     default=1)

    # Point rules
    points_win   = db.Column(db.Integer, default=3)
    points_draw  = db.Column(db.Integer, default=1)
    points_loss  = db.Column(db.Integer, default=0)
    draws_allowed= db.Column(db.Boolean, default=True)

    # Scheduling config (round-robin only)
    sched_start_date = db.Column(db.Date,    nullable=True)
    sched_end_date   = db.Column(db.Date,    nullable=True)
    sched_min_rest   = db.Column(db.Integer, default=2)

    teams   = db.relationship('Team',  backref='tournament', lazy=True, cascade='all, delete-orphan')
    matches = db.relationship('Match', backref='tournament', lazy=True, cascade='all, delete-orphan')

    # ── Helpers ──────────────────────────────────────────────────────────────
    def is_setup(self):        return self.status == 'setup'
    def is_active(self):       return self.status == 'active'
    def is_completed(self):    return self.status == 'completed'
    def is_score_based(self):  return self.result_mode == RESULT_MODE_SCORE
    def is_winner_only(self):  return self.result_mode == RESULT_MODE_WINNER
    def team_count(self):      return len(self.teams)

    def allowed_outcomes(self):
        """Return valid outcome types for this sport's winner_only mode."""
        return SPORT_WINNER_OUTCOMES.get(self.sport_type, ['win'])

    def standings_config(self):
        """
        Display config for the standings page.
        All labels are honest — no fake NRR, no fake stats.
        """
        cfg = {
            'football': {
                'show_scores': True,
                'col_sf': 'GF', 'col_sa': 'GA', 'col_sd': 'GD',
                'col_sf_title': 'Goals For', 'col_sa_title': 'Goals Against',
                'col_sd_title': 'Goal Difference',
                'tiebreaker': 'score_diff',
            },
            'basketball': {
                'show_scores': True,
                'col_sf': 'PF', 'col_sa': 'PA', 'col_sd': 'Diff',
                'col_sf_title': 'Points For', 'col_sa_title': 'Points Against',
                'col_sd_title': 'Point Difference',
                'tiebreaker': 'score_diff',
            },
            # Cricket: winner_only. NRR intentionally not shown (no ball-by-ball data).
            'cricket': {
                'show_scores': False,
                'tiebreaker': 'wins',
            },
            'esports': {
                'show_scores': False,
                'tiebreaker': 'wins',
            },
            'generic': {
                'show_scores': True,
                'col_sf': 'SF',  'col_sa': 'SA', 'col_sd': 'SD',
                'col_sf_title': 'Score For', 'col_sa_title': 'Score Against',
                'col_sd_title': 'Score Difference',
                'tiebreaker': 'score_diff',
            },
        }
        return cfg.get(self.sport_type, cfg['generic'])

    def __repr__(self):
        return f'<Tournament {self.name}>'


class Team(db.Model):
    __tablename__ = 'teams'

    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    # Normalised (stripped, lowercased) version of name.
    # DB-level unique constraint enforces case-insensitive uniqueness per
    # tournament — reliable even if Python-side check is bypassed.
    name_key      = db.Column(db.String(100), nullable=False)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('tournament_id', 'name_key',
                            name='uq_team_name_key_per_tournament'),
    )

    def __repr__(self):
        return f'<Team {self.name}>'


class Match(db.Model):
    __tablename__ = 'matches'

    id            = db.Column(db.Integer,  primary_key=True)
    tournament_id = db.Column(db.Integer,  db.ForeignKey('tournaments.id'), nullable=False)
    round_number  = db.Column(db.Integer,  nullable=False)
    match_number  = db.Column(db.Integer,  nullable=False)

    team1_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    team2_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)

    # score_based: numeric scores
    team1_score = db.Column(db.Integer, nullable=True)
    team2_score = db.Column(db.Integer, nullable=True)

    winner_id   = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)

    # winner_only outcome: 'win' | 'tie' | 'no_result' | None (score_based)
    result_type = db.Column(db.String(20), nullable=True)

    # 'pending' | 'scheduled' | 'completed'
    status     = db.Column(db.String(20), default='pending')
    is_bye     = db.Column(db.Boolean,   default=False)

    # Scheduling (fully wired)
    scheduled_at = db.Column(db.DateTime, nullable=True)

    # If True, the auto-scheduler will not overwrite this match's scheduled_at.
    # Set automatically when an admin manually edits the schedule time.
    manual_locked = db.Column(db.Boolean, default=False)

    team1  = db.relationship('Team', foreign_keys=[team1_id])
    team2  = db.relationship('Team', foreign_keys=[team2_id])
    winner = db.relationship('Team', foreign_keys=[winner_id])

    def __repr__(self):
        return f'<Match R{self.round_number} M{self.match_number}>'

    def is_scheduled(self):
        return self.scheduled_at is not None and self.status != 'completed'


class StandingEntry(db.Model):
    __tablename__ = 'standings'

    id            = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    team_id       = db.Column(db.Integer, db.ForeignKey('teams.id'),       nullable=False)

    played       = db.Column(db.Integer, default=0)
    wins         = db.Column(db.Integer, default=0)
    draws        = db.Column(db.Integer, default=0)
    losses       = db.Column(db.Integer, default=0)
    no_results   = db.Column(db.Integer, default=0)
    score_for    = db.Column(db.Integer, default=0)
    score_against= db.Column(db.Integer, default=0)
    points       = db.Column(db.Integer, default=0)

    team = db.relationship('Team', foreign_keys=[team_id])

    @property
    def score_difference(self):
        return self.score_for - self.score_against

    def __repr__(self):
        return f'<Standing team={self.team_id} pts={self.points}>'
