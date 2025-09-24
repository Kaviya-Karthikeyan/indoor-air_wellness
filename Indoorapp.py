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
import wmi   # NEW for OpenHardwareMonitor integration

# =============================
# CONFIG & DB INIT
# =============================
st.set_page_config(page_title="Indoor Air Wellness", layout="wide")

DB_DIR = "data"
DB_PATH = os.path.join(DB_DIR, "readings.db")
os.makedirs(DB_DIR, exist_ok=True)
REFRESH_INTERVAL = 5

# =============================
# IMAGE HELPER
# =============================
IMG_DIR = "images"
def img_path(filename):
    base = os.path.dirname(os.path.abspath(__file__))  # folder where Indoorapp.py lives
    return os.path.join(base, "images", filename)


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

# =============================
# AUTH HELPERS
# =============================
def create_user(username, email, password):
    hashed = pbkdf2_sha256.hash(password)
    try:
        c = conn.cursor()
        c.execute("INSERT INTO users (username, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                  (username, email, hashed, datetime.datetime.utcnow().isoformat()))
        conn.commit()
        return True, "User created"
    except sqlite3.IntegrityError as e:
        return False, str(e)

def verify_user(login, password):
    c = conn.cursor()
    c.execute("SELECT id, password_hash, username FROM users WHERE username=? OR email=?", (login, login))
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

# =============================
# READING HELPERS
# =============================
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
    c.execute("SELECT timestamp, temperature, humidity, co2, pm25, pm10, tvoc FROM readings WHERE user_id=? ORDER BY timestamp DESC LIMIT ?",
              (user_id, limit))
    rows = c.fetchall()
    cols = ["timestamp","temperature","humidity","co2","pm25","pm10","tvoc"]
    return pd.DataFrame(rows, columns=cols)

def get_latest_reading(user_id):
    df = get_readings(user_id, limit=1)
    if df.empty:
        return None
    return df.iloc[0].to_dict()

# =============================
# AQI HELPERS
# =============================
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

# =============================
# LAPTOP TEMPERATURE
# =============================
def get_laptop_temperature():
    try:
        w = wmi.WMI(namespace="root\\OpenHardwareMonitor")
        sensors = w.Sensor()
        cpu_temps = [s.Value for s in sensors if s.SensorType == 'Temperature' and ("cpu" in s.Name.lower() or "gpu" in s.Name.lower())]
        if cpu_temps:
            return sum(cpu_temps) / len(cpu_temps)
        battery_temps = [s.Value for s in sensors if s.SensorType == 'Temperature' and "battery" in s.Name.lower()]
        if battery_temps:
            return sum(battery_temps) / len(battery_temps)
    except Exception:
        pass

    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for entries in temps.values():
                for entry in entries:
                    if hasattr(entry, "current") and entry.current is not None:
                        return float(entry.current)
    except Exception:
        pass

    return random.uniform(30, 45)

def generate_virtual_reading(user_id):
    temp = get_laptop_temperature()
    temperature = round(temp / 3, 1)
    humidity = round(random.uniform(30, 60), 1)
    co2 = 400 + int(temp * 10)
    pm25 = round(temp / 2, 1)
    pm10 = pm25 + random.uniform(5, 20)
    tvoc = random.randint(50, 400)
    add_reading(user_id, temperature, humidity, co2, pm25, pm10, tvoc)
    return temp

# =============================
# SESSION DEFAULTS
# =============================
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
if 'user' not in st.session_state:
    st.session_state.user = None
if 'page' not in st.session_state:
    st.session_state.page = "home"
if 'last_aqi' not in st.session_state:
    st.session_state.last_aqi = None

# =============================
# ALERTS
# =============================
def speak_browser(text):
    components.html(f"""
    <script>
    var msg = new SpeechSynthesisUtterance("{text}");
    window.speechSynthesis.speak(msg);
    </script>
    """, height=0)

def notify_browser(title, body):
    components.html(f"""
    <script>
    if (Notification.permission !== "granted") {{
        Notification.requestPermission();
    }}
    new Notification("{title}", {{ body: "{body}" }});
    </script>
    """, height=0)

def trigger_browser_alerts(aqi, cat):
    if st.session_state.last_aqi is None or abs(aqi - st.session_state.last_aqi) >= 10:
        speak_browser(f"Air quality alert. AQI is {aqi}, {cat}")
        notify_browser("Air Quality Alert", f"AQI is {aqi} — {cat}")
    st.session_state.last_aqi = aqi

# =============================
# PAGES
# =============================
def page_home():
    st.title("Indoor Air Wellness")
    st.write("Monitor and improve your indoor air quality.")
    st.markdown("---")
    if not st.session_state.logged_in:
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Login"):
                st.session_state.page = "login"
                st.rerun()
        with col2:
            if st.button("Sign Up"):
                st.session_state.page = "signup"
                st.rerun()
    else:
        st.success(f"Logged in as {st.session_state.user['username']}")
        if st.button("Go to Dashboard"):
            st.session_state.page = "dashboard"
            st.rerun()

def page_login():
    st.header("🔐 User Login")
    with st.form("login_form", clear_on_submit=False):
        login = st.text_input("Username or Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign In")
        if submitted:
            res = verify_user(login, password)
            if res:
                st.session_state.logged_in = True
                st.session_state.user = res
                st.session_state.page = "dashboard"
                st.success(f"✅ Welcome back, {res['username']}!")
                st.rerun()
            else:
                st.error("❌ Invalid username/email or password.")

def page_signup():
    st.header("Sign up")
    with st.form("signup"):
        username = st.text_input("Choose a username")
        email = st.text_input("Email")
        pw1 = st.text_input("Password", type="password")
        pw2 = st.text_input("Confirm password", type="password")
        submitted = st.form_submit_button("Create account")
        if submitted:
            if pw1 != pw2:
                st.error("Passwords do not match.")
            else:
                ok, msg = create_user(username, email, pw1)
                if ok:
                    st.success("Account created. Please log in.")
                else:
                    st.error(f"Failed: {msg}")

def page_dashboard():
    st_autorefresh(interval=REFRESH_INTERVAL * 1000, key="refresh")
    st.header("Live Dashboard")
    st.caption(f"Last Updated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    user_id = st.session_state.user['id']
    latest = get_latest_reading(user_id)
    if latest is None:
        st.warning("No readings yet. Click below to simulate.")
    else:
        pm25 = latest.get("pm25")
        aqi = pm25_to_aqi(pm25)
        cat, color = aqi_category(aqi)
        st.markdown(f"<div style='background:{color};padding:12px;border-radius:6px;text-align:center'><h2>AQI: {aqi} — {cat}</h2></div>", unsafe_allow_html=True)
        trigger_browser_alerts(aqi, cat)
        col1, col2, col3 = st.columns(3)
        def gauge_chart(value, min_val, max_val, title, color):
            fig = go.Figure(go.Indicator(
                mode="gauge+number",
                value=value,
                gauge={'axis': {'range': [min_val, max_val]}, 'bar': {'color': color}},
                title={'text': title}
            ))
            fig.update_layout(height=250, margin=dict(t=0, b=0, l=0, r=0), template="plotly_dark")
            return fig
        col1.plotly_chart(gauge_chart(latest["temperature"], 0, 40, "Temperature (°C)", "orange"), use_container_width=True)
        col2.plotly_chart(gauge_chart(latest["co2"], 0, 2000, "CO₂ (ppm)", "green"), use_container_width=True)
        col3.plotly_chart(gauge_chart(latest["pm25"], 0, 200, "PM2.5 (µg/m³)", "red"), use_container_width=True)
        st.info(health_tip(cat))
        laptop_temp = get_laptop_temperature()
        st.markdown(f"<div style='background:#111;padding:12px;border-radius:6px;color:#bfefff'>💻 Sensor Heat Level: {laptop_temp:.1f} °C</div>", unsafe_allow_html=True)
        df = get_readings(user_id, limit=50)
        if not df.empty:
            df['aqi'] = df['pm25'].apply(pm25_to_aqi)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            fig = px.line(df.sort_values('timestamp'), x='timestamp', y='aqi', title="AQI over time",
                          markers=True, line_shape='spline', template="plotly_dark", color_discrete_sequence=["cyan"])
            st.plotly_chart(fig, use_container_width=True)
    if st.button("Simulate Reading"):
        temp = generate_virtual_reading(user_id)
        st.success(f"📡 Reading added (sensor heat {temp:.1f} °C)")
        st.rerun()

def page_history():
    st.header("History & Export")
    df = get_readings(st.session_state.user['id'])
    if df.empty:
        st.info("No data yet.")
        return
    st.dataframe(df)
    csv = df.to_csv(index=False).encode()
    st.download_button("Download CSV", csv, file_name="aqi_history.csv", mime="text/csv")
    df['aqi'] = df['pm25'].apply(pm25_to_aqi)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    fig = px.line(df.sort_values('timestamp'), x='timestamp', y='aqi', title="AQI over time",
                  markers=True, line_shape='spline', template="plotly_dark", color_discrete_sequence=["cyan"])
    st.plotly_chart(fig, use_container_width=True)

def page_recommendations():
    st.header("💡 Personalized Wellness Recommendations")
    latest = get_latest_reading(st.session_state.user['id'])
    if latest is None:
        st.info("No readings yet.")
        return
    aqi = pm25_to_aqi(latest.get("pm25"))
    cat, _ = aqi_category(aqi)
    st.subheader(f"Current AQI: {aqi} — {cat}")
    st.markdown("### 🌱 Suggested Indoor Plants")
    cols = st.columns(3)
    with cols[0]:
        st.image(img_path("areca_palm.jpg"), width=120)
        st.caption("Areca Palm – Absorbs CO₂ effectively")
    with cols[1]:
        st.image(img_path("snake_plant.jpg"), width=120)
        st.caption("Snake Plant – Releases O₂ at night")
    with cols[2]:
        st.image(img_path("peace_lily.jpg"), width=120)
        st.caption("Peace Lily – Absorbs VOCs & toxins")
    st.markdown("---")
    st.markdown("### 🧘 Yoga & Lifestyle Suggestions")
    shown=False
    if latest["co2"] > 1000:
        st.info("🪟 High CO₂ → Practice Pranayama (breathing exercises).")
    if latest["pm25"] > 50:
        st.info("🌫 High PM2.5 → Avoid vacuuming. Try Tadasana indoors.")
    if latest["humidity"] < 30:
        st.info("💧 Low Humidity → Use humidifier & Anulom-Vilom.")
    if latest["humidity"] > 70:
        st.info("🌧 High Humidity → Risk of mold. Do Surya Namaskar indoors.")
    if aqi <= 50:
        st.success("🌞 Air is clean → Go for a walk or Surya Namaskar outside.")
    if not shown:
        st.info(" 🧘 try simple breathing exercises and light stretching indoors today")
    st.markdown("---")
    tips = [
        "Drink warm lemon water 🍋 in the morning.",
        "Open windows 20 mins daily",
        "Do 10 mins meditation  for lung health.",
        "Keep a bowl of water near plants 🌱.",
        "Avoid chemical sprays indoors 🚫."
    ]
    st.markdown(f"### ✨ Daily Tip\n👉 {random.choice(tips)}")

def page_patterns():
    st.header("📊 Daily AQI Patterns")
    df = get_readings(st.session_state.user['id'], limit=500)
    if df.empty:
        st.info("No data yet.")
        return
    df['aqi'] = df['pm25'].apply(pm25_to_aqi)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['hour'] = df['timestamp'].dt.hour
    hourly_avg = df.groupby('hour')['aqi'].mean().reset_index()
    if hourly_avg.empty:
        st.info("Not enough data yet.")
        return
    worst = hourly_avg.loc[hourly_avg['aqi'].idxmax()]
    st.markdown(f"⚠ Worst hour: {int(worst['hour']):02d}:00 (Avg AQI {worst['aqi']:.1f})")
    fig = px.line(hourly_avg, x="hour", y="aqi", markers=True, title="Average AQI by Hour")
    st.plotly_chart(fig, use_container_width=True)

def page_profile():
    st.header("Profile")
    user = get_user_by_id(st.session_state.user['id'])
    st.write("Username:", user['username'])
    st.write("Email:", user['email'])
    st.write("Account created:", user['created_at'])
    with st.form("chg_pw"):
        current = st.text_input("Current password", type="password")
        new1 = st.text_input("New password", type="password")
        new2 = st.text_input("Confirm new password", type="password")
        submitted = st.form_submit_button("Change password")
        if submitted:
            verified = verify_user(user['username'], current)
            if not verified:
                st.error("Current password incorrect.")
            elif new1 != new2:
                st.error("Passwords do not match.")
            else:
                change_password(user['id'], new1)
                st.success("Password changed successfully.")

def page_settings():
    st.header("⚙ Settings")
    theme = st.radio("🎨 Theme", ["Dark", "Light"], index=0)
    st.checkbox("Enable Voice Alerts", value=True)
    st.checkbox("Enable Browser Notifications", value=True)
    st.slider("💾 Max readings", 100, 2000, 1000, step=100)
    if st.button("🗑 Clear History"):
        conn = get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM readings WHERE user_id=?", (st.session_state.user['id'],))
        conn.commit()
        st.success("History cleared.")
    new_email = st.text_input("Update Email")
    if st.button("Update Email"):
        if new_email:
            c = conn.cursor()
            c.execute("UPDATE users SET email=? WHERE id=?", (new_email, st.session_state.user['id']))
            conn.commit()
            st.success("Email updated!")
    if st.button("❌ Delete Account"):
        conn = get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM users WHERE id=?", (st.session_state.user['id'],))
        conn.commit()
        st.session_state.logged_in = False
        st.session_state.user = None
        st.session_state.page = "home"
        st.success("Account deleted.")

# =============================
# ROUTER WITH SIDEBAR
# =============================
PAGES = {
    "home": page_home,
    "login": page_login,
    "signup": page_signup,
    "dashboard": page_dashboard,
    "history": page_history,
    "recommendations": page_recommendations,
    "patterns": page_patterns,
    "profile": page_profile,
    "settings": page_settings
}

if st.session_state.logged_in:
    with st.sidebar:
        st.markdown('<div style="text-align:center;font-size:22px;font-weight:bold;color:#00ffff">🌍 Navigation</div>', unsafe_allow_html=True)
        # use option_menu if available; fallback to radio/selectbox if not
        try:
            selected = option_menu(
                None,
                ["Dashboard", "History", "Recommendations", "Patterns", "Profile", "Settings", "Logout"],
                icons=["house", "clock-history", "lightbulb", "bar-chart-line", "person-circle", "gear", "box-arrow-right"],
                default_index=0,
                orientation="vertical"
            )
        except Exception:
            selected = st.selectbox("Go to", ["Dashboard", "History", "Recommendations", "Patterns", "Profile", "Settings", "Logout"])
    if selected == "Dashboard": st.session_state.page = "dashboard"
    elif selected == "History": st.session_state.page = "history"
    elif selected == "Recommendations": st.session_state.page = "recommendations"
    elif selected == "Patterns": st.session_state.page = "patterns"
    elif selected == "Profile": st.session_state.page = "profile"
    elif selected == "Settings": st.session_state.page = "settings"
    elif selected == "Logout":
        st.session_state.logged_in = False
        st.session_state.user = None
        st.session_state.page = "home"
        st.rerun()
else:
    with st.sidebar:
        st.markdown('<div style="text-align:center;font-size:18px;font-weight:bold;color:#00ffff">🔐 Please Login</div>', unsafe_allow_html=True)
        if st.button("Login"):
            st.session_state.page = "login"
            st.rerun()
        if st.button("Sign Up"):
            st.session_state.page = "signup"
            st.rerun()
        st.write("Demo account: try creating one or sign up.")

# Render the current page
PAGES.get(st.session_state.page, page_home)()
