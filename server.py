# server.py
import os
import sqlite3
import csv
import io
import time
import requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, send_file
from dotenv import load_dotenv

# Load env
load_dotenv()

EMAIL_USER = os.getenv("EMAIL_USER")             # your email (used as recipient/from label)
RESEND_API_KEY = os.getenv("RESEND_API_KEY")     # required: set in Render env
SECRET_KEY = os.getenv("SECRET_KEY", "change_this_secret")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "12345")

DB_FILE = "portfolio.db"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Simple CORS headers for GitHub Pages -> Render
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

# ====== Database Setup ======
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT,
            user_agent TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# Initialize DB on startup (important for Render)
init_db()

# ====== Visitor Logger ======
@app.before_request
def log_visitor():
    ignore = {
        'static', 'dashboard', 'delete_message', 'delete_visit',
        'logout', 'send_email', 'export_messages',
        'delete_all_messages', 'delete_all_visits'
    }
    try:
        if request.endpoint not in ignore:
            ip = request.remote_addr or request.environ.get('HTTP_X_FORWARDED_FOR', '')
            ua = request.user_agent.string or ""
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("INSERT INTO visits (ip, user_agent) VALUES (?, ?)", (ip, ua))
            conn.commit()
            conn.close()
    except Exception as e:
        print("Visitor logging failed:", e)

# ====== Resend helper ======
def send_email_using_resend(to_email: str, subject: str, html_body: str):
    """
    Send email via Resend API.
    Returns (status_code:int, response_text:str)
    """
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY not configured in environment.")
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "from": f"Veerakabilan Portfolio <onboarding@resend.dev>",
        "to": to_email,
        "subject": subject,
        "html": html_body
    }
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    return r.status_code, r.text

# ====== Contact Form API ======
@app.route('/send-email', methods=['OPTIONS'])
def send_email_options():
    return jsonify({"status": "ok"}), 200

@app.route('/send-email', methods=['POST'])
def send_email():
    try:
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip()
        message = (data.get("message") or "").strip()

        if not name or not email or not message:
            return jsonify({"status": "error", "message": "Missing fields (name, email, message required)"}), 400

        # Save to DB
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO messages (name, email, message) VALUES (?, ?, ?)", (name, email, message))
        conn.commit()
        conn.close()

        # Prepare HTML email to site owner
        html_body = f"""
        <html><body>
          <h3>New message from your portfolio</h3>
          <p><b>Name:</b> {name}</p>
          <p><b>Email:</b> {email}</p>
          <p><b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
          <hr>
          <p><b>Message:</b><br>{message.replace('\n', '<br>')}</p>
          <hr>
          <p style="color:gray;">Sent from your portfolio site</p>
        </body></html>
        """

        # Send owner notification via Resend
        try:
            status, text = send_email_using_resend(
                EMAIL_USER,
                f"New Portfolio Message from {name}",
                html_body
            )
            if not (200 <= status < 300):
                print("Resend owner email failed:", status, text)
        except Exception as ex:
            print("Owner email send failed:", ex)

        # Auto-reply to visitor (non-fatal)
        try:
            reply_html = f"""
            <html><body>
              <p>Hi {name},</p>
              <p>Thanks for reaching out — I received your message and will reply soon.</p>
              <p><b>Your message:</b><br>{message.replace('\n','<br>')}</p>
              <br>
              <p>— Veerakabilan</p>
            </body></html>
            """
            try:
                r_status, r_text = send_email_using_resend(email, "Thanks — I've received your message", reply_html)
                if not (200 <= r_status < 300):
                    print("Resend reply failed:", r_status, r_text)
            except Exception as exr:
                print("Auto-reply failed:", exr)
        except Exception as ex_reply:
            print("Auto-reply (outer) failed:", ex_reply)

        return jsonify({"status": "success", "message": "Message stored; email attempts made."})

    except Exception as e:
        print("send_email error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500

# ====== Admin Dashboard ======
@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard():
    # Login flow
    if 'admin_logged_in' not in session:
        if request.method == 'POST':
            username = request.form.get('username', '')
            password = request.form.get('password', '')
            if username == ADMIN_USER and password == ADMIN_PASS:
                session['admin_logged_in'] = True
                return redirect(url_for('dashboard'))
            else:
                return render_template('dashboard.html', show_login=True, error="Invalid credentials")

        return render_template('dashboard.html', show_login=True)

    # Analytics and data for charts
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Totals
    c.execute("SELECT COUNT(*) FROM visits")
    total_visits = c.fetchone()[0] or 0

    c.execute("SELECT COUNT(DISTINCT ip) FROM visits")
    unique_visitors = c.fetchone()[0] or 0

    c.execute("SELECT COUNT(*) FROM messages")
    total_messages = c.fetchone()[0] or 0

    # recent rows
    c.execute("SELECT * FROM visits ORDER BY id DESC LIMIT 50")
    visits = c.fetchall()

    c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50")
    messages = c.fetchall()

    # Visits per day (last 30 days)
    c.execute("""
        SELECT DATE(timestamp) as day, COUNT(*) FROM visits
        WHERE timestamp >= date('now','-30 days')
        GROUP BY DATE(timestamp)
        ORDER BY DATE(timestamp)
    """)
    visit_data = c.fetchall()
    visits_labels = [row[0] for row in visit_data]
    visits_values = [row[1] for row in visit_data]

    # Messages per day (last 30 days)
    c.execute("""
        SELECT DATE(timestamp) as day, COUNT(*) FROM messages
        WHERE timestamp >= date('now','-30 days')
        GROUP BY DATE(timestamp)
        ORDER BY DATE(timestamp)
    """)
    msg_data = c.fetchall()
    messages_labels = [row[0] for row in msg_data]
    messages_values = [row[1] for row in msg_data]

    conn.close()

    return render_template(
        'dashboard.html',
        visits=visits,
        messages=messages,
        total_visits=total_visits,
        unique_visitors=unique_visitors,
        total_messages=total_messages,
        visits_labels=visits_labels,
        visits_values=visits_values,
        messages_labels=messages_labels,
        messages_values=messages_values,
        show_login=False
    )

# ====== Delete Single / Bulk ======
@app.route('/delete_message/<int:mid>')
def delete_message(mid):
    if 'admin_logged_in' not in session:
        return redirect(url_for('dashboard'))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE id = ?", (mid,))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/delete_visit/<int:vid>')
def delete_visit(vid):
    if 'admin_logged_in' not in session():
        return redirect(url_for('dashboard'))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM visits WHERE id = ?", (vid,))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/delete_all_messages')
def delete_all_messages():
    if 'admin_logged_in' not in session:
        return redirect(url_for('dashboard'))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM messages")
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/delete_all_visits')
def delete_all_visits():
    if 'admin_logged_in' not in session:
        return redirect(url_for('dashboard'))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM visits")
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

# ====== Export Messages to CSV ======
@app.route('/export_messages')
def export_messages():
    if 'admin_logged_in' not in session:
        return redirect(url_for('dashboard'))

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, email, message, timestamp FROM messages ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()

    # create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Email", "Message", "Timestamp"])
    for r in rows:
        writer.writerow(r)
    output.seek(0)

    mem = io.BytesIO()
    mem.write(output.getvalue().encode('utf-8'))
    mem.seek(0)

    filename = f"messages_{int(time.time())}.csv"
    return send_file(mem, as_attachment=True, download_name=filename, mimetype='text/csv')

# ====== Logout ======
@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('dashboard'))

# ====== Home (optional) ======
@app.route('/')
def home():
    return "Backend Active", 200

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
