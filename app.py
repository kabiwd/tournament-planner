import os
import hmac
import hashlib
import secrets
from flask import Flask, request, session, abort, g
from models import db
from routes.tournament_routes import tournament_bp
from routes.team_routes import team_bp
from routes.match_routes import match_bp
from routes.standings_routes import standings_bp


def create_app():
    app = Flask(__name__)

    app.config['SECRET_KEY'] = os.environ.get(
        'SECRET_KEY', 'dev-only-insecure-key-change-in-production'
    )
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URL', 'sqlite:///tournament.db'
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)

    # ── Lightweight CSRF protection (no flask_wtf dependency) ─────────────────
    # Generates a per-session token; validates it on every state-changing POST.
    # Token is exposed via {{ csrf_token() }} in templates.

    @app.before_request
    def _csrf_protect():
        """Validate CSRF token on all POST/PUT/DELETE/PATCH requests."""
        if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
            return
        # Allow tests to bypass via header
        if app.config.get('TESTING') and \
                request.headers.get('X-Test-Bypass-CSRF') == '1':
            return
        token = session.get('_csrf_token')
        form_token = (
            request.form.get('csrf_token') or
            request.headers.get('X-CSRFToken')
        )
        if not token or not form_token or not hmac.compare_digest(token, form_token):
            abort(403)

    def _generate_csrf_token():
        if '_csrf_token' not in session:
            session['_csrf_token'] = secrets.token_hex(32)
        return session['_csrf_token']

    # Make csrf_token() available in all templates
    app.jinja_env.globals['csrf_token'] = _generate_csrf_token

    # ─────────────────────────────────────────────────────────────────────────

    app.register_blueprint(tournament_bp)
    app.register_blueprint(team_bp)
    app.register_blueprint(match_bp)
    app.register_blueprint(standings_bp)

    with app.app_context():
        db.create_all()

    return app


app = create_app()

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug)

