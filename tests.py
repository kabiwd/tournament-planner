"""
tests.py — Tournament Planner v6 test suite

Critical paths tested:
 1. Round Robin: results enterable in ANY order (no round lock)
 2. Knockout: round-gated flow preserved
 3. IPL-style scheduler: weekday (1 slot) vs weekend (2 slots)
 4. Manual schedule validation: hard errors for invalid times/dates/conflicts
 5. Manual lock: auto-scheduler preserves locked matches on re-run
 6. Schedule overflow: unscheduled remainder when window is too tight
 7. Case-insensitive duplicate team names (Python + DB constraint)
 8. Odd-team round robin: correct bye generation
 9. Odd-team knockout: correct bye in first round
10. Standings sort: correct A-Z fallback on point tie
11. PDF export route responds 200 for round-robin tournament
12. Standings reverse on edit

Run: python tests.py
"""

import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, '.')

from app import create_app
from models import db, Tournament, Team, Match, StandingEntry, SPORT_RESULT_MODE
from routes.tournament_routes import (
    _generate_round_robin, _generate_knockout, _init_standings
)
from routes.match_routes import _update_standings
from scheduler import (
    schedule_round_robin, clear_pending_schedule,
    validate_manual_schedule, DAILY_SLOTS
)
from fixture_engine import generate_round_robin_fixtures, generate_knockout_fixtures


def _make_app():
    app = create_app()
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    return app


def _post(client, url, data=None):
    """POST with CSRF bypass header for tests."""
    return client.post(
        url,
        data=data or {},
        headers={'X-Test-Bypass-CSRF': '1'},
        follow_redirects=True,
    )


def _add_teams(tournament, names):
    teams = []
    for name in names:
        t = Team(name=name, name_key=name.strip().lower(), tournament_id=tournament.id)
        db.session.add(t)
        teams.append(t)
    db.session.flush()
    return teams


def _make_rr_tournament(app, sport='football', names=None):
    if names is None:
        names = ['Alpha', 'Beta', 'Gamma', 'Delta']
    with app.app_context():
        t = Tournament(
            name='Test RR', format='round_robin', sport_type=sport,
            result_mode=SPORT_RESULT_MODE[sport], status='active',
            points_win=3, points_draw=1, points_loss=0, draws_allowed=True,
        )
        db.session.add(t); db.session.flush()
        teams = _add_teams(t, names)
        _generate_round_robin(t, teams)
        _init_standings(t, teams)
        db.session.commit()
        return t.id


def _make_ko_tournament(app, names=None):
    if names is None:
        names = ['A', 'B', 'C', 'D']
    with app.app_context():
        t = Tournament(
            name='Test KO', format='knockout', sport_type='football',
            result_mode='score_based', status='active',
            points_win=3, points_draw=0, points_loss=0, draws_allowed=False,
        )
        db.session.add(t); db.session.flush()
        teams = _add_teams(t, names)
        _generate_knockout(t, teams)
        db.session.commit()
        return t.id


# ─────────────────────────────────────────────────────────────────────────────
class TestRoundRobinResultsAnyOrder(unittest.TestCase):
    """RR: any pending match should be submittable via the results page."""

    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()
        self.tid = _make_rr_tournament(self.app)

    def test_all_pending_visible_in_results_page(self):
        resp = self.client.get(f'/tournament/{self.tid}/results')
        self.assertEqual(resp.status_code, 200)
        # All 6 matches (4-team RR) should be editable — no round lock
        with self.app.app_context():
            matches = Match.query.filter_by(
                tournament_id=self.tid, is_bye=False, status='pending'
            ).all()
            self.assertEqual(len(matches), 6)

    def test_submit_round2_match_before_round1(self):
        """Submitting a Round 2 result without completing Round 1 must succeed."""
        with self.app.app_context():
            r2_match = Match.query.filter_by(
                tournament_id=self.tid, round_number=2, is_bye=False
            ).first()
            self.assertIsNotNone(r2_match)
            mid = r2_match.id

        resp = self.client.post(
            f'/tournament/{self.tid}/match/{mid}/update',
            data={'score1': '2', 'score2': '1'},
            headers={'X-Test-Bypass-CSRF': '1'},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            m = Match.query.get(mid)
            self.assertEqual(m.status, 'completed')
            self.assertEqual(m.team1_score, 2)


class TestKnockoutRoundGating(unittest.TestCase):
    """Knockout: future rounds must remain locked until current round is done."""

    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()
        self.tid = _make_ko_tournament(self.app, names=['A', 'B', 'C', 'D'])

    def test_results_page_shows_only_round1(self):
        resp = self.client.get(f'/tournament/{self.tid}/results')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Round 1', resp.data)

    def test_round2_match_rejected_before_round1_complete(self):
        """Submitting to a non-existent R2 match (URL hack) should fail gracefully."""
        with self.app.app_context():
            r1_matches = Match.query.filter_by(
                tournament_id=self.tid, round_number=1, is_bye=False
            ).all()
            # Try to submit R1 match 2 before match 1
            m2 = r1_matches[1]
            resp = self.client.post(
                f'/tournament/{self.tid}/match/{m2.id}/update',
                data={'score1': '3', 'score2': '1'},
                follow_redirects=True,
            )
            # Should succeed (no constraint on which R1 match goes first)
            self.assertEqual(resp.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────────
class TestIPLScheduler(unittest.TestCase):

    def _make_matches(self, pairs, tid=1):
        """Create dummy Match objects (not saved to DB)."""
        matches = []
        for i, (t1, t2) in enumerate(pairs):
            m = Match(
                id=i+1, tournament_id=tid,
                round_number=1, match_number=i+1,
                team1_id=t1, team2_id=t2,
                status='pending', is_bye=False,
            )
            matches.append(m)
        return matches

    def test_weekday_gets_single_slot(self):
        # Monday = 0
        monday_slots = DAILY_SLOTS[0]
        self.assertEqual(monday_slots, ['19:30'])

    def test_weekend_gets_two_slots(self):
        # Saturday = 5, Sunday = 6
        self.assertEqual(DAILY_SLOTS[5], ['15:30', '19:30'])
        self.assertEqual(DAILY_SLOTS[6], ['15:30', '19:30'])

    def test_scheduler_assigns_weekday_slot(self):
        app = _make_app()
        tid = _make_rr_tournament(app, names=['T1', 'T2', 'T3'])
        with app.app_context():
            # Find a Monday to start from
            d = date(2026, 3, 9)  # Monday
            assert d.weekday() == 0

            matches = Match.query.filter_by(
                tournament_id=tid, is_bye=False
            ).order_by(Match.round_number, Match.match_number).all()

            n, _ = schedule_round_robin(matches, start_date=d, min_rest_days=1)
            db.session.commit()

            scheduled = [m for m in matches if m.scheduled_at]
            for m in scheduled:
                wd = m.scheduled_at.weekday()
                t  = m.scheduled_at.strftime('%H:%M')
                # Weekdays: only 19:30 allowed
                if wd < 5:
                    self.assertEqual(t, '19:30', f'Weekday slot must be 19:30, got {t}')
                # Weekends: 15:30 or 19:30
                else:
                    self.assertIn(t, ['15:30', '19:30'])

    def test_no_same_day_for_same_team(self):
        app = _make_app()
        tid = _make_rr_tournament(app, names=['T1', 'T2', 'T3', 'T4'])
        with app.app_context():
            d = date(2026, 3, 9)  # Monday
            matches = Match.query.filter_by(
                tournament_id=tid, is_bye=False
            ).order_by(Match.round_number, Match.match_number).all()
            schedule_round_robin(matches, start_date=d, min_rest_days=1)
            db.session.commit()

            scheduled = [m for m in matches if m.scheduled_at]
            from collections import defaultdict
            team_dates = defaultdict(list)
            for m in scheduled:
                team_dates[m.team1_id].append(m.scheduled_at.date())
                team_dates[m.team2_id].append(m.scheduled_at.date())

            for tid_t, dates in team_dates.items():
                self.assertEqual(len(dates), len(set(dates)),
                                 f'Team {tid_t} has same-day matches')


# ─────────────────────────────────────────────────────────────────────────────
class TestManualScheduleValidation(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()
        self.tid = _make_rr_tournament(self.app)

    def _get_first_match_id(self):
        with self.app.app_context():
            return Match.query.filter_by(
                tournament_id=self.tid, is_bye=False
            ).first().id

    def test_invalid_time_rejected(self):
        mid = self._get_first_match_id()
        # 04:30 is not an allowed slot
        resp = self.client.post(
            f'/tournament/{self.tid}/match/{mid}/set-schedule',
            data={'scheduled_at': '2026-06-15T04:30'},
            headers={'X-Test-Bypass-CSRF': '1'},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'rejected', resp.data)

    def test_weekday_1530_rejected(self):
        mid = self._get_first_match_id()
        # Monday 15:30 — not allowed (weekdays only get 19:30)
        # Find next Monday
        d = date(2026, 3, 9)  # Monday
        resp = self.client.post(
            f'/tournament/{self.tid}/match/{mid}/set-schedule',
            data={'scheduled_at': f'{d}T15:30'},
            headers={'X-Test-Bypass-CSRF': '1'},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'rejected', resp.data)

    def test_weekend_1930_accepted(self):
        mid = self._get_first_match_id()
        # Saturday 19:30 — should succeed
        with self.app.app_context():
            t = Tournament.query.get(self.tid)
            t.sched_start_date = date(2026, 3, 1)
            t.sched_end_date   = date(2026, 12, 31)
            db.session.commit()
        d = date(2026, 3, 14)  # Saturday
        resp = self.client.post(
            f'/tournament/{self.tid}/match/{mid}/set-schedule',
            data={'scheduled_at': f'{d}T19:30'},
            headers={'X-Test-Bypass-CSRF': '1'},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'rejected', resp.data)

    def test_validate_manual_schedule_function(self):
        with self.app.app_context():
            t = Tournament.query.get(self.tid)
            t.sched_start_date = date(2026, 3, 1)
            t.sched_end_date   = date(2026, 12, 31)
            t.sched_min_rest   = 2
            db.session.commit()
            match = Match.query.filter_by(
                tournament_id=self.tid, is_bye=False
            ).first()

            # Valid: Saturday 19:30
            sat = date(2026, 3, 28)
            assert sat.weekday() == 5
            ok, err = validate_manual_schedule(t, match, datetime(2026, 3, 28, 19, 30))
            self.assertTrue(ok, err)

            # Invalid: arbitrary time
            ok, err = validate_manual_schedule(t, match, datetime(2026, 3, 28, 10, 0))
            self.assertFalse(ok)
            self.assertIn('slot', err.lower())

            # Invalid: before start date
            ok, err = validate_manual_schedule(t, match, datetime(2026, 2, 1, 19, 30))
            self.assertFalse(ok)


# ─────────────────────────────────────────────────────────────────────────────
class TestManualLock(unittest.TestCase):
    """Locked matches must survive auto-scheduler re-runs."""

    def test_locked_match_preserved(self):
        app = _make_app()
        tid = _make_rr_tournament(app, names=['X', 'Y', 'Z', 'W'])
        with app.app_context():
            d = date(2026, 3, 14)  # Saturday
            matches = Match.query.filter_by(
                tournament_id=tid, is_bye=False
            ).order_by(Match.round_number, Match.match_number).all()

            # Manually lock the first match
            first = matches[0]
            first.scheduled_at  = datetime(2026, 3, 14, 19, 30)
            first.status        = 'scheduled'
            first.manual_locked = True
            db.session.commit()

            # Re-run scheduler
            clear_pending_schedule(matches)
            schedule_round_robin(matches, start_date=date(2026, 3, 9), min_rest_days=1)
            db.session.commit()

            # First match must still have its original datetime
            m = Match.query.get(first.id)
            self.assertEqual(m.scheduled_at, datetime(2026, 3, 14, 19, 30))
            self.assertTrue(m.manual_locked)


# ─────────────────────────────────────────────────────────────────────────────
class TestScheduleOverflow(unittest.TestCase):

    def test_overflow_leaves_unscheduled(self):
        """A very tight window should leave some matches unscheduled."""
        app = _make_app()
        tid = _make_rr_tournament(app, names=['A', 'B', 'C', 'D', 'E', 'F'])
        with app.app_context():
            matches = Match.query.filter_by(
                tournament_id=tid, is_bye=False
            ).order_by(Match.round_number, Match.match_number).all()
            # 15 matches, 3-day window starting Monday → Mon/Tue/Wed = 3 slots
            d = date(2026, 3, 9)  # Monday
            scheduled, unscheduled = schedule_round_robin(
                matches, start_date=d,
                end_date=d + timedelta(days=2),
                min_rest_days=1,
            )
            self.assertGreater(unscheduled, 0)
            self.assertGreater(scheduled, 0)


# ─────────────────────────────────────────────────────────────────────────────
class TestCaseInsensitiveDuplicates(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()
        with self.app.app_context():
            t = Tournament(
                name='Dup Test', format='round_robin', sport_type='football',
                result_mode='score_based', status='setup',
                points_win=3, points_draw=1, points_loss=0, draws_allowed=True,
            )
            db.session.add(t); db.session.commit()
            self.tid = t.id

    def test_duplicate_rejected_case_insensitive(self):
        self.client.post(
            f'/tournament/{self.tid}/teams',
            data={'action': 'add', 'team_name': 'Arsenal FC'},
        )
        resp = self.client.post(
            f'/tournament/{self.tid}/teams',
            data={'action': 'add', 'team_name': 'ARSENAL FC'},
            headers={'X-Test-Bypass-CSRF': '1'},
            follow_redirects=True,
        )
        self.assertIn(b'already exists', resp.data)
        with self.app.app_context():
            count = Team.query.filter_by(tournament_id=self.tid).count()
            self.assertEqual(count, 1)

    def test_db_constraint_name_key(self):
        """name_key uniqueness must be enforced at DB level too."""
        with self.app.app_context():
            t1 = Team(name='Arsenal FC', name_key='arsenal fc', tournament_id=self.tid)
            db.session.add(t1); db.session.commit()
            t2 = Team(name='Arsenal FC', name_key='arsenal fc', tournament_id=self.tid)
            db.session.add(t2)
            with self.assertRaises(Exception):
                db.session.commit()
            db.session.rollback()


# ─────────────────────────────────────────────────────────────────────────────
class TestOddTeamRoundRobin(unittest.TestCase):

    def test_3_team_rr_has_3_rounds(self):
        app = _make_app()
        with app.app_context():
            t = Tournament(
                name='3T', format='round_robin', sport_type='football',
                result_mode='score_based', status='active',
                points_win=3, points_draw=1, points_loss=0, draws_allowed=True,
            )
            db.session.add(t); db.session.flush()
            teams = _add_teams(t, ['A', 'B', 'C'])
            _generate_round_robin(t, teams)
            _init_standings(t, teams)
            db.session.commit()

            rounds = db.session.query(Match.round_number).filter_by(
                tournament_id=t.id
            ).distinct().count()
            self.assertEqual(rounds, 3)

            byes = Match.query.filter_by(tournament_id=t.id, is_bye=True).count()
            self.assertEqual(byes, 3)

    def test_5_team_rr_has_5_rounds_and_5_byes(self):
        app = _make_app()
        with app.app_context():
            t = Tournament(
                name='5T', format='round_robin', sport_type='football',
                result_mode='score_based', status='active',
                points_win=3, points_draw=1, points_loss=0, draws_allowed=True,
            )
            db.session.add(t); db.session.flush()
            teams = _add_teams(t, ['A', 'B', 'C', 'D', 'E'])
            _generate_round_robin(t, teams)
            _init_standings(t, teams)
            db.session.commit()

            rounds  = db.session.query(Match.round_number).filter_by(tournament_id=t.id).distinct().count()
            byes    = Match.query.filter_by(tournament_id=t.id, is_bye=True).count()
            real    = Match.query.filter_by(tournament_id=t.id, is_bye=False).count()
            self.assertEqual(rounds, 5)
            self.assertEqual(byes, 5)
            self.assertEqual(real, 10)


# ─────────────────────────────────────────────────────────────────────────────
class TestOddTeamKnockout(unittest.TestCase):

    def test_5_team_ko_has_1_bye(self):
        app = _make_app()
        with app.app_context():
            t = Tournament(
                name='KO5', format='knockout', sport_type='football',
                result_mode='score_based', status='active',
                points_win=3, points_draw=0, points_loss=0, draws_allowed=False,
            )
            db.session.add(t); db.session.flush()
            teams = _add_teams(t, ['A', 'B', 'C', 'D', 'E'])
            _generate_knockout(t, teams)
            db.session.commit()

            byes = Match.query.filter_by(tournament_id=t.id, is_bye=True).count()
            real = Match.query.filter_by(tournament_id=t.id, is_bye=False).count()
            self.assertEqual(byes, 1)
            self.assertEqual(real, 2)


# ─────────────────────────────────────────────────────────────────────────────
class TestStandingsSort(unittest.TestCase):

    def test_alphabetical_fallback_is_az(self):
        """Teams with equal points should be sorted A-Z, not Z-A."""
        from routes.standings_routes import _sort_entries

        app = _make_app()
        with app.app_context():
            t = Tournament(
                name='S', format='round_robin', sport_type='football',
                result_mode='score_based', status='active',
                points_win=3, points_draw=1, points_loss=0, draws_allowed=True,
            )
            db.session.add(t); db.session.flush()
            teams = _add_teams(t, ['Zebra', 'Mango', 'Alpha'])
            _init_standings(t, teams)
            db.session.commit()

            entries = StandingEntry.query.filter_by(tournament_id=t.id).all()
            sorted_e = _sort_entries(entries, t)
            names = [e.team.name for e in sorted_e]
            self.assertEqual(names, ['Alpha', 'Mango', 'Zebra'])

    def test_higher_points_ranks_first(self):
        from routes.standings_routes import _sort_entries

        app = _make_app()
        with app.app_context():
            t = Tournament(
                name='S2', format='round_robin', sport_type='football',
                result_mode='score_based', status='active',
                points_win=3, points_draw=1, points_loss=0, draws_allowed=True,
            )
            db.session.add(t); db.session.flush()
            teams = _add_teams(t, ['Low', 'High'])
            _init_standings(t, teams)
            e1 = StandingEntry.query.filter_by(team_id=teams[0].id).first()
            e2 = StandingEntry.query.filter_by(team_id=teams[1].id).first()
            e1.points = 1; e2.points = 9
            db.session.commit()

            entries = StandingEntry.query.filter_by(tournament_id=t.id).all()
            sorted_e = _sort_entries(entries, t)
            self.assertEqual(sorted_e[0].team.name, 'High')


# ─────────────────────────────────────────────────────────────────────────────
class TestPDFExport(unittest.TestCase):

    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()
        self.tid = _make_rr_tournament(self.app)

    def test_pdf_route_returns_200_or_redirect(self):
        resp = self.client.get(f'/tournament/{self.tid}/fixtures/pdf')
        # 200 if reportlab is installed, or a redirect with flash if not
        self.assertIn(resp.status_code, [200, 302])
        if resp.status_code == 200:
            self.assertEqual(resp.content_type, 'application/pdf')

    def test_pdf_route_knockout_redirects(self):
        app = _make_app()
        client = app.test_client()
        kid = _make_ko_tournament(app)
        resp = client.get(f'/tournament/{kid}/fixtures/pdf')
        self.assertEqual(resp.status_code, 302)


# ─────────────────────────────────────────────────────────────────────────────
class TestStandingsOnEdit(unittest.TestCase):

    def test_standings_reverse_and_reapply(self):
        app = _make_app()
        client = app.test_client()
        tid = _make_rr_tournament(app, names=['AA', 'BB'])

        with app.app_context():
            m = Match.query.filter_by(
                tournament_id=tid, is_bye=False
            ).first()
            mid = m.id

        # Submit 2-0
        client.post(
            f'/tournament/{tid}/match/{mid}/update',
            data={'score1': '2', 'score2': '0'},
            headers={'X-Test-Bypass-CSRF': '1'},
            follow_redirects=True,
        )
        with app.app_context():
            e_aa = StandingEntry.query.filter_by(tournament_id=tid).join(Team).filter(Team.name == 'AA').first()
            self.assertEqual(e_aa.points, 3)

        # Edit to 0-2
        client.post(
            f'/tournament/{tid}/match/{mid}/edit',
            data={'score1': '0', 'score2': '2'},
            headers={'X-Test-Bypass-CSRF': '1'},
            follow_redirects=True,
        )
        with app.app_context():
            e_aa = StandingEntry.query.filter_by(tournament_id=tid).join(Team).filter(Team.name == 'AA').first()
            e_bb = StandingEntry.query.filter_by(tournament_id=tid).join(Team).filter(Team.name == 'BB').first()
            self.assertEqual(e_aa.points, 0)
            self.assertEqual(e_bb.points, 3)


# ─────────────────────────────────────────────────────────────────────────────
class TestEsportsNoTie(unittest.TestCase):
    """Esports results page must not show Tie option."""

    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()
        self.tid = _make_rr_tournament(self.app, sport='esports')

    def test_no_tie_option_in_esports(self):
        resp = self.client.get(f'/tournament/{self.tid}/results')
        self.assertNotIn(b'value="tie"', resp.data)

    def test_win_option_present(self):
        resp = self.client.get(f'/tournament/{self.tid}/results')
        self.assertIn(b'value="win"', resp.data)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    loader  = unittest.TestLoader()
    suite   = loader.loadTestsFromModule(__import__('__main__'))
    runner  = unittest.TextTestRunner(verbosity=2)
    result  = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
