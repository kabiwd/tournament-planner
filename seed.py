"""
seed.py — Creates 4 sample tournaments.
Run: python seed.py
"""
from app import app
from models import db, Tournament, Team, Match, StandingEntry
from routes.tournament_routes import _generate_round_robin, _generate_knockout, _init_standings
from routes.match_routes import _update_standings


def seed():
    with app.app_context():
        db.drop_all()
        db.create_all()

        # 1. Football league — score_based, round_robin
        t1 = Tournament(
            name="Premier League Demo",
            format="round_robin", sport_type="football",
            result_mode="score_based", status="active",
            points_win=3, points_draw=1, points_loss=0, draws_allowed=True,
        )
        db.session.add(t1); db.session.flush()
        teams1 = _add_teams(t1, ["Arsenal FC", "Chelsea FC", "Liverpool FC",
                                  "Manchester City", "Tottenham", "Everton FC"])
        _generate_round_robin(t1, teams1)
        _init_standings(t1, teams1)
        db.session.commit()
        _sim_score_results(t1, 1, [(3, 1), (2, 2), (1, 0)])
        print("✅ Premier League Demo (Football RR, 6 teams, R1 played)")

        # 2. Cricket — winner_only, round_robin
        t2 = Tournament(
            name="IPL Style League",
            format="round_robin", sport_type="cricket",
            result_mode="winner_only", status="active",
            points_win=2, points_draw=1, points_loss=0, draws_allowed=True,
        )
        db.session.add(t2); db.session.flush()
        teams2 = _add_teams(t2, ["Mumbai Indians", "Chennai Super Kings",
                                  "Kolkata Knight Riders", "Royal Challengers"])
        _generate_round_robin(t2, teams2)
        _init_standings(t2, teams2)
        db.session.commit()
        _sim_winner_only_results(t2, 1, [(0, 'win'), (1, 'tie')])
        print("✅ IPL Style League (Cricket RR, 4 teams, R1 played)")

        # 3. Esports — winner_only, knockout (5 teams = odd — tests bye handling)
        t3 = Tournament(
            name="Valorant Champions Cup",
            format="knockout", sport_type="esports",
            result_mode="winner_only", status="active",
            points_win=1, points_draw=0, points_loss=0, draws_allowed=False,
        )
        db.session.add(t3); db.session.flush()
        teams3 = _add_teams(t3, ["Team Liquid", "Cloud9", "Fnatic", "NaVi", "Sentinels"])
        _generate_knockout(t3, teams3)
        db.session.commit()
        print("✅ Valorant Champions Cup (Esports KO, 5 teams — odd, 1 bye)")

        # 4. Basketball — score_based, setup (no fixtures yet)
        t4 = Tournament(
            name="NBA Shootout",
            format="round_robin", sport_type="basketball",
            result_mode="score_based", status="setup",
            points_win=3, points_draw=0, points_loss=0, draws_allowed=False,
        )
        db.session.add(t4); db.session.flush()
        _add_teams(t4, ["Lakers", "Celtics", "Bulls", "Warriors"])
        db.session.commit()
        print("✅ NBA Shootout (Basketball RR, setup, 4 teams)")

        print("\n🎉 Seed complete! Run: python app.py → http://localhost:5000")


def _add_teams(tournament, names):
    teams = []
    for name in names:
        t = Team(name=name, name_key=name.strip().lower(), tournament_id=tournament.id)
        db.session.add(t)
        teams.append(t)
    db.session.flush()
    return teams


def _sim_score_results(tournament, round_num, results):
    matches = Match.query.filter_by(
        tournament_id=tournament.id, round_number=round_num, is_bye=False
    ).order_by(Match.match_number).all()
    for match, (s1, s2) in zip(matches, results):
        match.team1_score = s1; match.team2_score = s2
        match.result_type = None; match.status = 'completed'
        match.winner_id = (
            match.team1_id if s1 > s2 else
            match.team2_id if s2 > s1 else None
        )
        db.session.flush()
        _update_standings(tournament, match)
    db.session.commit()


def _sim_winner_only_results(tournament, round_num, results):
    matches = Match.query.filter_by(
        tournament_id=tournament.id, round_number=round_num, is_bye=False
    ).order_by(Match.match_number).all()
    for match, (winner_idx, result_type) in zip(matches, results):
        match.team1_score = None; match.team2_score = None
        match.result_type = result_type; match.status = 'completed'
        match.winner_id = (
            match.team1_id if result_type == 'win' and winner_idx == 0 else
            match.team2_id if result_type == 'win' and winner_idx == 1 else None
        )
        db.session.flush()
        _update_standings(tournament, match)
    db.session.commit()


if __name__ == '__main__':
    seed()
