# server.py
import os
import sqlite3
import csv
import io
import time
import requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, send_file
from flask_cors import CORS
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "change_this_secret")

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM")  # e.g., support@yourdomain.com
EMAIL_TO = os.getenv("EMAIL_TO")      # your email

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "12345")

DB_FILE = "portfolio.db"

app = Flask(__name__)
app.secret_key = SECRET_KEY

CORS(app, resources={r"/*": {"origins": "*"}})


# Add CORS headers
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ------------------ DATABASE SETUP ------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT,
            user_agent TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


init_db()


# ------------------ VISITOR LOGGER ------------------
@app.before_request
def log_visitor():
    ignore = {
        'static', 'dashboard', 'delete_message', 'delete_visit',
        'logout', 'send_email', 'export_messages',
        'delete_all_messages', 'delete_all_visits'
    }

    try:
        if request.endpoint not in ignore:
            ip = request.remote_addr
            ua = request.user_agent.string

            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("INSERT INTO visits (ip, user_agent) VALUES (?, ?)", (ip, ua))
            conn.commit()
            conn.close()
    except Exception as e:
        print("Visitor log error:", e)


# ------------------ EMAIL SENDER (RESEND) ------------------

@app.route("/send-email", methods=["OPTIONS"])
def send_email_options():
    return jsonify({"status": "ok"}), 200


@app.route("/send-email", methods=["POST"])
def send_email():
    try:
        data = request.get_json(force=True)
        name = data.get("name", "").strip()
        email = data.get("email", "").strip()
        message = data.get("message", "").strip()

        if not name or not email or not message:
            return jsonify({"status": "error", "message": "All fields required"}), 400

        # Store in DB
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO messages (name, email, message) VALUES (?, ?, ?)",
                  (name, email, message))
        conn.commit()
        conn.close()

        # RESEND headers
        headers = {
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json"
        }

        # --- EMAIL TO ADMIN ---
        admin_html = f"""
        <h2>New Portfolio Message</h2>
        <p><b>Name:</b> {name}</p>
        <p><b>Email:</b> {email}</p>
        <p><b>Message:</b><br>{message.replace('\n', '<br>')}</p>
        """

        payload_admin = {
            "from": EMAIL_FROM,
            "to": EMAIL_TO,
            "subject": f"New Message from {name}",
            "html": admin_html
        }

        r1 = requests.post("https://api.resend.com/emails", json=payload_admin, headers=headers)

        # --- AUTO REPLY TO VISITOR ---
        reply_html = f"""
        <p>Hi {name},</p>
        <p>Thanks for contacting me — I received your message and will respond soon.</p>
        <hr>
        <p><b>Your Message:</b><br>{message.replace('\n','<br>')}</p>
        <br>
        <p>— Veerakabilan</p>
        """

        payload_reply = {
            "from": EMAIL_FROM,
            "to": email,
            "subject": "I received your message ✔",
            "html": reply_html
        }

        r2 = requests.post("https://api.resend.com/emails", json=payload_reply, headers=headers)

        # Check status
        if r1.status_code != 200:
            return jsonify({"status": "error", "message": "Admin email failed"}), 500

        if r2.status_code != 200:
            return jsonify({"status": "error", "message": "Message sent, auto reply failed"}), 500

        return jsonify({"status": "success", "message": "Delivered successfully"}), 200

    except Exception as e:
        print("send_email error:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ------------------ DASHBOARD ------------------

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "admin_logged_in" not in session:
        if request.method == "POST":
            if request.form.get("username") == ADMIN_USER and request.form.get("password") == ADMIN_PASS:
                session["admin_logged_in"] = True
                return redirect(url_for("dashboard"))
            return render_template("dashboard.html", show_login=True, error="Invalid credentials")

        return render_template("dashboard.html", show_login=True)

    # Fetch data
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM visits")
    total_visits = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT ip) FROM visits")
    unique_visitors = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM messages")
    total_messages = c.fetchone()[0]

    c.execute("SELECT * FROM visits ORDER BY id DESC LIMIT 50")
    visits = c.fetchall()

    c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50")
    messages = c.fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        visits=visits,
        messages=messages,
        total_visits=total_visits,
        unique_visitors=unique_visitors,
        total_messages=total_messages,
        show_login=False
    )


# ------------------ DELETE FUNCTIONS ------------------

@app.route("/delete_message/<int:mid>")
def delete_message(mid):
    if "admin_logged_in" not in session:
        return redirect(url_for("dashboard"))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/delete_visit/<int:vid>")
def delete_visit(vid):
    if "admin_logged_in" not in session:
        return redirect(url_for("dashboard"))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM visits WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


# ------------------ ADMIN LOGOUT ------------------

@app.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("dashboard"))


# ------------------ HOME ------------------

@app.route("/")
def home():
    return "Backend Active", 200


# ------------------ RUN ------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
