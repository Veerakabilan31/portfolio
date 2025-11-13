# server.py
import os
import sqlite3
import smtplib
import csv
import io
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, send_file, abort
from flask_cors import CORS
from dotenv import load_dotenv

# Load env
load_dotenv()

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
SECRET_KEY = os.getenv("SECRET_KEY", "change_this_secret")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "12345")

DB_FILE = "portfolio.db"

app = Flask(__name__)
app.secret_key = SECRET_KEY
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

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
    # endpoints to ignore (so admin actions / API calls don't spam visits)
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
        owner_msg = MIMEMultipart("alternative")
        owner_msg["Subject"] = f"New Portfolio Message from {name}"
        owner_msg["From"] = EMAIL_USER
        owner_msg["To"] = EMAIL_USER

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
        owner_msg.attach(MIMEText(html_body, "html"))

        # Send email via Gmail TLS (more reliable from localhost)
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, EMAIL_USER, owner_msg.as_string())

            # Auto-reply to visitor
            try:
                reply = MIMEMultipart("alternative")
                reply["Subject"] = "Thanks — I've received your message"
                reply["From"] = EMAIL_USER
                reply["To"] = email

                reply_html = f"""
                <html><body>
                  <p>Hi {name},</p>
                  <p>Thanks for reaching out — I received your message and will reply soon.</p>
                  <p><b>Your message:</b><br>{message.replace('\n','<br>')}</p>
                  <br>
                  <p>— Veerakabilan</p>
                </body></html>
                """
                reply.attach(MIMEText(reply_html, "html"))
                server.sendmail(EMAIL_USER, email, reply.as_string())
            except Exception as ex_reply:
                # Non-fatal: auto-reply failed (maybe recipient blocked)
                print("Auto-reply failed:", ex_reply)

        return jsonify({"status": "success", "message": "Message stored and email sent."})

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
    if 'admin_logged_in' not in session:
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

