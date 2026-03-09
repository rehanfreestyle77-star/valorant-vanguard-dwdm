from flask import Flask, render_template, request, redirect, url_for, make_response
import sqlite3
import pandas as pd
from fpdf import FPDF
import os

app = Flask(__name__)

# 1. Database Connection (SQLite)
def get_db_connection():
    conn = sqlite3.connect('valorant.db')
    conn.row_factory = sqlite3.Row
    return conn

# 2. Initialize Tables
def init_db():
    conn = get_db_connection()
    conn.execute('''CREATE TABLE IF NOT EXISTS dim_player 
                    (player_id INTEGER PRIMARY KEY, player_name TEXT, current_rank TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS fact_player_stats 
                    (stat_id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER, kills INTEGER, 
                     headshot_percentage REAL, reaction_time_ms INTEGER, is_cheater INTEGER)''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    file = request.files.get('csv_file')
    if file:
        df = pd.read_csv(file)
        conn = get_db_connection()
        conn.execute("DELETE FROM fact_player_stats")
        conn.execute("DELETE FROM dim_player")
        
        for _, row in df.iterrows():
            conn.execute("INSERT OR IGNORE INTO dim_player VALUES (?, ?, ?)", 
                         (int(row['player_id']), row['player_name'], row['current_rank']))
            conn.execute("INSERT INTO fact_player_stats (player_id, kills, headshot_percentage, reaction_time_ms, is_cheater) VALUES (?, ?, ?, ?, 0)", 
                         (int(row['player_id']), int(row['kills']), float(row['headshot_percentage']), int(row['reaction_time_ms'])))
        conn.commit()
        conn.close()
        return redirect(url_for('dashboard'))
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    conn = get_db_connection()
    total_players = conn.execute("SELECT COUNT(*) FROM fact_player_stats").fetchone()[0]
    total_banned = conn.execute("SELECT COUNT(*) FROM fact_player_stats WHERE is_cheater = 1").fetchone()[0]
    total_safe = total_players - total_banned # Logic for Pie Chart
    
    players = conn.execute("""
        SELECT p.player_name, p.current_rank, f.kills, f.headshot_percentage, f.is_cheater 
        FROM fact_player_stats f 
        JOIN dim_player p ON f.player_id = p.player_id
        ORDER BY f.is_cheater DESC
    """).fetchall()
    conn.close()
    return render_template('dashboard.html', total_players=total_players, total_banned=total_banned, total_safe=total_safe, players=players)

@app.route('/run-mining')
def run_mining():
    conn = get_db_connection()
    # Data Mining Logic: Detection of Anomalies
    conn.execute("""
        UPDATE fact_player_stats 
        SET is_cheater = 1 
        WHERE (headshot_percentage > 80 AND reaction_time_ms < 130)
        OR (kills > 45 AND headshot_percentage > 70)
    """)
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard', mining='success'))

@app.route('/download-report')
def download_report():
    conn = get_db_connection()
    players = conn.execute("""
        SELECT p.player_name, p.current_rank, f.kills, f.headshot_percentage 
        FROM fact_player_stats f 
        JOIN dim_player p ON f.player_id = p.player_id
        WHERE f.is_cheater = 1
    """).fetchall()
    conn.close()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(200, 10, txt="VANGUARD // THREAT DETECTION REPORT", ln=True, align='C')
    pdf.ln(10)

    # Table Header
    pdf.set_fill_color(255, 70, 85) 
    pdf.set_text_color(255, 255, 255)
    pdf.cell(60, 10, "Player Name", 1, 0, 'C', True)
    pdf.cell(40, 10, "Rank", 1, 0, 'C', True)
    pdf.cell(30, 10, "Kills", 1, 0, 'C', True)
    pdf.cell(30, 10, "HS%", 1, 1, 'C', True)

    pdf.set_text_color(0, 0, 0)
    for row in players:
        pdf.cell(60, 10, row['player_name'].upper(), 1)
        pdf.cell(40, 10, row['current_rank'].upper(), 1)
        pdf.cell(30, 10, str(row['kills']), 1)
        pdf.cell(30, 10, f"{row['headshot_percentage']:.2f}%", 1, 1)

    response = make_response(pdf.output(dest='S').encode('latin-1'))
    response.headers.set('Content-Disposition', 'attachment', filename='Vanguard_Report.pdf')
    response.headers.set('Content-Type', 'application/pdf')
    return response

if __name__ == '__main__':
    app.run(debug=True)