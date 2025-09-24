import streamlit as st
import sqlite3, os, datetime
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from passlib.hash import pbkdf2_sha256
import random
from streamlit_autorefresh import st_autorefresh
import streamlit.components.v1 as components
from streamlit_option_menu import option_menu
import psutil

# Optional WMI import (Windows only)
try:
    import wmi
except ImportError:
    wmi = None

# ==============================
# CONFIG & DB INIT
# ==============================
st.set_page_config(page_title="Indoor Air Wellness", layout="wide")
DB_DIR = "data"
DB_PATH = os.path.join(DB_DIR, "readings.db")
os.makedirs(DB_DIR, exist_ok=True)
REFRESH_INTERVAL = 5

IMG_DIR = "images"
os.makedirs(IMG_DIR, exist_ok=True)

def img_path(filename):
    return os.path.join(IMG_DIR, filename)

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE,
            email TEXT UNIQUE,
            password_hash TEXT,
            created_at TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            timestamp TEXT,
            temperature REAL,
            humidity REAL,
            co2 REAL,
            pm25 REAL,
            pm10 REAL,
            tvoc REAL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    conn.commit()
    return conn

conn = init_db()

# ==============================
# AUTH HELPERS
# ==============================
def create_user(username, email, password):
    hashed = pbkdf2_sha256.hash(password)
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO users (username, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (username, email, hashed, datetime.datetime.utcnow().isoformat())
        )
        conn.commit()
        return True, "User created"
    except sqlite3.IntegrityError as e:
        return False, str(e)

def verify_user(login, password):
    c = conn.cursor()
    c.execute(
        "SELECT id, password_hash, username FROM users WHERE username=? OR email=?",
        (login, login)
    )
    row = c.fetchone()
    if not row:
        return None
    user_id, pw_hash, username = row
    if pbkdf2_sha256.verify(password, pw_hash):
        return {"id": user_id, "username": username}
    return None

def get_user_by_id(user_id):
    c = conn.cursor()
    c.execute("SELECT id, username, email, created_at FROM users WHERE id=?", (user_id,))
    r = c.fetchone()
    if r:
        return {"id": r[0], "username": r[1], "email": r[2], "created_at": r[3]}
    return None

def change_password(user_id, new_password):
    hashed = pbkdf2_sha256.hash(new_password)
    c = conn.cursor()
    c.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, user_id))
    conn.commit()
    return True

# ==============================
# READING HELPERS
# ==============================
def add_reading(user_id, temperature, humidity, co2, pm25, pm10, tvoc, timestamp=None):
    timestamp = timestamp or datetime.datetime.utcnow().isoformat()
    c = conn.cursor()
    c.execute(
        "INSERT INTO readings (user_id, timestamp, temperature, humidity, co2, pm25, pm10, tvoc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, timestamp, temperature, humidity, co2, pm25, pm10, tvoc)
    )
    conn.commit()

def get_readings(user_id, limit=1000):
    c = conn.cursor()
    c.execute(
        "SELECT timestamp, temperature, humidity, co2, pm25, pm10, tvoc FROM readings WHERE user_id=? ORDER BY timestamp DESC LIMIT ?",
        (user_id, limit)
    )
    rows = c.fetchall()
    cols = ["timestamp","temperature","humidity","co2","pm25","pm10","tvoc"]
    return pd.DataFrame(rows, columns=cols)

def get_latest_reading(user_id):
    df = get_readings(user_id, limit=1)
    if df.empty:
        return None
    return df.iloc[0].to_dict()

# ==============================
# AQI HELPERS
# ==============================
def pm25_to_aqi(pm):
    if pm is None:
        return None
    pm = float(pm)
    bps = [
        (0.0, 12.0, 0, 50),
        (12.1, 35.4, 51, 100),
        (35.5, 55.4, 101, 150),
        (55.5, 150.4, 151, 200),
        (150.5, 250.4, 201, 300),
        (250.5, 350.4, 301, 400),
        (350.5, 500.4, 401, 500),
    ]
    for (Clow, Chigh, Ilow, Ihigh) in bps:
        if Clow <= pm <= Chigh:
            return int(round(((Ihigh - Ilow)/(Chigh - Clow))*(pm - Clow) + Ilow))
    return 500

def aqi_category(aqi):
    if aqi is None: return ("Unknown", "#9AA0A6")
    if aqi <= 50: return ("Good", "#00E400")
    if aqi <= 100: return ("Moderate", "#FFFF00")
    if aqi <= 150: return ("Unhealthy for Sensitive Groups", "#FF7E00")
    if aqi <= 200: return ("Unhealthy", "#FF0000")
    if aqi <= 300: return ("Very Unhealthy", "#8F3F97")
    return ("Hazardous", "#7E0023")

def health_tip(cat):
    tips = {
        "Good": "Air quality is good. Keep windows open when possible.",
        "Moderate": "Sensitive groups should limit outdoor activity.",
        "Unhealthy for Sensitive Groups": "Use air purifier and avoid outdoor activity.",
        "Unhealthy": "Limit outdoor exposure.",
        "Very Unhealthy": "Stay indoors and use air purifier.",
        "Hazardous": "Avoid outdoor activities completely."
    }
    return tips.get(cat, "Monitor conditions and stay safe.")

# ==============================
# LAPTOP TEMPERATURE
# ==============================
def get_laptop_temperature():
    if wmi:
        try:
            w = wmi.WMI(namespace="root\\OpenHardwareMonitor")
            sensors = w.Sensor()
            temps = [s.Value for s in sensors if s.SensorType=='Temperature' and ('cpu' in s.Name.lower() or 'gpu' in s.Name.lower())]
            if temps: return sum(temps)/len(temps)
        except: pass
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for entries in temps.values():
                for entry in entries:
                    if hasattr(entry,'current') and entry.current: return float(entry.current)
    except: pass
    return random.uniform(30,45)

def generate_virtual_reading(user_id):
    temp = get_laptop_temperature()
    temperature = round(temp/3,1)
    humidity = round(random.uniform(30,60),1)
    co2 = 400 + int(temp*10)
    pm25 = round(temp/2,1)
    pm10 = pm25 + random.uniform(5,20)
    tvoc = random.randint(50,400)
    add_reading(user_id, temperature, humidity, co2, pm25, pm10, tvoc)
    return temp

# ==============================
# SESSION DEFAULTS
# ==============================
if 'logged_in' not in st.session_state: st.session_state.logged_in=False
if 'user' not in st.session_state: st.session_state.user=None
if 'page' not in st.session_state: st.session_state.page="home"
if 'last_aqi' not in st.session_state: st.session_state.last_aqi=None

# ==============================
# ALERTS
# ==============================
def speak_browser(text):
    components.html(f"<script>var msg = new SpeechSynthesisUtterance('{text}'); window.speechSynthesis.speak(msg);</script>", height=0)

def notify_browser(title, body):
    components.html(f"<script>if(Notification.permission!=='granted'){{Notification.requestPermission();}} new Notification('{title}',{{body:'{body}'}});</script>", height=0)

def trigger_browser_alerts(aqi, cat):
    if st.session_state.last_aqi is None or abs(aqi-st.session_state.last_aqi)>=10:
        speak_browser(f"Air quality alert. AQI is {aqi}, {cat}")
        notify_browser("Air Quality Alert", f"AQI is {aqi} — {cat}")
    st.session_state.last_aqi=aqi

# ==============================
# PAGES
# ==============================
def page_home():
    st.title("Indoor Air Wellness")
    st.write("Monitor and improve your indoor air quality.")
    st.markdown("---")
    if not st.session_state.logged_in:
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Login"):
                st.session_state.page="login"
                st.rerun()
        with col2:
            if st.button("Sign Up"):
                st.session_state.page="signup"
                st.rerun()
    else:
        st.success(f"Logged in as {st.session_state.user['username']}")
        if st.button("Go to Dashboard"):
            st.session_state.page="dashboard"
            st.rerun()

# LOGIN PAGE
def page_login():
    st.title("Login")
    login = st.text_input("Username or Email")
    password = st.text_input("Password", type="password")
    if st.button("Login"):
        user = verify_user(login, password)
        if user:
            st.session_state.logged_in=True
            st.session_state.user=user
            st.session_state.page="dashboard"
            st.success("Logged in successfully!")
            st.rerun()
        else:
            st.error("Invalid credentials")
    if st.button("Back to Home"):
        st.session_state.page="home"
        st.rerun()

# SIGNUP PAGE
def page_signup():
    st.title("Sign Up")
    username = st.text_input("Username")
    email = st.text_input("Email")
    password = st.text_input("Password", type="password")
    if st.button("Sign Up"):
        success, msg = create_user(username,email,password)
        if success:
            st.success(msg)
            st.session_state.page="login"
            st.rerun()
        else:
            st.error(msg)
    if st.button("Back to Home"):
        st.session_state.page="home"
        st.rerun()

# DASHBOARD PAGE
def page_dashboard():
    st.title("Dashboard")
    user_id = st.session_state.user['id']
    if st.button("Add Virtual Reading"):
        generate_virtual_reading(user_id)
        st.success("Reading added")
    df = get_readings(user_id)
    if not df.empty:
        latest = df.iloc[0]
        aqi = pm25_to_aqi(latest['pm25'])
        cat, color = aqi_category(aqi)
        st.metric("Latest AQI", f"{aqi} ({cat})")
        trigger_browser_alerts(aqi, cat)
        st.write("Health Tip:", health_tip(cat))
        fig = px.line(df, x='timestamp', y=['pm25','pm10','co2','tvoc'])
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No readings yet")
    if st.button("Logout"):
        st.session_state.logged_in=False
        st.session_state.user=None
        st.session_state.page="home"
        st.rerun()

# ROUTER
PAGES = {
    "home": page_home,
    "login": page_login,
    "signup": page_signup,
    "dashboard": page_dashboard
}

# RENDER CURRENT PAGE
PAGES.get(st.session_state.page, page_home)()
