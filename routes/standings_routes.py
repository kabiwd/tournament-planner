"""
standings_routes.py — v6

New: /tournament/<id>/fixtures/pdf  —  PDF export for round-robin fixtures.
     Grouped by date if scheduled, by round if unscheduled.
     Uses reportlab (pure-Python, no wkhtmltopdf required).
"""

import io
from datetime import datetime

from flask import Blueprint, render_template, send_file
from models import db, Tournament, Match, StandingEntry, Team

standings_bp = Blueprint('standings', __name__)


def _sort_entries(entries, tournament):
    """
    Sort standings correctly per sport.

    score_diff tiebreaker: points → score_difference → score_for → name A-Z
    wins tiebreaker:       points → wins → name A-Z

    Two-pass stable sort ensures name fallback is always A-Z (ascending).
    """
    cfg = tournament.standings_config()

    # Pass 1: stable sort by name A-Z (lowest-priority tiebreaker)
    entries.sort(key=lambda e: e.team.name.lower())

    # Pass 2: sort by primary keys descending (stable preserves name order for ties)
    if cfg['tiebreaker'] == 'score_diff':
        entries.sort(
            key=lambda e: (e.points, e.score_difference, e.score_for),
            reverse=True,
        )
    else:
        entries.sort(
            key=lambda e: (e.points, e.wins),
            reverse=True,
        )

    return entries


@standings_bp.route('/tournament/<int:tournament_id>/standings')
def standings(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    entries = StandingEntry.query.filter_by(
        tournament_id=tournament_id
    ).join(Team).all()

    entries = _sort_entries(entries, tournament)
    cfg     = tournament.standings_config()

    return render_template(
        'standings.html',
        tournament=tournament,
        entries=entries,
        cfg=cfg,
    )


@standings_bp.route('/tournament/<int:tournament_id>/progress')
def progress(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    all_matches = Match.query.filter_by(
        tournament_id=tournament_id,
    ).order_by(Match.round_number, Match.match_number).all()

    completed_matches = [m for m in all_matches if m.status == 'completed' and not m.is_bye]
    pending_matches   = [m for m in all_matches if m.status in ('pending', 'scheduled')]

    champion = None
    if tournament.is_completed():
        if tournament.format == 'knockout':
            last = Match.query.filter_by(
                tournament_id=tournament_id,
                status='completed',
                is_bye=False,
            ).order_by(Match.round_number.desc()).first()
            if last and last.winner:
                champion = last.winner
        else:
            entries = StandingEntry.query.filter_by(
                tournament_id=tournament_id
            ).join(Team).all()
            entries = _sort_entries(entries, tournament)
            if entries:
                champion = entries[0].team

    rounds = {}
    for match in all_matches:
        rounds.setdefault(match.round_number, []).append(match)

    return render_template(
        'progress.html',
        tournament=tournament,
        rounds=rounds,
        completed_matches=completed_matches,
        pending_matches=pending_matches,
        champion=champion,
    )


@standings_bp.route('/tournament/<int:tournament_id>/schedule-view')
def schedule_view(tournament_id):
    """
    True chronological schedule view — grouped by date, ordered by time.
    Unscheduled matches appear at the bottom with a clear warning.
    """
    tournament = Tournament.query.get_or_404(tournament_id)

    all_matches = Match.query.filter_by(
        tournament_id=tournament_id,
        is_bye=False,
    ).order_by(Match.round_number, Match.match_number).all()

    scheduled = sorted(
        [m for m in all_matches if m.scheduled_at],
        key=lambda m: m.scheduled_at,
    )
    unscheduled = [
        m for m in all_matches
        if not m.scheduled_at and m.status != 'completed'
    ]

    # Build date-grouped structure for template
    from collections import OrderedDict
    date_groups = OrderedDict()
    for m in scheduled:
        d = m.scheduled_at.date()
        date_groups.setdefault(d, []).append(m)

    return render_template(
        'schedule_view.html',
        tournament=tournament,
        date_groups=date_groups,
        unscheduled=unscheduled,
        scheduled_count=len(scheduled),
    )


# ─────────────────────────────────────────────────────────────────────────────
# PDF Export — Round Robin fixtures
# ─────────────────────────────────────────────────────────────────────────────

@standings_bp.route('/tournament/<int:tournament_id>/fixtures/pdf')
def export_fixtures_pdf(tournament_id):
    """
    Generate and download a PDF of all fixtures for a round-robin tournament.

    Layout:
    - If scheduled: grouped by date in chronological order.
    - If some unscheduled: separate section at the end.
    - If nothing scheduled: grouped by round.

    Requires: reportlab (pip install reportlab)
    """
    tournament = Tournament.query.get_or_404(tournament_id)

    if tournament.format != 'round_robin':
        from flask import flash, redirect, url_for
        flash('PDF export is only available for Round Robin tournaments.', 'warning')
        return redirect(url_for('match.fixtures', tournament_id=tournament_id))

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
    except ImportError:
        from flask import flash, redirect, url_for
        flash('PDF generation requires reportlab. Run: pip install reportlab', 'danger')
        return redirect(url_for('match.fixtures', tournament_id=tournament_id))

    all_matches = Match.query.filter_by(
        tournament_id=tournament_id,
        is_bye=False,
    ).order_by(Match.round_number, Match.match_number).all()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'Title2', parent=styles['Heading1'],
        fontSize=20, spaceAfter=4, alignment=TA_CENTER,
    )
    subtitle_style = ParagraphStyle(
        'Subtitle', parent=styles['Normal'],
        fontSize=10, textColor=colors.HexColor('#555555'),
        spaceAfter=2, alignment=TA_CENTER,
    )
    section_style = ParagraphStyle(
        'Section', parent=styles['Heading2'],
        fontSize=12, spaceBefore=14, spaceAfter=6,
        textColor=colors.HexColor('#1a1a2e'),
    )
    normal = styles['Normal']

    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    story.append(Paragraph(tournament.name, title_style))
    story.append(Paragraph(
        f'{tournament.sport_type.title()} · Round Robin · '
        f'Exported {datetime.utcnow().strftime("%d %b %Y, %H:%M UTC")}',
        subtitle_style,
    ))

    # Scheduling summary if available
    if tournament.sched_start_date:
        sched_info = f'Schedule window: {tournament.sched_start_date}'
        if tournament.sched_end_date:
            sched_info += f' to {tournament.sched_end_date}'
        sched_info += (
            f' · Min rest: {tournament.sched_min_rest or 2} days '
            f'· Slots: Mon–Fri 7:30 PM · Sat–Sun 3:30 PM & 7:30 PM'
        )
        story.append(Paragraph(sched_info, subtitle_style))

    story.append(Spacer(1, 0.4*cm))
    story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#cccccc')))
    story.append(Spacer(1, 0.3*cm))

    scheduled   = sorted([m for m in all_matches if m.scheduled_at], key=lambda m: m.scheduled_at)
    unscheduled = [m for m in all_matches if not m.scheduled_at]

    def _match_row(m, show_date=False):
        """Build a table row for a single match."""
        t1 = m.team1.name if m.team1 else 'TBD'
        t2 = m.team2.name if m.team2 else 'TBD'
        rnd = f'R{m.round_number}'

        if m.status == 'completed':
            if m.result_type == 'tie':
                result = 'TIE'
            elif m.result_type == 'no_result':
                result = 'N/R'
            elif m.winner:
                result = f'{m.winner.name} won'
            elif m.team1_score is not None:
                result = f'{m.team1_score} – {m.team2_score}'
            else:
                result = 'Done'
        elif m.scheduled_at and not show_date:
            result = m.scheduled_at.strftime('%I:%M %p')
        else:
            result = 'Unscheduled'

        if show_date:
            return [rnd, t1, 'vs', t2, result]
        else:
            return [rnd, t1, 'vs', t2, result]

    col_widths = [1.2*cm, 5.5*cm, 0.8*cm, 5.5*cm, 2.8*cm]
    base_style = TableStyle([
        ('FONTNAME',    (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE',    (0,0), (-1,-1), 9),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.white, colors.HexColor('#f5f5f5')]),
        ('TEXTCOLOR',   (2,0), (2,-1), colors.HexColor('#999999')),  # 'vs' column
        ('TEXTCOLOR',   (0,0), (0,-1), colors.HexColor('#666666')),  # round tag
        ('ALIGN',       (0,0), (0,-1), 'CENTER'),
        ('ALIGN',       (2,0), (2,-1), 'CENTER'),
        ('ALIGN',       (4,0), (4,-1), 'RIGHT'),
        ('TOPPADDING',  (0,0), (-1,-1), 5),
        ('BOTTOMPADDING',(0,0),(-1,-1), 5),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING',(0,0), (-1,-1), 6),
    ])

    if scheduled:
        # Group by date
        from collections import OrderedDict
        date_groups = OrderedDict()
        for m in scheduled:
            date_groups.setdefault(m.scheduled_at.date(), []).append(m)

        for d, day_matches in date_groups.items():
            day_label = d.strftime('%A, %d %B %Y')
            story.append(Paragraph(day_label, section_style))
            rows = [_match_row(m) for m in day_matches]
            tbl  = Table(rows, colWidths=col_widths)
            tbl.setStyle(base_style)
            story.append(tbl)
            story.append(Spacer(1, 0.2*cm))

    elif all_matches:
        # No scheduling — group by round
        from collections import OrderedDict
        round_groups = OrderedDict()
        for m in all_matches:
            round_groups.setdefault(m.round_number, []).append(m)

        for rn, rnd_matches in round_groups.items():
            story.append(Paragraph(f'Round {rn}', section_style))
            rows = [_match_row(m) for m in rnd_matches]
            tbl  = Table(rows, colWidths=col_widths)
            tbl.setStyle(base_style)
            story.append(tbl)
            story.append(Spacer(1, 0.2*cm))

    if unscheduled:
        story.append(Spacer(1, 0.3*cm))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#dddddd')))
        story.append(Paragraph('Unscheduled Fixtures', section_style))
        story.append(Paragraph(
            'These matches have not yet been assigned a date and time.',
            ParagraphStyle('warn', parent=normal, textColor=colors.HexColor('#c0392b'), spaceAfter=6),
        ))
        rows = [_match_row(m) for m in unscheduled]
        tbl  = Table(rows, colWidths=col_widths)
        tbl.setStyle(base_style)
        story.append(tbl)

    doc.build(story)
    buf.seek(0)

    safe_name = ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in tournament.name)
    filename  = f'{safe_name}_fixtures.pdf'

    return send_file(
        buf,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename,
    )
