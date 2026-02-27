from flask import Flask, render_template, request, redirect, url_for, session, send_file
import sqlite3
import os
import io
import re
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

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

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
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("role") != "admin":
            return "Access Denied"
        return f(*args, **kwargs)
    return decorated_function

# ================= OCR CLEANING =================

def clean_ocr_text(text):
    text = re.sub(r'[^\x00-\x7F]+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

# ================= DATA EXTRACTION =================

def extract_patient_info(text):
    data = {"name": "Unknown", "heart_rate": 0, "sys": 0, "dia": 0}

    name_match = re.search(r'PREPARED FOR\s+([A-Z][a-z]+\s[A-Z][a-z]+)', text)
    if name_match:
        data["name"] = name_match.group(1)
    else:
        dob_match = re.search(r'([A-Z][a-z]+\s[A-Z][a-z]+)\s+\d{1,2}/\d{1,2}/\d{4}', text)
        if dob_match:
            data["name"] = dob_match.group(1)

    hr_match = re.search(r'(\d{2,3})\s*bpm', text, re.IGNORECASE)
    if hr_match:
        hr = int(hr_match.group(1))
        if 30 < hr < 200:
            data["heart_rate"] = hr

    bp_matches = re.findall(r'(\d{2,3})/(\d{2,3})', text)
    for sys, dia in bp_matches:
        sys, dia = int(sys), int(dia)
        if 70 <= sys <= 200 and 40 <= dia <= 130:
            data["sys"] = sys
            data["dia"] = dia
            break

    return data

# ================= RISK ENGINE =================

def calculate_risk(data):
    score = 0

    if data["heart_rate"] < 60 or data["heart_rate"] > 100:
        score += 2

    if data["sys"] >= 140 or data["dia"] >= 90:
        score += 4
    elif data["sys"] >= 120 or data["dia"] >= 80:
        score += 2

    if score <= 2:
        risk = "LOW"
    elif score <= 6:
        risk = "MODERATE"
    else:
        risk = "HIGH"

    return risk, score

# ================= AI REWRITE =================

def generate_ai_rewrite(data, risk):
    return f"""
Clinical Interpretation:

The patient's heart rate is {data['heart_rate']} bpm.
Blood pressure recorded at {data['sys']}/{data['dia']} mmHg.

Based on calculated indicators, overall health risk is {risk}.

Recommendation:
Maintain routine monitoring and consult healthcare provider if needed.
"""

# ================= AUTH ROUTES =================
def is_strong_password(password):
    if len(password) < 8:
        return False

    if not re.search(r"[A-Z]", password):
        return False

    if not re.search(r"[a-z]", password):
        return False

    if not re.search(r"[0-9]", password):
        return False

    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        return False

    return True
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if not username or not password:
            return render_template("register.html", error="All fields are required")

        if not is_strong_password(password):
            return render_template("register.html",
                                   error="Password must contain uppercase, lowercase, number and special character")

        hashed_password = generate_password_hash(password)

        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username=?", (username,))
        existing_user = c.fetchone()

        if existing_user:
            conn.close()
            return render_template("register.html", error="Username already exists")

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

        return render_template("login.html", error="Invalid username or password")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ================= HOME =================

@app.route("/", methods=["GET","POST"])
@login_required
def home():
    simplified = ""
    ai_rewrite = ""
    risk = ""
    risk_score = 0
    report_id = None

    if request.method == "POST":
        file = request.files["pdf_file"]
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], file.filename)
        file.save(filepath)

        images = convert_from_path(
            filepath,
            poppler_path=r"C:\poppler-25.12.0\Library\bin"
        )

        text = ""
        for img in images:
            text += pytesseract.image_to_string(img)

        text = clean_ocr_text(text)
        data = extract_patient_info(text)
        risk, risk_score = calculate_risk(data)

        simplified = f"""
Patient Name: {data['name']}
Heart Rate: {data['heart_rate']} bpm
Blood Pressure: {data['sys']}/{data['dia']} mmHg
Risk Level: {risk}
"""

        ai_rewrite = generate_ai_rewrite(data, risk)

        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("""
        INSERT INTO reports
        (user_id,name,risk,risk_score,summary,created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (session["user_id"], data["name"], risk,
         risk_score, simplified,
         datetime.now().strftime("%Y-%m-%d %H:%M")))
        report_id = c.lastrowid
        conn.commit()
        conn.close()

    return render_template("index.html",
                           simplified=simplified,
                           ai_rewrite=ai_rewrite,
                           risk=risk,
                           risk_score=risk_score,
                           report_id=report_id)

# ================= DASHBOARD =================

@app.route("/dashboard")
@login_required
def dashboard():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT * FROM reports WHERE user_id=?",
              (session["user_id"],))
    reports = c.fetchall()
    conn.close()
    return render_template("dashboard.html", reports=reports)

# ================= COMPARE =================

@app.route("/compare")
@login_required
def compare():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT risk_score, created_at FROM reports WHERE user_id=?",
              (session["user_id"],))
    data = c.fetchall()
    conn.close()
    return render_template("compare.html", data=data)

# ================= ADMIN =================

@app.route("/admin")
@admin_required
def admin_panel():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT id, username, role FROM users")
    users = c.fetchall()
    c.execute("SELECT COUNT(*) FROM reports")
    report_count = c.fetchone()[0]
    conn.close()
    return render_template("admin.html",
                           users=users,
                           report_count=report_count)

# ================= PDF DOWNLOAD =================

@app.route("/download/<int:report_id>")
@login_required
def download(report_id):
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT summary FROM reports WHERE id=?",
              (report_id,))
    result = c.fetchone()
    conn.close()

    if not result:
        return "Report not found"

    content = result[0]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer)
    styles = getSampleStyleSheet()
    elements = []

    for line in content.split("\n"):
        elements.append(Paragraph(line, styles["Normal"]))
        elements.append(Spacer(1, 0.2 * inch))

    doc.build(elements)
    buffer.seek(0)

    return send_file(buffer,
                     as_attachment=True,
                     download_name="Medical_Report.pdf",
                     mimetype="application/pdf")

# ================= RUN =================

if __name__ == "__main__":
    app.run()