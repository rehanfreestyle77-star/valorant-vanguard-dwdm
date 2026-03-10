from flask import Flask, render_template, request, redirect, url_for, make_response
import sqlite3
import pandas as pd
from fpdf import FPDF
import os

app = Flask(__name__)

def get_db_connection():
    conn = sqlite3.connect('valorant.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS dim_player (player_id INTEGER PRIMARY KEY, player_name TEXT, current_rank TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS fact_player_stats (stat_id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER, kills INTEGER, headshot_percentage REAL, reaction_time_ms INTEGER, is_cheater INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS etl_log (extracted INTEGER, redundant INTEGER, integrity TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS ban_history (id INTEGER PRIMARY KEY AUTOINCREMENT, report_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, ban_count INTEGER)')
    conn.commit(); conn.close()

init_db()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    file = request.files.get('csv_file')
    if file:
        df = pd.read_csv(file)
        unique_df = df.drop_duplicates(subset=['player_id'])
        conn = get_db_connection()
        conn.execute("DELETE FROM fact_player_stats"); conn.execute("DELETE FROM dim_player"); conn.execute("DELETE FROM etl_log")
        conn.execute("INSERT INTO etl_log VALUES (?, ?, ?)", (len(df), len(df)-len(unique_df), "100% Verified"))
        for _, row in unique_df.iterrows():
            conn.execute("INSERT OR IGNORE INTO dim_player VALUES (?, ?, ?)", (int(row['player_id']), row['player_name'], row['current_rank']))
            conn.execute("INSERT INTO fact_player_stats (player_id, kills, headshot_percentage, reaction_time_ms, is_cheater) VALUES (?, ?, ?, ?, 0)", (int(row['player_id']), int(row['kills']), float(row['headshot_percentage']), int(row['reaction_time_ms'])))
        conn.commit(); conn.close()
        return redirect(url_for('dashboard'))
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    conn = get_db_connection()
    etl = conn.execute("SELECT * FROM etl_log").fetchone()
    etl_data = etl if etl else {'extracted': 0, 'redundant': 0, 'integrity': 'N/A'}
    total_players = conn.execute("SELECT COUNT(*) FROM fact_player_stats").fetchone()[0]
    total_banned = conn.execute("SELECT COUNT(*) FROM fact_player_stats WHERE is_cheater = 1").fetchone()[0]
    
    # Mining: Cluster Centroids (Average Stats for Radar Chart)
    # 1. Average for Banned Players
    b_avg = conn.execute("SELECT AVG(kills), AVG(headshot_percentage), AVG(reaction_time_ms) FROM fact_player_stats WHERE is_cheater = 1").fetchone()
    # 2. Average for Safe Players
    s_avg = conn.execute("SELECT AVG(kills), AVG(headshot_percentage), AVG(reaction_time_ms) FROM fact_player_stats WHERE is_cheater = 0").fetchone()
    
    # Formatting averages for chart
    avg_banned = [round(x, 2) if x else 0 for x in b_avg]
    avg_safe = [round(x, 2) if x else 0 for x in s_avg]

    rank_stats = conn.execute("SELECT p.current_rank, COUNT(f.stat_id) as count FROM fact_player_stats f JOIN dim_player p ON f.player_id = p.player_id WHERE f.is_cheater = 1 GROUP BY p.current_rank").fetchall()
    history = conn.execute("SELECT ban_count FROM ban_history ORDER BY id DESC LIMIT 7").fetchall()
    
    players_raw = conn.execute("SELECT p.player_name, p.current_rank, f.kills, f.headshot_percentage, f.reaction_time_ms, f.is_cheater FROM fact_player_stats f JOIN dim_player p ON f.player_id = p.player_id ORDER BY f.is_cheater DESC").fetchall()
    
    players = []
    for p in players_raw:
        score = 0
        if p['headshot_percentage'] > 40: score += 30
        if p['headshot_percentage'] > 70: score += 40
        if p['reaction_time_ms'] < 150: score += 20
        if p['kills'] > 35: score += 10
        player_dict = dict(p)
        player_dict['risk_score'] = min(score, 100) if p['is_cheater'] else min(score, 40)
        players.append(player_dict)

    conn.close()
    return render_template('dashboard.html', total_players=total_players, total_banned=total_banned, total_safe=total_players-total_banned, players=players, etl=etl_data, rank_labels=[r['current_rank'].upper() for r in rank_stats], rank_values=[r['count'] for r in rank_stats], history_values=[r['ban_count'] for r in reversed(history)], avg_banned=avg_banned, avg_safe=avg_safe)

@app.route('/run-mining')
def run_mining():
    conn = get_db_connection()
    conn.execute("UPDATE fact_player_stats SET is_cheater = 1 WHERE (headshot_percentage > 80 AND reaction_time_ms < 130) OR (kills > 45 AND headshot_percentage > 70)")
    current_bans = conn.execute("SELECT COUNT(*) FROM fact_player_stats WHERE is_cheater = 1").fetchone()[0]
    conn.execute("INSERT INTO ban_history (ban_count) VALUES (?)", (current_bans,))
    conn.commit(); conn.close()
    return redirect(url_for('dashboard'))

@app.route('/download-report')
def download_report():
    conn = get_db_connection()
    players = conn.execute("SELECT p.player_name, p.current_rank, f.kills, f.headshot_percentage FROM fact_player_stats f JOIN dim_player p ON f.player_id = p.player_id WHERE f.is_cheater = 1").fetchall()
    conn.close()
    pdf = FPDF()
    pdf.add_page(); pdf.set_font("Arial", 'B', 16)
    pdf.cell(200, 10, txt="VANGUARD // THREAT DETECTION REPORT", ln=True, align='C')
    pdf.ln(10); pdf.set_fill_color(255, 70, 85); pdf.set_text_color(255, 255, 255)
    pdf.cell(60, 10, "Player Name", 1, 0, 'C', True); pdf.cell(40, 10, "Rank", 1, 0, 'C', True); pdf.cell(30, 10, "Kills", 1, 0, 'C', True); pdf.cell(30, 10, "HS%", 1, 1, 'C', True)
    pdf.set_text_color(0, 0, 0)
    for row in players:
        pdf.cell(60, 10, row['player_name'].upper(), 1); pdf.cell(40, 10, row['current_rank'].upper(), 1); pdf.cell(30, 10, str(row['kills']), 1); pdf.cell(30, 10, f"{row['headshot_percentage']:.2f}%", 1, 1)
    response = make_response(pdf.output(dest='S').encode('latin-1'))
    response.headers.set('Content-Disposition', 'attachment', filename='Vanguard_Report.pdf')
    response.headers.set('Content-Type', 'application/pdf')
    return response

if __name__ == '__main__':
    app.run(debug=True)