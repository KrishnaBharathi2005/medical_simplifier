from flask import Flask, render_template, request, redirect, url_for, session, send_file, jsonify
import sqlite3
import os
import io
import re
import platform
from functools import wraps
from datetime import datetime
import pytesseract
from pdf2image import convert_from_path
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from werkzeug.security import generate_password_hash, check_password_hash

# ================= APP CONFIG =================

app = Flask(__name__)
app.secret_key = "secure_medical_system"

UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ================= PRODUCTION OCR CONFIG =================

if platform.system() == "Windows":
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    POPPLER_PATH = r"C:\poppler-25.12.0\Library\bin"
else:
    pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"
    POPPLER_PATH = None

# ================= DATABASE INIT =================

def init_db():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT DEFAULT 'user'
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        risk TEXT,
        risk_score INTEGER,
        summary TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()

init_db()



# ================= AUTH DECORATORS =================

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            return "Access Denied"
        return f(*args, **kwargs)
    return wrapper

# ================= OCR CLEAN =================

def clean_ocr_text(text):
    text = re.sub(r'[^\x00-\x7F]+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

# ================= LAB EXTRACTION =================

def extract_lab_values(text):
    text = text.lower()

    patterns = {
        "heart_rate": r'(\d{2,3})\s*bpm',
        "blood_pressure": r'(\d{2,3})/(\d{2,3})',
        "hscrp": r'hscrp.*?(\d+\.?\d*)',
        "ldl": r'ldl.*?(\d+\.?\d*)',
        "glucose": r'glucose.*?(\d+\.?\d*)',
        "troponin": r'troponin.*?(\d+\.?\d*)'
    }

    results = {}

    hr = re.search(patterns["heart_rate"], text)
    results["heart_rate"] = int(hr.group(1)) if hr else 0

    bp = re.search(patterns["blood_pressure"], text)
    if bp:
        results["sys"] = int(bp.group(1))
        results["dia"] = int(bp.group(2))
    else:
        results["sys"] = 0
        results["dia"] = 0

    for key in ["hscrp", "ldl", "glucose", "troponin"]:
        match = re.search(patterns[key], text)
        results[key] = float(match.group(1)) if match else None

    return results

# ================= ADVANCED RISK ENGINE =================

def calculate_risk(data):
    score = 0

    if data["heart_rate"] and (data["heart_rate"] < 60 or data["heart_rate"] > 100):
        score += 2

    if data["sys"] >= 140 or data["dia"] >= 90:
        score += 3

    if data["hscrp"] and data["hscrp"] > 3:
        score += 3

    if data["ldl"] and data["ldl"] > 130:
        score += 2

    if data["troponin"] and data["troponin"] > 14:
        score += 4

    if score <= 2:
        risk = "LOW"
    elif score <= 6:
        risk = "MODERATE"
    else:
        risk = "HIGH"

    return risk, score

# ================= MEDICINE SUGGESTION =================

def suggest_medicine(data):
    suggestions = []

    if data["ldl"] and data["ldl"] > 130:
        suggestions.append("Consider Statins (Consult Doctor)")

    if data["glucose"] and data["glucose"] > 126:
        suggestions.append("Blood Sugar Control Monitoring")

    if data["hscrp"] and data["hscrp"] > 3:
        suggestions.append("Inflammation Management")

    if not suggestions:
        suggestions.append("Maintain Healthy Lifestyle")

    return suggestions

# ================= PASSWORD CHECK =================

def is_strong_password(password):
    return (
        len(password) >= 8 and
        re.search(r"[A-Z]", password) and
        re.search(r"[a-z]", password) and
        re.search(r"[0-9]", password) and
        re.search(r"[!@#$%^&*(),.?\":{}|<>]", password)
    )

# ================= AUTH ROUTES =================

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if not username or not password:
            return render_template("register.html", error="All fields required")

        if not is_strong_password(password):
            return render_template("register.html",
                                   error="Password must contain uppercase, lowercase, number and special character")

        hashed_password = generate_password_hash(password)

        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username=?", (username,))
        if c.fetchone():
            conn.close()
            return render_template("register.html", error="Username exists")

        c.execute("INSERT INTO users (username,password,role) VALUES (?,?,?)",
                  (username, hashed_password, "user"))
        conn.commit()
        conn.close()
        return redirect(url_for("login"))

    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username=?", (username,))
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            session["user_id"] = user[0]
            session["username"] = user[1]
            session["role"] = user[3]
            return redirect(url_for("dashboard"))

        return render_template("login.html", error="Invalid credentials")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ================= UPLOAD + ANALYSIS =================

@app.route("/", methods=["GET","POST"])
@login_required
def home():
    simplified = ""
    ai_text = ""
    risk = ""
    risk_score = 0
    suggestions = []

    if request.method == "POST":
        file = request.files["pdf_file"]
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], file.filename)
        file.save(filepath)

        if POPPLER_PATH:
            images = convert_from_path(filepath, poppler_path=POPPLER_PATH)
        else:
            images = convert_from_path(filepath)

        full_text = ""
        for img in images:
            full_text += pytesseract.image_to_string(img)

        text = clean_ocr_text(full_text)
        data = extract_lab_values(text)
        risk, risk_score = calculate_risk(data)
        suggestions = suggest_medicine(data)

        simplified = f"""
Heart Rate: {data['heart_rate']} bpm
Blood Pressure: {data['sys']}/{data['dia']} mmHg
Risk Level: {risk}
"""

        ai_text = f"""
Clinical Summary:

Risk Level: {risk}
Risk Score: {risk_score}

Recommended Actions:
{', '.join(suggestions)}
"""

        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("""
        INSERT INTO reports (user_id,name,risk,risk_score,summary,created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (session["user_id"], session["username"], risk,
         risk_score, simplified,
         datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()

    return render_template("index.html",
                           simplified=simplified,
                           ai_rewrite=ai_text,
                           risk=risk,
                           risk_score=risk_score)

# ================= DASHBOARD =================

@app.route("/dashboard")
@login_required
def dashboard():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT * FROM reports WHERE user_id=?", (session["user_id"],))
    reports = c.fetchall()
    conn.close()
    return render_template("dashboard.html", reports=reports)

# ================= DELETE REPORT =================

@app.route("/delete/<int:report_id>")
@login_required
def delete_report(report_id):
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("DELETE FROM reports WHERE id=? AND user_id=?",
              (report_id, session["user_id"]))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))

#===================  COMPARE =====================
@app.route("/compare")
@login_required
def compare():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("""
        SELECT risk_score, created_at
        FROM reports
        WHERE user_id=?
        ORDER BY created_at ASC
    """, (session["user_id"],))
    data = c.fetchall()
    conn.close()

    return render_template("compare.html", data=data)

# ================= API ENDPOINT =================

@app.route("/api/reports")
@login_required
def api_reports():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT id,risk,risk_score,created_at FROM reports WHERE user_id=?",
              (session["user_id"],))
    data = c.fetchall()
    conn.close()
    return jsonify(data)

# ================= RUN =================

if __name__ == "__main__":
    app.run(debug=True,use_reloader=False)