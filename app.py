from flask import Flask, render_template, request, redirect, url_for, make_response, jsonify
import sqlite3
import pandas as pd
import io
import os
import json
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

app = Flask(__name__)

# ─────────────────────────────────────────────
#  DATA WAREHOUSE SCHEMA (Star Schema)
#  Fact: fact_player_stats
#  Dims: dim_player, dim_rank, dim_time, dim_detection_rule
# ─────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect('valorant_dw.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    # ── DIMENSION TABLES ──────────────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS dim_player (
        player_id    INTEGER PRIMARY KEY,
        player_name  TEXT NOT NULL,
        region       TEXT DEFAULT 'NA',
        account_age_days INTEGER DEFAULT 365,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS dim_rank (
        rank_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        rank_name    TEXT UNIQUE NOT NULL,
        rank_tier    INTEGER,         -- 1=Iron ... 9=Radiant
        rank_group   TEXT            -- Low / Mid / High
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS dim_time (
        time_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        full_date    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        day_of_week  TEXT,
        week_number  INTEGER,
        month        INTEGER,
        quarter      INTEGER,
        year         INTEGER
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS dim_detection_rule (
        rule_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_name    TEXT NOT NULL,
        rule_logic   TEXT,
        severity     TEXT   -- LOW / MEDIUM / HIGH / CRITICAL
    )''')

    # ── FACT TABLE ────────────────────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS fact_player_stats (
        stat_id              INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id            INTEGER REFERENCES dim_player(player_id),
        rank_id              INTEGER REFERENCES dim_rank(rank_id),
        time_id              INTEGER REFERENCES dim_time(time_id),
        rule_id              INTEGER REFERENCES dim_detection_rule(rule_id),
        kills                INTEGER,
        headshot_percentage  REAL,
        reaction_time_ms     INTEGER,
        matches_played       INTEGER DEFAULT 1,
        win_rate             REAL DEFAULT 50.0,
        is_cheater           INTEGER DEFAULT 0
    )''')

    # ── ETL AUDIT LOG ─────────────────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS etl_audit_log (
        log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_run    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        stage           TEXT,       -- EXTRACT / TRANSFORM / LOAD
        records_in      INTEGER,
        records_out     INTEGER,
        duplicates_removed INTEGER DEFAULT 0,
        nulls_filled    INTEGER DEFAULT 0,
        outliers_flagged INTEGER DEFAULT 0,
        status          TEXT DEFAULT 'SUCCESS',
        notes           TEXT
    )''')

    # ── BAN HISTORY (for trend chart) ─────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS ban_history (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        report_date  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ban_count    INTEGER,
        rule_applied TEXT
    )''')

    # ── SEED DETECTION RULES ──────────────────────────────────
    c.execute('''INSERT OR IGNORE INTO dim_detection_rule (rule_id, rule_name, rule_logic, severity)
                 VALUES
                 (1, 'AIMBOT_ALPHA',  'hs% > 80 AND reaction_ms < 130', 'CRITICAL'),
                 (2, 'AIMBOT_BETA',   'kills > 45 AND hs% > 70',        'HIGH'),
                 (3, 'SUSPICIOUS',    'hs% > 60 AND reaction_ms < 150', 'MEDIUM')''')

    # ── SEED RANK DIMENSION ───────────────────────────────────
    ranks = [
        (1,'Iron',1,'Low'),(2,'Bronze',2,'Low'),(3,'Silver',3,'Low'),
        (4,'Gold',4,'Mid'),(5,'Platinum',5,'Mid'),(6,'Diamond',6,'Mid'),
        (7,'Ascendant',7,'High'),(8,'Immortal',8,'High'),(9,'Radiant',9,'High')
    ]
    c.executemany('INSERT OR IGNORE INTO dim_rank VALUES (?,?,?,?)', ranks)

    conn.commit()
    conn.close()

init_db()


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _rank_id(rank_name, conn):
    row = conn.execute(
        "SELECT rank_id FROM dim_rank WHERE LOWER(rank_name) = LOWER(?)", (rank_name.strip(),)
    ).fetchone()
    if row:
        return row['rank_id']
    now = datetime.now()
    cur = conn.execute(
        "INSERT INTO dim_rank (rank_name, rank_tier, rank_group) VALUES (?,?,?)",
        (rank_name.strip(), 5, 'Mid')
    )
    return cur.lastrowid

def _time_id(conn):
    now = datetime.now()
    cur = conn.execute(
        "INSERT INTO dim_time (full_date, day_of_week, week_number, month, quarter, year) VALUES (?,?,?,?,?,?)",
        (now, now.strftime('%A'), now.isocalendar()[1], now.month, (now.month-1)//3+1, now.year)
    )
    return cur.lastrowid


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    file = request.files.get('csv_file')
    if not file:
        return redirect(url_for('index'))

    df = pd.read_csv(file)
    raw_count = len(df)

    # ── TRANSFORM STAGE ───────────────────────
    df.drop_duplicates(subset=['player_id'], inplace=True)
    nulls_filled = int(df.isnull().sum().sum())
    df.fillna({'kills': df['kills'].median(), 'headshot_percentage': 30.0, 'reaction_time_ms': 250}, inplace=True)
    deduped_count = len(df)
    duplicates = raw_count - deduped_count

    conn = get_db_connection()

    # Clear previous load
    for tbl in ['fact_player_stats', 'dim_player', 'etl_audit_log', 'ban_history']:
        conn.execute(f"DELETE FROM {tbl}")

    # ── ETL LOG: EXTRACT ──────────────────────
    conn.execute("""INSERT INTO etl_audit_log (stage, records_in, records_out, duplicates_removed, nulls_filled, notes)
                    VALUES ('EXTRACT', ?, ?, ?, ?, 'CSV ingestion complete')""",
                 (raw_count, raw_count, 0, nulls_filled))

    # ── ETL LOG: TRANSFORM ────────────────────
    conn.execute("""INSERT INTO etl_audit_log (stage, records_in, records_out, duplicates_removed, nulls_filled, notes)
                    VALUES ('TRANSFORM', ?, ?, ?, ?, 'Dedup + null-fill + type cast')""",
                 (raw_count, deduped_count, duplicates, nulls_filled))

    # ── LOAD INTO DW ──────────────────────────
    time_id = _time_id(conn)
    loaded = 0
    for _, row in df.iterrows():
        rid = _rank_id(row['current_rank'], conn)
        conn.execute("INSERT OR IGNORE INTO dim_player (player_id, player_name) VALUES (?,?)",
                     (int(row['player_id']), row['player_name']))
        conn.execute("""INSERT INTO fact_player_stats
                        (player_id, rank_id, time_id, kills, headshot_percentage, reaction_time_ms, is_cheater)
                        VALUES (?,?,?,?,?,?,0)""",
                     (int(row['player_id']), rid, time_id,
                      int(row['kills']), float(row['headshot_percentage']), int(row['reaction_time_ms'])))
        loaded += 1

    # ── ETL LOG: LOAD ─────────────────────────
    conn.execute("""INSERT INTO etl_audit_log (stage, records_in, records_out, notes)
                    VALUES ('LOAD', ?, ?, 'Star schema load complete')""",
                 (deduped_count, loaded))

    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
def dashboard():
    conn = get_db_connection()

    # ── CORE KPIs ─────────────────────────────
    total_players = conn.execute("SELECT COUNT(*) FROM fact_player_stats").fetchone()[0]
    total_banned  = conn.execute("SELECT COUNT(*) FROM fact_player_stats WHERE is_cheater = 1").fetchone()[0]

    # ── ETL PIPELINE STAGES ───────────────────
    etl_stages = conn.execute(
        "SELECT stage, records_in, records_out, duplicates_removed, nulls_filled, outliers_flagged, status, notes FROM etl_audit_log ORDER BY log_id"
    ).fetchall()
    etl_stages = [dict(e) for e in etl_stages]

    # ── CLUSTER CENTROIDS (Radar) ──────────────
    b_avg = conn.execute("SELECT AVG(kills), AVG(headshot_percentage), AVG(reaction_time_ms) FROM fact_player_stats WHERE is_cheater=1").fetchone()
    s_avg = conn.execute("SELECT AVG(kills), AVG(headshot_percentage), AVG(reaction_time_ms) FROM fact_player_stats WHERE is_cheater=0").fetchone()
    avg_banned = [round(x,2) if x else 0 for x in b_avg]
    avg_safe   = [round(x,2) if x else 0 for x in s_avg]

    # ── OLAP: Ban distribution by rank tier ───
    rank_stats = conn.execute("""
        SELECT dr.rank_name, dr.rank_group, COUNT(f.stat_id) as count
        FROM fact_player_stats f
        JOIN dim_rank dr ON f.rank_id = dr.rank_id
        WHERE f.is_cheater = 1
        GROUP BY dr.rank_name
        ORDER BY dr.rank_tier
    """).fetchall()

    # ── BAN HISTORY TREND ─────────────────────
    history = conn.execute("SELECT ban_count, rule_applied FROM ban_history ORDER BY id DESC LIMIT 10").fetchall()

    # ── RULE BREAKDOWN ────────────────────────
    rule_counts = conn.execute("""
        SELECT ddr.rule_name, ddr.severity, COUNT(f.stat_id) as hit_count
        FROM fact_player_stats f
        JOIN dim_detection_rule ddr ON f.rule_id = ddr.rule_id
        WHERE f.is_cheater = 1
        GROUP BY ddr.rule_id
    """).fetchall()
    rule_counts = [dict(r) for r in rule_counts]

    # ── FULL PLAYER LIST ──────────────────────
    players_raw = conn.execute("""
        SELECT p.player_name, dr.rank_name as current_rank, dr.rank_group,
               f.kills, f.headshot_percentage, f.reaction_time_ms, f.is_cheater,
               ddr.rule_name as triggered_rule, ddr.severity
        FROM fact_player_stats f
        JOIN dim_player p  ON f.player_id = p.player_id
        JOIN dim_rank dr   ON f.rank_id   = dr.rank_id
        LEFT JOIN dim_detection_rule ddr ON f.rule_id = ddr.rule_id
        ORDER BY f.is_cheater DESC, f.headshot_percentage DESC
    """).fetchall()

    players = []
    for p in players_raw:
        score = 0
        if p['headshot_percentage'] > 40: score += 30
        if p['headshot_percentage'] > 70: score += 40
        if p['reaction_time_ms'] < 150:   score += 20
        if p['kills'] > 35:               score += 10
        d = dict(p)
        d['risk_score'] = min(score, 100) if p['is_cheater'] else min(score, 40)
        players.append(d)

    # ── SCHEMA STATS (for DW Schema panel) ────
    schema_info = {
        'fact_rows':  total_players,
        'dim_player': conn.execute("SELECT COUNT(*) FROM dim_player").fetchone()[0],
        'dim_rank':   conn.execute("SELECT COUNT(*) FROM dim_rank").fetchone()[0],
        'dim_time':   conn.execute("SELECT COUNT(*) FROM dim_time").fetchone()[0],
        'dim_rule':   conn.execute("SELECT COUNT(*) FROM dim_detection_rule").fetchone()[0],
    }

    conn.close()

    return render_template('dashboard.html',
        total_players=total_players, total_banned=total_banned,
        total_safe=total_players - total_banned,
        players=players, etl_stages=etl_stages,
        rank_labels=[r['rank_name'].upper() for r in rank_stats],
        rank_values=[r['count'] for r in rank_stats],
        rank_groups=[r['rank_group'] for r in rank_stats],
        history_values=[r['ban_count'] for r in reversed(history)],
        avg_banned=avg_banned, avg_safe=avg_safe,
        rule_counts=rule_counts,
        schema_info=schema_info
    )


@app.route('/run-mining')
def run_mining():
    conn = get_db_connection()
    # Apply rule_id to each flagged record
    conn.execute("""UPDATE fact_player_stats SET is_cheater=1, rule_id=1
                    WHERE headshot_percentage > 80 AND reaction_time_ms < 130""")
    conn.execute("""UPDATE fact_player_stats SET is_cheater=1, rule_id=2
                    WHERE kills > 45 AND headshot_percentage > 70 AND (rule_id IS NULL OR rule_id != 1)""")
    current_bans = conn.execute("SELECT COUNT(*) FROM fact_player_stats WHERE is_cheater=1").fetchone()[0]
    conn.execute("INSERT INTO ban_history (ban_count, rule_applied) VALUES (?, 'MULTI-RULE')", (current_bans,))
    conn.commit(); conn.close()
    return redirect(url_for('dashboard'))


@app.route('/api/schema')
def api_schema():
    """Return DW schema as JSON for live schema explorer"""
    return jsonify({
        "warehouse": "valorant_dw",
        "schema_type": "Star Schema",
        "fact_table": {
            "name": "fact_player_stats",
            "columns": ["stat_id","player_id","rank_id","time_id","rule_id",
                        "kills","headshot_percentage","reaction_time_ms","matches_played","win_rate","is_cheater"],
            "foreign_keys": ["player_id → dim_player","rank_id → dim_rank",
                             "time_id → dim_time","rule_id → dim_detection_rule"]
        },
        "dimension_tables": [
            {"name":"dim_player",         "columns":["player_id","player_name","region","account_age_days"]},
            {"name":"dim_rank",           "columns":["rank_id","rank_name","rank_tier","rank_group"]},
            {"name":"dim_time",           "columns":["time_id","full_date","day_of_week","week_number","month","quarter","year"]},
            {"name":"dim_detection_rule", "columns":["rule_id","rule_name","rule_logic","severity"]}
        ]
    })


@app.route('/download-report')
def download_report():
    conn = get_db_connection()
    players = conn.execute("""
        SELECT p.player_name, dr.rank_name, f.kills, f.headshot_percentage,
               f.reaction_time_ms, ddr.rule_name, ddr.severity
        FROM fact_player_stats f
        JOIN dim_player p ON f.player_id = p.player_id
        JOIN dim_rank dr   ON f.rank_id   = dr.rank_id
        LEFT JOIN dim_detection_rule ddr ON f.rule_id = ddr.rule_id
        WHERE f.is_cheater = 1
    """).fetchall()
    etl_rows = conn.execute("SELECT * FROM etl_audit_log").fetchall()
    total_banned = len(players)
    total_players = conn.execute("SELECT COUNT(*) FROM fact_player_stats").fetchone()[0]
    conn.close()

    # ── Build PDF in memory with reportlab ────
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()

    RED   = colors.HexColor('#ff4655')
    CYAN  = colors.HexColor('#00ffd1')
    DARK  = colors.HexColor('#0b141d')
    PANEL = colors.HexColor('#0d1822')
    GRAY  = colors.HexColor('#1c2d3d')
    WHITE = colors.white

    title_style = ParagraphStyle('title', fontSize=20, textColor=RED,
                                  fontName='Helvetica-Bold', alignment=TA_CENTER, spaceAfter=4)
    sub_style   = ParagraphStyle('sub',   fontSize=9,  textColor=CYAN,
                                  fontName='Helvetica',    alignment=TA_CENTER, spaceAfter=14)
    section_style = ParagraphStyle('sec', fontSize=11, textColor=WHITE,
                                    fontName='Helvetica-Bold', backColor=DARK,
                                    leftIndent=4, spaceAfter=6, spaceBefore=14,
                                    borderPad=4)
    note_style  = ParagraphStyle('note', fontSize=8,  textColor=colors.HexColor('#4a6070'),
                                  fontName='Helvetica')

    story = []

    # ── HEADER ────────────────────────────────
    story.append(Paragraph("VANGUARD // THREAT DETECTION REPORT", title_style))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  "
        f"Threats: {total_banned} / {total_players}  |  Star Schema DW", sub_style))

    # ── KPI SUMMARY TABLE ─────────────────────
    ban_rate = f"{(total_banned/total_players*100):.1f}%" if total_players else "0%"
    kpi_data = [
        ['TOTAL SCANNED', 'THREATS FOUND', 'SECURE AGENTS', 'BAN RATE'],
        [str(total_players), str(total_banned), str(total_players - total_banned), ban_rate],
    ]
    kpi_table = Table(kpi_data, colWidths=[45*mm]*4)
    kpi_table.setStyle(TableStyle([
        ('BACKGROUND',   (0,0), (-1,0), DARK),
        ('TEXTCOLOR',    (0,0), (-1,0), CYAN),
        ('FONTNAME',     (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',     (0,0), (-1,0), 7),
        ('BACKGROUND',   (0,1), (-1,1), PANEL),
        ('TEXTCOLOR',    (0,1), (-1,1), WHITE),
        ('FONTNAME',     (0,1), (-1,1), 'Helvetica-Bold'),
        ('FONTSIZE',     (0,1), (-1,1), 18),
        ('ALIGN',        (0,0), (-1,-1), 'CENTER'),
        ('VALIGN',       (0,0), (-1,-1), 'MIDDLE'),
        ('ROWBACKGROUNDS',(0,0),(-1,-1), [DARK, PANEL]),
        ('GRID',         (0,0), (-1,-1), 0.5, GRAY),
        ('TOPPADDING',   (0,0), (-1,-1), 8),
        ('BOTTOMPADDING',(0,0), (-1,-1), 8),
    ]))
    story.append(kpi_table)

    # ── BANNED PLAYERS TABLE ──────────────────
    story.append(Paragraph("BANNED PLAYER ROSTER", section_style))

    header = ['PLAYER', 'RANK', 'KILLS', 'HS%', 'REACTION', 'RULE', 'SEVERITY']
    rows   = [header]
    for r in players:
        rows.append([
            (r['player_name'] or '').upper(),
            (r['rank_name']   or '').upper(),
            str(r['kills']),
            f"{r['headshot_percentage']:.1f}%",
            f"{r['reaction_time_ms']} ms",
            (r['rule_name']   or 'MANUAL').upper(),
            (r['severity']    or '—').upper(),
        ])

    if len(rows) == 1:
        rows.append(['No banned players found', '', '', '', '', '', ''])

    col_w = [42*mm, 26*mm, 16*mm, 16*mm, 22*mm, 32*mm, 22*mm]
    player_table = Table(rows, colWidths=col_w, repeatRows=1)

    ts = TableStyle([
        # Header
        ('BACKGROUND',   (0,0), (-1,0), RED),
        ('TEXTCOLOR',    (0,0), (-1,0), WHITE),
        ('FONTNAME',     (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',     (0,0), (-1,0), 8),
        # Body
        ('BACKGROUND',   (0,1), (-1,-1), PANEL),
        ('TEXTCOLOR',    (0,1), (-1,-1), WHITE),
        ('FONTNAME',     (0,1), (-1,-1), 'Helvetica'),
        ('FONTSIZE',     (0,1), (-1,-1), 8),
        ('ROWBACKGROUNDS',(0,1),(-1,-1), [PANEL, colors.HexColor('#111e2a')]),
        ('GRID',         (0,0), (-1,-1), 0.4, GRAY),
        ('ALIGN',        (0,0), (-1,-1), 'CENTER'),
        ('ALIGN',        (0,1), (0,-1),  'LEFT'),
        ('VALIGN',       (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',   (0,0), (-1,-1), 5),
        ('BOTTOMPADDING',(0,0), (-1,-1), 5),
        ('LEFTPADDING',  (0,0), (-1,-1), 6),
    ])
    # Highlight CRITICAL severity rows in red tint
    for i, r in enumerate(players, start=1):
        if (r['severity'] or '') == 'CRITICAL':
            ts.add('BACKGROUND', (0,i), (-1,i), colors.HexColor('#1f0a0d'))
    player_table.setStyle(ts)
    story.append(player_table)

    # ── ETL AUDIT TABLE ───────────────────────
    story.append(Paragraph("ETL PIPELINE AUDIT — DATA LINEAGE", section_style))

    etl_header = ['STAGE', 'RECORDS IN', 'RECORDS OUT', 'DUPES REMOVED', 'NULLS FILLED', 'STATUS', 'NOTES']
    etl_data   = [etl_header]
    stage_colors = {'EXTRACT': colors.HexColor('#3b2f00'), 'TRANSFORM': colors.HexColor('#1e1535'), 'LOAD': colors.HexColor('#002b24')}
    for e in etl_rows:
        etl_data.append([
            e['stage'], str(e['records_in']), str(e['records_out']),
            str(e['duplicates_removed'] or 0), str(e['nulls_filled'] or 0),
            e['status'] or 'SUCCESS', e['notes'] or ''
        ])

    if len(etl_data) == 1:
        etl_data.append(['No ETL runs recorded', '', '', '', '', '', ''])

    etl_col_w = [22*mm, 22*mm, 22*mm, 26*mm, 22*mm, 20*mm, 46*mm]
    etl_table = Table(etl_data, colWidths=etl_col_w, repeatRows=1)
    etl_ts = TableStyle([
        ('BACKGROUND',   (0,0), (-1,0), DARK),
        ('TEXTCOLOR',    (0,0), (-1,0), CYAN),
        ('FONTNAME',     (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',     (0,0), (-1,0), 7),
        ('BACKGROUND',   (0,1), (-1,-1), PANEL),
        ('TEXTCOLOR',    (0,1), (-1,-1), WHITE),
        ('FONTNAME',     (0,1), (-1,-1), 'Helvetica'),
        ('FONTSIZE',     (0,1), (-1,-1), 7),
        ('GRID',         (0,0), (-1,-1), 0.4, GRAY),
        ('ALIGN',        (0,0), (-1,-1), 'CENTER'),
        ('VALIGN',       (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',   (0,0), (-1,-1), 5),
        ('BOTTOMPADDING',(0,0), (-1,-1), 5),
        ('WORDWRAP',     (6,1), (6,-1), True),
    ])
    for i, e in enumerate(etl_rows, start=1):
        bg = stage_colors.get(e['stage'], PANEL)
        etl_ts.add('BACKGROUND', (0,i), (0,i), bg)
    etl_table.setStyle(etl_ts)
    story.append(etl_table)

    # ── FOOTER NOTE ───────────────────────────
    story.append(Spacer(1, 10*mm))
    story.append(Paragraph(
        "VANGUARD Anti-Cheat System  |  Star Schema Data Warehouse  |  "
        "Detection powered by multi-rule mining engine  |  CONFIDENTIAL",
        note_style))

    doc.build(story)
    buf.seek(0)

    response = make_response(buf.read())
    response.headers['Content-Disposition'] = 'attachment; filename=Vanguard_Report.pdf'
    response.headers['Content-Type'] = 'application/pdf'
    return response


if __name__ == '__main__':
    app.run(debug=True)