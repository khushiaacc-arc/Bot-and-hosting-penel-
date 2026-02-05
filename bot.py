import os
import sys
import threading
import subprocess
import zipfile
import random
import hashlib
import json
import atexit
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
from flask import Flask, request, redirect, session, url_for, render_template_string, jsonify, send_file
import telebot
import re
import time
import secrets
import signal
import psutil
from werkzeug.utils import secure_filename
import traceback

# ---------------- CONFIG ----------------
PORT = int(os.environ.get("PORT", 10000))

DATA_DIR = os.path.join(os.getcwd(), "data")
BOTS_DIR = os.path.join(DATA_DIR, "bots")
DB_FILE = os.path.join(DATA_DIR, "bothosting.db")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BOTS_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# Default configuration
DEFAULT_CONFIG = {
    "BOT_TOKEN": os.environ.get("BOT_TOKEN"),
    "ADMIN_ID": 8465446299,
    "DEFAULT_SLOTS": 3,
    "MAX_FILE_SIZE": 50 * 1024 * 1024,
    "ALLOWED_EXTENSIONS": [".py", ".zip"],
    "SESSION_TIMEOUT": 24 * 60 * 60,
    "PUBLIC_URL": os.environ.get("PUBLIC_URL", None)
}

# Helper function to save config safely
def save_config(cfg):
    try:
        cfg_to_save = {k: list(v) if isinstance(v, set) else v for k, v in cfg.items()}
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg_to_save, f, indent=4)
    except Exception as e:
        print(f"❌ Failed to save config: {e}")

# Load config
config = {}
try:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        if "ALLOWED_EXTENSIONS" in config:
            config["ALLOWED_EXTENSIONS"] = set(config["ALLOWED_EXTENSIONS"])
        for key, value in DEFAULT_CONFIG.items():
            if key not in config:
                config[key] = value
        save_config(config)
    else:
        config = DEFAULT_CONFIG.copy()
        config["ALLOWED_EXTENSIONS"] = set(config["ALLOWED_EXTENSIONS"])
        save_config(config)
except Exception as e:
    print(f"❌ Config load error: {e}")
    config = DEFAULT_CONFIG.copy()
    config["ALLOWED_EXTENSIONS"] = set(config["ALLOWED_EXTENSIONS"])
    save_config(config)

# ---------------- SAFETY CHECK ----------------
BOT_TOKEN = config.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ BOT_TOKEN is not set! Please define BOT_TOKEN in environment or config.json")
    sys.exit(1)

ADMIN_ID = config.get("ADMIN_ID", 8465446299)
DEFAULT_SLOTS = config.get("DEFAULT_SLOTS", 3)
MAX_FILE_SIZE = config.get("MAX_FILE_SIZE", 50 * 1024 * 1024)
ALLOWED_EXTENSIONS = config.get("ALLOWED_EXTENSIONS", {".py", ".zip"})
SESSION_TIMEOUT = config.get("SESSION_TIMEOUT", 24 * 60 * 60)
PUBLIC_URL = config.get("PUBLIC_URL")

# ---------------- DATABASE SETUP ----------------
try:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = conn.cursor()

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            password TEXT,
            verified INTEGER DEFAULT 0,
            slots INTEGER DEFAULT {DEFAULT_SLOTS},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            bot_name TEXT,
            original_name TEXT,
            file_size INTEGER,
            upload_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'stopped',
            last_started TIMESTAMP,
            FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            action TEXT,
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    print("✅ Database connected and tables initialized")

except Exception as e:
    print(f"❌ Database setup failed: {e}")
    sys.exit(1)

# ---------------- LOGGING ----------------
def log_activity(user_id, action, details=""):
    try:
        cur.execute(
            "INSERT INTO activity_log (telegram_id, action, details) VALUES (?, ?, ?)",
            (user_id, action, details)
        )
        conn.commit()
    except Exception as e:
        print(f"Logging error: {e}")

# ---------------- TELEGRAM BOT ----------------
try:
    tg = telebot.TeleBot(BOT_TOKEN)
    print("✅ Telegram bot initialized")
except Exception as e:
    print(f"❌ Telegram bot error: {e}")
    sys.exit(1)

OTP_CACHE = {}
RUNNING_BOTS = {}
ACTIVE_SESSIONS = {}

# ---------------- HELPER FUNCTIONS ----------------
def generate_otp():
    return str(random.randint(100000, 999999))

def send_otp(tg_id):
    otp = generate_otp()
    OTP_CACHE[tg_id] = {
        "otp": otp,
        "expires": datetime.now() + timedelta(minutes=10),
        "attempts": 0
    }
    try:
        tg.send_message(
            tg_id,
            f"""
🔐 𝐒𝐄𝐂𝐔𝐑𝐈𝐓𝐘 𝐎𝐓𝐏 

𝐘𝐨𝐮𝐫 𝐯𝐞𝐫𝐢𝐟𝐢𝐜𝐚𝐭𝐢𝐨𝐧 𝐜𝐨𝐝𝐞:👉🏻 `{otp}` 
📱 𝐒ᴇɴᴛ ᴛᴏ: `{tg_id}`

⏰ 𝐕ᴀʟɪ𝐝 ꜰᴏʀ: 𝟏𝟎 𝐌ɪɴᴜᴛᴇ𝐬
""",
            parse_mode="Markdown"
        )
        return True
    except Exception as e:
        print(f"OTP send error: {e}")
        return False

def get_user_bots_count(user_id):
    cur.execute("SELECT COUNT(*) FROM uploads WHERE telegram_id=?", (user_id,))
    return cur.fetchone()[0]

def get_running_bots_count(user_id):
    count = 0
    for bot_name in RUNNING_BOTS:
        if bot_name.startswith(f"{user_id}_"):
            count += 1
    return count

def get_user_slots(user_id):
    cur.execute("SELECT slots FROM users WHERE telegram_id=?", (user_id,))
    row = cur.fetchone()
    return row[0] if row else DEFAULT_SLOTS

def allowed_file(filename):
    return any(filename.endswith(ext) for ext in ALLOWED_EXTENSIONS)

def cleanup_bot_processes():
    for bot_name, bot_info in list(RUNNING_BOTS.items()):
        try:
            process = bot_info["process"]
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        except:
            pass

atexit.register(cleanup_bot_processes)

# ---------------- TELEGRAM COMMANDS ----------------
@tg.message_handler(commands=["start", "help"])
def tg_start(msg):
    chat_id = msg.chat.id
    user_id = msg.from_user.id
    
    welcome_text = """
𝐖𝐄𝐋𝐂𝐎𝐌𝐄 𝐓𝐎 𝐇𝐎𝐒𝐓𝐈𝐍𝐆 𝐖𝐄𝐁𝐒𝐈𝐓𝐄 𝐁𝐎𝐓

𝐊ᴇᴇᴘ 𝐲𝐨𝐮𝐫 𝐏𝐲𝐭𝐡𝐨𝐧 𝐛𝐨𝐭𝐬 𝐨𝐧𝐥𝐢𝐧𝐞 𝟐𝟒/𝟕 𝐨𝐧 𝐨𝐮𝐫 𝐡𝐢𝐠𝐡-𝐬𝐩𝐞𝐞𝐝 𝐜𝐥𝐨𝐮𝐝 𝐬𝐞𝐫𝐯𝐞𝐫𝐬.

🤖𝐁𝐨𝐭 𝐅𝐞𝐚𝐭𝐮𝐫𝐞𝐬:
• 🌐 𝐎𝐩𝐞𝐧 𝐂𝐨𝐧𝐭𝐫𝐨𝐥 𝐏𝐚𝐧𝐞𝐥
• 🛡️ 𝐒𝐞𝐜𝐮𝐫𝐞 𝐎𝐓𝐏 𝐋𝐨𝐠𝐢𝐧
• 💻 𝐖𝐞𝐛 𝐂𝐨𝐝𝐞 𝐄𝐝𝐢𝐭𝐨𝐫
• 📊 𝐋𝐢𝐯𝐞 𝐌𝐨𝐧𝐢𝐭𝐨𝐫𝐢𝐧𝐠

🛠 𝐂𝐮𝐫𝐫𝐞𝐧𝐭 𝐋𝐢𝐦𝐢𝐭𝐬:
├ 📁 𝐒𝐮𝐩𝐩𝐨𝐫𝐭: .𝐩𝐲 / .𝐳𝐢𝐩 𝐨𝐧𝐥𝐲
├ 🎯 𝐒𝐥𝐨𝐭𝐬: 𝟑 𝐅𝐫𝐞𝐞 𝐁𝐨𝐭𝐬
└ ⚡ 𝐒𝐩𝐞𝐞𝐝: 𝐈𝐧𝐬𝐭𝐚𝐧𝐭 𝐃𝐞𝐩𝐥𝐨𝐲

👇 𝐂𝐥𝐢𝐜𝐤 𝐛𝐞𝐥𝐨𝐰 𝐭𝐨 𝐚𝐜𝐜𝐞𝐬𝐬 𝐲𝐨𝐮𝐫 𝐩𝐚𝐧𝐞𝐥:
"""
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    if PUBLIC_URL:
        keyboard.add(
            InlineKeyboardButton("🌐 𝐎ᴘᴇ𝐧 𝐏ᴀɴᴇ𝐥", url=PUBLIC_URL),
            InlineKeyboardButton("📊 𝐌ʏ 𝐒ᴛᴀᴛs", callback_data="stats"),
            InlineKeyboardButton("🆘 𝐇ᴇʟ𝐩", callback_data="help"),
            InlineKeyboardButton("𝐇ᴏsᴛɪɴɢ 𝐁ᴏᴛ 𝐕𝟏", url="https://t.me/Kaalix_gang_bot")
        )
    
    try:
        photo_url = "https://files.catbox.moe/unrq3g.png"
        tg.send_photo(chat_id, photo=photo_url, caption=welcome_text, 
                     parse_mode="Markdown", reply_markup=keyboard)
    except:
        tg.send_message(chat_id, welcome_text, parse_mode="Markdown", reply_markup=keyboard)
    
    log_activity(user_id, "start_command")

@tg.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id

    if call.data == "stats":
        bots_count = get_user_bots_count(user_id)
        running_count = get_running_bots_count(user_id)
        slots = get_user_slots(user_id)

        bar_length = 10
        progress = (bots_count / slots) if slots > 0 else 0
        filled_length = int(bar_length * progress)
        bar = '■' * filled_length + '□' * (bar_length - filled_length)

        stats_text = f"""
📊 𝐔𝐬𝐞𝐫 𝐒𝐭𝐚𝐭𝐢𝐬𝐭𝐢𝐜𝐬 𝐑𝐞𝐩𝐨𝐫𝐭

👤 𝐔sᴇʀ 𝐈𝐃: `{user_id}`
📁 𝐓ᴏᴛᴀʟ 𝐁ᴏᴛs: {bots_count}
🚀 𝐑ᴜɴɴɪɴɢ: {running_count}
🎯 𝐌ᴀx 𝐒ʟᴏᴛs: {slots}

📈 𝐒ʟᴏᴛ 𝐔sᴀɢᴇ: `[{bar}]` {bots_count}/{slots}

𝐒ᴛᴀᴛᴜs: {'✅ 𝐀ᴄᴛɪᴠᴇ' if bots_count < slots else '⚠️ 𝐅ᴜʟʟ'}
"""
        tg.answer_callback_query(call.id)
        tg.send_message(chat_id, stats_text, parse_mode="Markdown")

    elif call.data == "help":
        help_text = """
🆘 𝐇ᴇʟᴘ & 𝐒ᴜᴘᴘᴏʀᴛ 𝐂ᴇɴᴛᴇʀ

🚀 𝐂ᴏᴍᴍᴏɴ 𝐈𝐬𝐬𝐮𝐞𝐬:
• 𝐋ᴏɢɪɴ 𝐅ᴀɪʟᴇᴅ: Check ID & Password
• 𝐎𝐓𝐏 𝐈𝐬𝐬ᴜᴇ: Wait 60s or check Spam
• 𝐁ᴏᴛ 𝐄ʀʀᴏʀ: Check your Python code
• 𝐔ᴘʟᴏᴀᴅ:  (.py / .zip)

🛠 𝐒ᴜᴘᴘᴏʀᴛ 𝐂ʜᴀɴɴᴇʟ:
┌── 👤 𝐀ᴅᴍɪɴ: @ROCKY_BHAI787
└── 📢 𝐔𝐩𝐝ᴀᴛᴇs: @KAALIX_OS
"""
        tg.answer_callback_query(call.id)
        tg.send_message(chat_id, help_text, parse_mode="Markdown")

    elif call.data == "premium":
        premium_text = """
👑 *Premium Features*

• Unlimited bot slots
• Priority support
• Faster startup
• Advanced monitoring

💰 Contact @ROCKYBHAI787
"""
        tg.answer_callback_query(call.id)
        tg.send_message(chat_id, premium_text, parse_mode="Markdown")
        
@tg.message_handler(commands=["stats"])
def stats_command(msg):
    chat_id = msg.chat.id
    user_id = msg.from_user.id

    bots_count = get_user_bots_count(user_id)
    running_count = get_running_bots_count(user_id)
    slots = get_user_slots(user_id)

    bar_length = 10
    progress = (bots_count / slots) if slots > 0 else 0
    filled_length = int(bar_length * progress)
    bar = '■' * filled_length + '□' * (bar_length - filled_length)

    stats_text = f"""
📊 𝐔𝐬𝐞𝐫 𝐒𝐭𝐚𝐭𝐢𝐬𝐭𝐢𝐜𝐬 𝐑𝐞𝐩𝐨𝐫𝐭

👤 𝐔sᴇʀ 𝐈𝐃: `{user_id}`
📁 𝐓ᴏᴛᴀʟ 𝐁ᴏᴛs: {bots_count}
🚀 𝐑ᴜɴɴɪɴɢ: {running_count}
🎯 𝐌ᴀx 𝐒ʟᴏᴛs: {slots}

📈 𝐒ʟᴏᴛ 𝐔sᴀɢᴇ: `[{bar}]` {bots_count}/{slots}
"""
    tg.send_message(chat_id, stats_text, parse_mode="Markdown")

@tg.message_handler(commands=["admin"])
def admin_panel(msg):
    if msg.from_user.id != ADMIN_ID:
        tg.send_message(msg.chat.id, "❌ Access denied!")
        return
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
        InlineKeyboardButton("👥 Users", callback_data="admin_users"),
        InlineKeyboardButton("🔄 Restart All", callback_data="admin_restart"),
        InlineKeyboardButton("🛑 Stop All", callback_data="admin_stop")
    )
    
    tg.send_message(msg.chat.id, "🛠 *Admin Panel*", parse_mode="Markdown", reply_markup=keyboard)

# ---------------- FLASK APP ----------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=7)
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

# ---------------- HTML TEMPLATES ----------------

BASE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KAALIX_OS | Bot Cloud</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=Rajdhani:wght@500;700&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
    <style>
        :root {
            --accent: #00f2ff;
            --accent-glow: rgba(0, 242, 255, 0.4);
            --bg-body: #050508;
            --panel-bg: #0d0e14;
            --border: #1a1c26;
            --danger: #ff4757;
            --success: #00ff88;
            --text-dim: #94a3b8;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Rajdhani', sans-serif;
            background-color: var(--bg-body);
            color: #f1f5f9;
            min-height: 100vh;
            overflow-x: hidden;
        }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: var(--bg-body); }
        ::-webkit-scrollbar-thumb { background: #222; border-radius: 10px; }
        .auth-wrapper {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .main-card {
            background: var(--panel-bg);
            border: 1px solid var(--border);
            border-radius: 16px;
            width: 100%;
            max-width: 450px;
            overflow: hidden;
            box-shadow: 0 20px 50px rgba(0,0,0,0.5);
            position: relative;
        }
        .main-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 4px;
            background: linear-gradient(90deg, transparent, var(--accent), transparent);
        }
        .card-header {
            padding: 30px 20px;
            text-align: center;
            background: rgba(255,255,255,0.02);
            border-bottom: 1px solid var(--border);
        }
        .card-header h1 {
            font-family: 'Orbitron', sans-serif;
            color: var(--accent);
            font-size: 1.6rem;
            letter-spacing: 2px;
            text-shadow: 0 0 15px var(--accent-glow);
        }
        .card-body { padding: 30px; }
        .form-group { margin-bottom: 20px; }
        label {
            display: block;
            font-size: 0.8rem;
            font-weight: 700;
            color: var(--text-dim);
            text-transform: uppercase;
            margin-bottom: 8px;
            letter-spacing: 1px;
        }
        input, textarea, select {
            width: 100%;
            background: #000;
            border: 1px solid var(--border);
            padding: 12px 15px;
            border-radius: 8px;
            color: #fff;
            font-family: 'Rajdhani', sans-serif;
            font-size: 1rem;
            outline: none;
            transition: 0.3s;
        }
        input:focus {
            border-color: var(--accent);
            box-shadow: 0 0 10px var(--accent-glow);
        }
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            padding: 12px 25px;
            border-radius: 6px;
            font-family: 'Orbitron', sans-serif;
            font-weight: 700;
            font-size: 0.85rem;
            cursor: pointer;
            transition: 0.3s;
            text-decoration: none;
            border: none;
            width: 100%;
        }
        .btn-primary {
            background: var(--accent);
            color: #000;
        }
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px var(--accent-glow);
            filter: brightness(1.1);
        }
        .alert {
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-weight: 700;
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 10px;
            border: 1px solid transparent;
        }
        .alert-success { background: rgba(0, 255, 136, 0.1); color: var(--success); border-color: var(--success); }
        .alert-error { background: rgba(255, 71, 87, 0.1); color: var(--danger); border-color: var(--danger); }
        .auth-footer {
            text-align: center;
            margin-top: 20px;
            font-size: 0.85rem;
        }
        .auth-footer a {
            color: var(--accent);
            text-decoration: none;
            font-weight: 700;
        }
        @media (max-width: 480px) {
            .card-body { padding: 20px; }
            .card-header h1 { font-size: 1.3rem; }
        }
    </style>
</head>
<body>
"""

LOGIN_HTML = BASE_HTML + """
    <div class="auth-wrapper">
        <div class="main-card">
            <div class="card-header">
                <h1><i class="fas fa-robot"></i> KAALIX_OS</h1>
                <p style="font-size: 0.8rem; color: var(--text-dim); margin-top: 5px;">AUTHORIZATION_REQUIRED</p>
            </div>
            <div class="card-body">
                {% with messages = get_flashed_messages() %}
                    {% if messages %}
                        {% for message in messages %}
                            <div class="alert alert-error"><i class="fas fa-exclamation-triangle"></i> {{ message }}</div>
                        {% endfor %}
                    {% endif %}
                {% endwith %}
                
                <form method="POST" action="/">
                    <div class="form-group">
                        <label for="tgid">Telegram_ID</label>
                        <input type="number" id="tgid" name="tgid" required placeholder="123456789">
                    </div>
                    
                    <div class="form-group">
                        <label for="password">Access_Key</label>
                        <input type="password" id="password" name="password" required placeholder="••••••••">
                    </div>
                    
                    <div class="form-group" style="display: flex; align-items: center; gap: 8px;">
                        <input type="checkbox" name="remember" style="width: 16px; height: 16px; cursor: pointer;">
                        <span style="font-size: 0.8rem; color: var(--text-dim);">REMEMBER_ME (7 DAYS)</span>
                    </div>
                    
                    <button type="submit" class="btn btn-primary">
                        <i class="fas fa-sign-in-alt"></i> LOGIN
                    </button>
                </form>
                
                <div class="auth-footer">
                    <a href="/forgot">FORGOT_PASSWORD</a> | 
                    <a href="https://t.me/ROCKYBHAI787" target="_blank">SUPPORT</a>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

OTP_HTML = BASE_HTML + """
    <div class="auth-wrapper">
        <div class="main-card">
            <div class="card-header">
                <h1><i class="fas fa-shield-alt"></i> VERIFY_IDENTITY</h1>
                <p style="font-size: 0.8rem; color: var(--text-dim); margin-top: 5px;">SECURITY_PROTOCOL_ACTIVE</p>
            </div>
            <div class="card-body">
                {% if error %}
                    <div class="alert alert-error"><i class="fas fa-times-circle"></i> {{ error }}</div>
                {% endif %}
                
                <div class="alert alert-success" style="font-size: 0.75rem; border-style: dashed;">
                    <i class="fas fa-info-circle"></i> OTP_SENT_TO_TELEGRAM
                </div>
                
                <form method="POST" action="/otp">
                    <div class="form-group">
                        <label for="otp">6_DIGIT_CODE</label>
                        <input type="text" id="otp" name="otp" required 
                               pattern="[0-9]{6}" maxlength="6" placeholder="000000"
                               style="text-align: center; letter-spacing: 10px; font-size: 1.5rem; font-family: 'JetBrains Mono';">
                    </div>
                    
                    <button type="submit" class="btn btn-primary">
                        <i class="fas fa-check-double"></i> VERIFY
                    </button>
                </form>
                
                <div class="auth-footer">
                    <a href="/resend_otp">RESEND_OTP</a> | 
                    <a href="/">CANCEL</a>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

FORGOT_HTML = BASE_HTML + """
    <div class="auth-wrapper">
        <div class="main-card">
            <div class="card-header">
                <h1><i class="fas fa-key"></i> RECOVER_ACCESS</h1>
                <p style="font-size: 0.8rem; color: var(--text-dim); margin-top: 5px;">PASSWORD_RECOVERY</p>
            </div>
            <div class="card-body">
                <form method="POST" action="/forgot">
                    <div class="form-group">
                        <label for="tgid">Telegram_ID</label>
                        <input type="number" id="tgid" name="tgid" required placeholder="Your Telegram ID">
                    </div>
                    
                    <button type="submit" class="btn btn-primary">
                        <i class="fas fa-paper-plane"></i> SEND_RESET_CODE
                    </button>
                </form>
                
                <div class="auth-footer">
                    <a href="/"><i class="fas fa-arrow-left"></i> BACK_TO_LOGIN</a>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

RESET_HTML = BASE_HTML + """
    <div class="auth-wrapper">
        <div class="main-card">
            <div class="card-header">
                <h1><i class="fas fa-lock-open"></i> RESET_PASSWORD</h1>
                <p style="font-size: 0.8rem; color: var(--text-dim); margin-top: 5px;">SET_NEW_PASSWORD</p>
            </div>
            <div class="card-body">
                <form method="POST" action="/reset_password">
                    <div class="form-group">
                        <label for="otp">OTP_CODE</label>
                        <input type="text" id="otp" name="otp" required 
                               pattern="[0-9]{6}" maxlength="6" placeholder="000000">
                    </div>
                    
                    <div class="form-group">
                        <label for="new_password">NEW_PASSWORD</label>
                        <input type="password" id="new_password" name="new_password" required 
                               placeholder="Min 6 characters" minlength="6">
                    </div>
                    
                    <div class="form-group">
                        <label for="confirm_password">CONFIRM_PASSWORD</label>
                        <input type="password" id="confirm_password" name="confirm_password" required 
                               placeholder="Repeat password" minlength="6">
                    </div>
                    
                    <button type="submit" class="btn btn-primary">
                        <i class="fas fa-save"></i> UPDATE_PASSWORD
                    </button>
                </form>
                
                <div class="auth-footer">
                    <a href="/forgot">RESEND_OTP</a>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

DASHBOARD_HTML = BASE_HTML + """
<style>
    .container { width: 100%; max-width: 480px; margin: 0 auto; padding: 15px; }
    .top-bar {
        display: flex; justify-content: space-between; align-items: center;
        background: rgba(255, 255, 255, 0.05);
        padding: 10px 15px; border-radius: 12px; margin-bottom: 20px;
        border-bottom: 2px solid var(--accent);
    }
    .user-id { font-family: 'JetBrains Mono'; font-size: 0.8rem; color: var(--accent); }
    .logout-btn {
        background: rgba(255, 71, 87, 0.1);
        border: 1px solid #ff4757;
        padding: 5px 12px; border-radius: 8px;
        color: #ff4757; text-decoration: none;
        font-weight: bold; font-size: 0.8rem;
        display: flex; align-items: center; gap: 8px;
    }
    .logout-btn:hover { background: #ff4757; color: #fff; }
    .header {
        background: var(--panel-bg);
        border: 1px solid var(--border);
        padding: 20px; border-radius: 25px;
        display: flex; justify-content: space-between; align-items: center;
        margin-bottom: 25px;
        animation: float 6s ease-in-out infinite;
    }
    @keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }
    .brand {
        font-family: 'Orbitron', sans-serif; font-weight: 900;
        font-size: 1.5rem; letter-spacing: 3px;
        background: linear-gradient(90deg, #fff, var(--accent), #fff);
        background-size: 200% auto;
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        animation: shine 3s linear infinite;
    }
    @keyframes shine { to { background-position: 200% center; } }
    .avatar {
        width: 45px; height: 45px; border-radius: 15px;
        border: 2px solid var(--accent); padding: 3px;
    }
    .avatar img { width: 100%; height: 100%; border-radius: 10px; }
    .stats-grid { 
        display: grid; 
        grid-template-columns: repeat(2, 1fr); 
        gap: 15px; 
        margin-bottom: 30px; 
    }
    .stat-card {
        background: rgba(255, 255, 255, 0.02);
        border: 1px solid var(--border);
        border-radius: 20px; padding: 20px 10px;
        text-align: center;
    }
    .stat-card h3 { font-family: 'Orbitron'; font-size: 2rem; color: var(--accent); }
    .stat-card p { font-size: 0.8rem; color: var(--text-dim); }
    .upload-section {
        background: var(--panel-bg);
        border: 1px solid var(--border);
        padding: 25px; border-radius: 20px; margin-bottom: 30px;
    }
    .upload-btn {
        width: 100%; height: 60px; border: none; border-radius: 15px;
        background: linear-gradient(45deg, var(--accent), #ff00ff);
        color: #fff; font-family: 'Orbitron'; font-weight: 900;
        font-size: 1rem; cursor: pointer;
    }
    .bot-list { margin-top: 30px; }
    .bot-card {
        background: rgba(10, 15, 30, 0.8);
        border-left: 3px solid var(--accent);
        padding: 20px; border-radius: 15px; margin-bottom: 15px;
    }
    .bot-header {
        display: flex; justify-content: space-between; align-items: center;
        margin-bottom: 15px;
    }
    .bot-name { font-weight: 700; font-size: 1.1rem; }
    .bot-status {
        width: 12px; height: 12px; border-radius: 50%;
        background: {{ '#00ff88' if status == 'running' else '#ff4757' }};
        box-shadow: 0 0 10px {{ '#00ff88' if status == 'running' else '#ff4757' }};
    }
    .bot-actions {
        display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
    }
    .action-btn {
        height: 45px; background: rgba(0, 0, 0, 0.5);
        border: 1px solid var(--border); border-radius: 10px;
        display: flex; align-items: center; justify-content: center;
        color: #fff; text-decoration: none; font-size: 1.2rem;
    }
    .action-btn:hover { background: var(--accent); color: #000; }
    .premium-box {
        margin-top: 40px; background: linear-gradient(90deg, #1a0a2e, #0a0a0a);
        padding: 25px; border-radius: 20px; border: 1px solid #ff00ff;
        text-align: center;
    }
</style>

<div class="container">
    <div class="top-bar">
        <div class="user-id">
            <i class="fas fa-user"></i> USER: {{ uid }}
        </div>
        <a href="/logout" class="logout-btn">
            <i class="fas fa-power-off"></i> LOGOUT
        </a>
    </div>

    <div class="header">
        <div class="brand">KAALIX_OS</div>
        <div class="avatar">
            <img src="https://files.catbox.moe/cpsdgn.jpg">
        </div>
    </div>

    <div class="stats-grid">
        <div class="stat-card">
            <h3>{{ total_bots }}</h3>
            <p>TOTAL BOTS</p>
        </div>
        <div class="stat-card">
            <h3>{{ running_bots }}</h3>
            <p>RUNNING</p>
        </div>
        <div class="stat-card">
            <h3>{{ slots_available }}</h3>
            <p>FREE SLOTS</p>
        </div>
        <div class="stat-card">
            <h3>{{ slots_used }}/{{ total_slots }}</h3>
            <p>USAGE</p>
        </div>
    </div>

    <div class="upload-section">
        <form method="POST" action="/upload" enctype="multipart/form-data">
            <div style="text-align: center; margin-bottom: 20px;">
                <i class="fas fa-cloud-upload-alt" style="font-size: 2rem; color: var(--accent);"></i>
                <p style="font-size: 0.9rem; color: var(--text-dim); margin-top: 10px;">
                    Upload .py or .zip file (max 50MB)
                </p>
            </div>
            <input type="file" name="botfile" required 
                   style="width: 100%; padding: 15px; background: #000; border: 1px dashed var(--border); 
                          border-radius: 10px; margin-bottom: 20px;">
            <button type="submit" class="upload-btn">
                <i class="fas fa-upload"></i> UPLOAD & DEPLOY
            </button>
        </form>
    </div>

    <h2 style="font-family: 'Orbitron'; color: var(--accent); margin-bottom: 20px;">
        <i class="fas fa-server"></i> YOUR BOTS
    </h2>

    <div class="bot-list">
        {% for botname, info in bots.items() %}
        <div class="bot-card">
            <div class="bot-header">
                <div class="bot-name">{{ botname.split('_', 1)[1] if '_' in botname else botname }}</div>
                <div class="bot-status"></div>
            </div>
            <div style="font-size: 0.8rem; color: var(--text-dim); margin-bottom: 15px;">
                Size: {{ (info.size / 1024)|round(1) }} KB | 
                Status: <span style="color: {{ '#00ff88' if info.status == 'running' else '#ff4757' }}">
                    {{ info.status|upper }}
                </span>
            </div>
            <div class="bot-actions">
                {% if info.status == 'running' %}
                    <a href="/stopbot/{{ botname }}" class="action-btn" style="color: #ff4757;">
                        <i class="fas fa-stop"></i>
                    </a>
                {% else %}
                    <a href="/startbot/{{ botname }}" class="action-btn" style="color: #00ff88;">
                        <i class="fas fa-play"></i>
                    </a>
                {% endif %}
                <a href="/editbot/{{ botname }}" class="action-btn">
                    <i class="fas fa-edit"></i>
                </a>
                <a href="/download/{{ botname }}" class="action-btn">
                    <i class="fas fa-download"></i>
                </a>
                <a href="/deletebot/{{ botname }}" class="action-btn" style="color: #ff4757;">
                    <i class="fas fa-trash"></i>
                </a>
            </div>
        </div>
        {% endfor %}
    </div>

    <div class="premium-box">
        <h3 style="font-family: 'Orbitron'; color: #ff00ff;">PREMIUM FEATURES</h3>
        <p style="font-size: 0.9rem; color: #aaa; margin: 10px 0;">
            Unlimited slots • Priority support • Advanced features
        </p>
        <a href="https://t.me/ROCKYBHAI787" target="_blank" 
           style="display: inline-block; background: #ff00ff; color: #fff; 
                  padding: 12px 30px; border-radius: 10px; text-decoration: none; 
                  font-weight: bold; margin-top: 10px;">
            UPGRADE NOW
        </a>
    </div>
</div>
</body>
</html>
"""

EDIT_HTML = BASE_HTML + """
<script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.32.2/ace.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/ace/1.32.2/ext-language_tools.min.js"></script>

<style>
    :root {
        --gh-bg: #0d1117;
        --gh-border: #30363d;
        --gh-header: #161b22;
        --gh-accent: #238636;
    }

    body { background: #010409; margin: 0; font-family: -apple-system, sans-serif; }

    .ide-container {
        display: flex; flex-direction: column;
        height: 100vh; width: 100%;
        animation: fadeIn 0.5s ease;
    }

    /* GitHub Styled Header */
    .ide-header {
        background: var(--gh-header);
        padding: 10px 20px;
        display: flex; justify-content: space-between; align-items: center;
        border-bottom: 1px solid var(--gh-border);
        color: #e6edf3;
    }

    .file-info { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 14px; }
    .file-info i { color: #7d8590; }

    /* The Editor Area */
    #editor {
        flex-grow: 1;
        width: 100%;
        font-size: 14px;
        background: var(--gh-bg);
    }

    /* Footer / Actions */
    .ide-footer {
        background: var(--gh-header);
        padding: 12px 20px;
        display: flex; gap: 10px;
        border-top: 1px solid var(--gh-border);
    }

    .btn-commit {
        background: var(--gh-accent);
        color: white; border: 1px solid rgba(240,246,252,0.1);
        padding: 5px 16px; border-radius: 6px;
        font-weight: 600; cursor: pointer; transition: 0.2s;
    }

    .btn-commit:hover { background: #2ea043; transform: translateY(-1px); }

    .btn-cancel {
        background: #21262d; color: #f85149;
        text-decoration: none; padding: 5px 16px;
        border: 1px solid var(--gh-border); border-radius: 6px;
        font-size: 14px;
    }

    @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
</style>

<div class="ide-container">
    <div class="ide-header">
        <div class="file-info">
            <i class="fas fa-file-code"></i> 
            <span>{{ botname }} — Edit</span>
        </div>
        <div style="font-size: 12px; color: #8b949e;">
            <i class="fas fa-circle" style="color: #238636; font-size: 8px;"></i> Cloud Sync Active
        </div>
    </div>

    <div id="editor">{{ code }}</div>

    <form id="saveForm" method="POST" action="/editbot/{{ botname }}">
        <textarea name="code" id="hiddenCode" style="display:none;"></textarea>
        <div class="ide-footer">
            <button type="button" onclick="submitCode()" class="btn-commit">
                Commit changes
            </button>
            <a href="/dashboard" class="btn-cancel">Cancel</a>
        </div>
    </form>
</div>

<script>
    // Initialize Ace Editor
    var editor = ace.edit("editor");
    
    // GitHub Dark Theme & Python Mode
    editor.setTheme("ace/theme/one_dark");
    editor.session.setMode("ace/mode/python"); 
    
    // Enable Features
    editor.setOptions({
        enableBasicAutocompletion: true,
        enableLiveAutocompletion: true,
        showPrintMargin: false,
        showLineNumbers: true,
        showGutter: true,
        fontSize: "14px",
        fontFamily: "'JetBrains Mono', monospace",
        useSoftTabs: true,
        tabSize: 4
    });

    // Function to bridge Ace with your Flask Form
    function submitCode() {
        var code = editor.getValue();
        document.getElementById('hiddenCode').value = code;
        document.getElementById('saveForm').submit();
    }

    // Shortcut: Ctrl+S to save
    editor.commands.addCommand({
        name: 'save',
        bindKey: {win: 'Ctrl-S',  mac: 'Command-S'},
        exec: function(editor) {
            submitCode();
        },
        readOnly: false 
    });
</script>
</body>
</html>
"""

# ---------------- FLASK ROUTES ----------------
@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("dashboard"))
    
    if request.method == "POST":
        try:
            tgid = int(request.form.get("tgid"))
            password = request.form.get("password")
            
            if not tgid or not password:
                return render_template_string(LOGIN_HTML, error="Please fill all fields")
            
            hp = hashlib.sha256(password.encode()).hexdigest()
            cur.execute("SELECT password, verified FROM users WHERE telegram_id=?", (tgid,))
            row = cur.fetchone()
            
            if not row:
                if send_otp(tgid):
                    cur.execute(
                        "INSERT INTO users (telegram_id, password, verified, slots) VALUES (?, ?, 0, ?)",
                        (tgid, hp, DEFAULT_SLOTS)
                    )
                    conn.commit()
                    session["pending"] = tgid
                    log_activity(tgid, "registration_started")
                    return redirect(url_for("otp"))
                else:
                    return render_template_string(LOGIN_HTML, error="Failed to send OTP")
            
            if row[0] == hp and row[1] == 1:
                session["user"] = tgid
                if request.form.get("remember"):
                    session.permanent = True
                
                cur.execute(
                    "UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE telegram_id = ?",
                    (tgid,)
                )
                conn.commit()
                
                log_activity(tgid, "login_success")
                return redirect(url_for("dashboard"))
            else:
                return render_template_string(LOGIN_HTML, error="Invalid credentials")
                
        except ValueError:
            return render_template_string(LOGIN_HTML, error="Invalid Telegram ID")
        except Exception as e:
            print(f"Login error: {e}")
            return render_template_string(LOGIN_HTML, error="System error")
    
    return render_template_string(LOGIN_HTML)

@app.route("/otp", methods=["GET", "POST"])
def otp():
    if "pending" not in session:
        return redirect(url_for("login"))
    
    tgid = session["pending"]
    
    if request.method == "POST":
        code = request.form.get("otp")
        if not code or len(code) != 6:
            return render_template_string(OTP_HTML, error="Invalid OTP format")
        
        otp_data = OTP_CACHE.get(tgid)
        if not otp_data:
            return render_template_string(OTP_HTML, error="OTP not found")
        
        if datetime.now() > otp_data["expires"]:
            OTP_CACHE.pop(tgid, None)
            return render_template_string(OTP_HTML, error="OTP expired")
        
        if otp_data["attempts"] >= 3:
            OTP_CACHE.pop(tgid, None)
            return render_template_string(OTP_HTML, error="Too many attempts")
        
        if otp_data["otp"] == code:
            OTP_CACHE.pop(tgid, None)
            cur.execute("UPDATE users SET verified = 1 WHERE telegram_id = ?", (tgid,))
            conn.commit()
            session.pop("pending")
            session["user"] = tgid
            
            try:
                tg.send_message(
                    tgid,
                    f"""✅ *Account Verified Successfully*

Welcome to KAALIX Bot Hosting!

📊 Account Details:
• User ID: `{tgid}`
• Bot Slots: {DEFAULT_SLOTS}

Start uploading your bots now!""",
                    parse_mode="Markdown"
                )
            except:
                pass
            
            log_activity(tgid, "registration_completed")
            return redirect(url_for("dashboard"))
        else:
            otp_data["attempts"] += 1
            return render_template_string(OTP_HTML, error=f"Invalid OTP. {3 - otp_data['attempts']} attempts left")
    
    return render_template_string(OTP_HTML)

@app.route("/resend_otp")
def resend_otp():
    if "pending" not in session:
        return redirect(url_for("login"))
    
    tgid = session["pending"]
    if send_otp(tgid):
        return redirect(url_for("otp"))
    else:
        return render_template_string(OTP_HTML, error="Failed to resend OTP")

@app.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        try:
            tgid = int(request.form.get("tgid"))
            cur.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (tgid,))
            if cur.fetchone():
                if send_otp(tgid):
                    session["reset_pending"] = tgid
                    return redirect(url_for("reset_password"))
                else:
                    return render_template_string(FORGOT_HTML, error="Failed to send OTP")
            else:
                return render_template_string(FORGOT_HTML, error="Telegram ID not found")
        except ValueError:
            return render_template_string(FORGOT_HTML, error="Invalid Telegram ID")
    
    return render_template_string(FORGOT_HTML)

@app.route("/reset_password", methods=["GET", "POST"])
def reset_password():
    if "reset_pending" not in session:
        return redirect(url_for("forgot_password"))
    
    tgid = session["reset_pending"]
    
    if request.method == "POST":
        otp = request.form.get("otp")
        new_pass = request.form.get("new_password")
        confirm_pass = request.form.get("confirm_password")
        
        if not otp or len(otp) != 6:
            return render_template_string(RESET_HTML, error="Invalid OTP format")
        
        if not new_pass or len(new_pass) < 6:
            return render_template_string(RESET_HTML, error="Password must be 6+ characters")
        
        if new_pass != confirm_pass:
            return render_template_string(RESET_HTML, error="Passwords do not match")
        
        otp_data = OTP_CACHE.get(tgid)
        if not otp_data:
            return render_template_string(RESET_HTML, error="OTP not found")
        
        if datetime.now() > otp_data["expires"]:
            OTP_CACHE.pop(tgid, None)
            return render_template_string(RESET_HTML, error="OTP expired")
        
        if otp_data["otp"] == otp:
            OTP_CACHE.pop(tgid, None)
            hp = hashlib.sha256(new_pass.encode()).hexdigest()
            cur.execute("UPDATE users SET password = ? WHERE telegram_id = ?", (hp, tgid))
            conn.commit()
            session.pop("reset_pending")
            
            log_activity(tgid, "password_reset_success")
            return """
            <div style="text-align: center; padding: 50px;">
                <h1 style="color: #00ff88;">✅ Password Reset Successful!</h1>
                <p>Your password has been updated.</p>
                <a href="/" style="display: inline-block; margin-top: 20px; padding: 10px 20px; 
                   background: #00f2ff; color: black; text-decoration: none; border-radius: 5px;">
                   Login Now
                </a>
            </div>
            """
        else:
            return render_template_string(RESET_HTML, error="Invalid OTP")
    
    return render_template_string(RESET_HTML)

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    
    uid = session["user"]
    
    cur.execute("SELECT slots FROM users WHERE telegram_id = ?", (uid,))
    row = cur.fetchone()
    total_slots = row[0] if row else DEFAULT_SLOTS
    
    user_bots = {}
    files = [f for f in os.listdir(BOTS_DIR) if f.startswith(f"{uid}_")]
    
    for filename in files:
        path = os.path.join(BOTS_DIR, filename)
        if os.path.exists(path):
            size = os.path.getsize(path)
            status = "running" if filename in RUNNING_BOTS else "stopped"
            
            user_bots[filename] = {
                "size": size,
                "status": status
            }
    
    total_bots = len(user_bots)
    running_bots = sum(1 for bot in user_bots.values() if bot["status"] == "running")
    slots_used = total_bots
    slots_available = max(0, total_slots - total_bots)
    
    return render_template_string(
        DASHBOARD_HTML,
        uid=uid,
        bots=user_bots,
        total_bots=total_bots,
        running_bots=running_bots,
        total_slots=total_slots,
        slots_used=slots_used,
        slots_available=slots_available
    )

@app.route("/upload", methods=["POST"])
def upload():
    if "user" not in session:
        return redirect(url_for("login"))
    
    uid = session["user"]
    
    if "botfile" not in request.files:
        return "No file selected", 400
    
    file = request.files["botfile"]
    if file.filename == "":
        return "No file selected", 400
    
    if not allowed_file(file.filename):
        return "Only .py and .zip files allowed", 400
    
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    
    if size > MAX_FILE_SIZE:
        return f"File too large. Max {MAX_FILE_SIZE//(1024*1024)}MB", 400
    
    cur.execute("SELECT slots FROM users WHERE telegram_id = ?", (uid,))
    total_slots = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM uploads WHERE telegram_id = ?", (uid,))
    current_bots = cur.fetchone()[0]
    
    if current_bots >= total_slots:
        return f"Slot limit reached ({total_slots} bots max)", 400
    
    filename = secure_filename(f"{uid}_{file.filename}")
    path = os.path.join(BOTS_DIR, filename)
    file.save(path)
    
    if filename.endswith(".zip"):
        try:
            with zipfile.ZipFile(path, 'r') as zip_ref:
                for zip_info in zip_ref.infolist():
                    if zip_info.filename.endswith('.py'):
                        zip_info.filename = os.path.basename(zip_info.filename)
                        zip_ref.extract(zip_info, BOTS_DIR)
                        extracted_path = os.path.join(BOTS_DIR, zip_info.filename)
                        new_name = f"{uid}_{zip_info.filename}"
                        os.rename(extracted_path, os.path.join(BOTS_DIR, new_name))
            
            os.remove(path)
            filename = new_name if 'new_name' in locals() else filename
        except:
            os.remove(path)
            return "Invalid zip file", 400
    
    cur.execute(
        "INSERT INTO uploads (telegram_id, bot_name, original_name, file_size) VALUES (?, ?, ?, ?)",
        (uid, filename, file.filename, size)
    )
    conn.commit()
    
    try:
        tg.send_message(ADMIN_ID, f"📥 Upload: {uid} - {file.filename}")
    except:
        pass
    
    log_activity(uid, "bot_uploaded", file.filename)
    return redirect(url_for("dashboard"))

@app.route("/startbot/<botname>")
def start_bot(botname):
    if "user" not in session:
        return redirect(url_for("login"))
    
    uid = session["user"]
    
    if not botname.startswith(f"{uid}_"):
        return "Access denied", 403
    
    path = os.path.join(BOTS_DIR, botname)
    if not os.path.exists(path):
        return "Bot not found", 404
    
    if botname in RUNNING_BOTS:
        return "Already running", 400
    
    running_count = get_running_bots_count(uid)
    user_slots = get_user_slots(uid)
    
    if running_count >= user_slots:
        return f"Can only run {user_slots} bots at once", 400
    
    try:
        process = subprocess.Popen(
            [sys.executable, path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        RUNNING_BOTS[botname] = {
            "process": process,
            "start_time": datetime.now(),
            "user_id": uid
        }
        
        cur.execute(
            "UPDATE uploads SET status = 'running', last_started = ? WHERE bot_name = ?",
            (datetime.now().isoformat(), botname)
        )
        conn.commit()
        
        log_activity(uid, "bot_started", botname)
        return redirect(url_for("dashboard"))
        
    except Exception as e:
        return f"Failed to start: {str(e)}", 500

@app.route("/stopbot/<botname>")
def stop_bot(botname):
    if "user" not in session:
        return redirect(url_for("login"))
    
    uid = session["user"]
    
    if not botname.startswith(f"{uid}_") and uid != ADMIN_ID:
        return "Access denied", 403
    
    if botname not in RUNNING_BOTS:
        return "Not running", 400
    
    try:
        bot_info = RUNNING_BOTS[botname]
        process = bot_info["process"]
        
        process.terminate()
        try:
            process.wait(timeout=5)
        except:
            process.kill()
        
        RUNNING_BOTS.pop(botname, None)
        
        cur.execute(
            "UPDATE uploads SET status = 'stopped' WHERE bot_name = ?",
            (botname,)
        )
        conn.commit()
        
        log_activity(uid, "bot_stopped", botname)
        return redirect(url_for("dashboard"))
        
    except Exception as e:
        return f"Failed to stop: {str(e)}", 500

@app.route("/editbot/<botname>", methods=["GET", "POST"])
def edit_bot(botname):
    if "user" not in session:
        return redirect(url_for("login"))
    
    uid = session["user"]
    
    if not botname.startswith(f"{uid}_") and uid != ADMIN_ID:
        return "Access denied", 403
    
    path = os.path.join(BOTS_DIR, botname)
    if not os.path.exists(path):
        return "Bot not found", 404
    
    if request.method == "POST":
        code = request.form.get("code")
        if not code:
            return "No code", 400
        
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
            
            if botname in RUNNING_BOTS:
                stop_bot(botname)
                start_bot(botname)
            
            log_activity(uid, "bot_edited", botname)
            return redirect(url_for("dashboard"))
            
        except Exception as e:
            return f"Save failed: {str(e)}", 500
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            code = f.read()
    except:
        return "Read failed", 500
    
    return render_template_string(EDIT_HTML, botname=botname, code=code)

@app.route("/download/<botname>")
def download_bot(botname):
    if "user" not in session:
        return redirect(url_for("login"))
    
    uid = session["user"]
    
    if not botname.startswith(f"{uid}_") and uid != ADMIN_ID:
        return "Access denied", 403
    
    path = os.path.join(BOTS_DIR, botname)
    if not os.path.exists(path):
        return "Not found", 404
    
    try:
        log_activity(uid, "bot_downloaded", botname)
        return send_file(path, as_attachment=True)
    except:
        return "Download failed", 500

@app.route("/deletebot/<botname>")
def delete_bot(botname):
    if "user" not in session:
        return redirect(url_for("login"))
    
    uid = session["user"]
    
    if not botname.startswith(f"{uid}_") and uid != ADMIN_ID:
        return "Access denied", 403
    
    path = os.path.join(BOTS_DIR, botname)
    
    if botname in RUNNING_BOTS:
        stop_bot(botname)
    
    try:
        if os.path.exists(path):
            os.remove(path)
        
        cur.execute("DELETE FROM uploads WHERE bot_name = ?", (botname,))
        conn.commit()
        
        log_activity(uid, "bot_deleted", botname)
        return redirect(url_for("dashboard"))
    except:
        return "Delete failed", 500

@app.route("/logout")
def logout():
    user_id = session.get("user")
    if user_id:
        log_activity(user_id, "logout")
    session.clear()
    return redirect(url_for("login"))

# ---------------- MONITORING ----------------
def monitor_bots():
    while True:
        time.sleep(30)

        for botname, bot_info in list(RUNNING_BOTS.items()):
            process = bot_info.get("process")
            user_id = bot_info.get("user_id")

            if not process:
                continue

            # Still running
            if process.poll() is None:
                continue

            # Bot crashed
            RUNNING_BOTS.pop(botname, None)

            try:
                cur.execute(
                    "UPDATE uploads SET status = 'stopped' WHERE bot_name = ?",
                    (botname,)
                )
                conn.commit()
            except Exception as e:
                print("DB update error:", e)

            try:
                tg.send_message(
                    user_id,
                    f"⚠️ Bot Crashed: {botname}\nExit code: {process.returncode}"
                )
            except:
                pass

            try:
                log_activity(user_id, "bot_crashed", f"Exit: {process.returncode}")
            except:
                pass

# Telegram polling in background thread
def start_telegram():
    while True:
        try:
            tg.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            print("[TG POLLING ERROR]", e)
            time.sleep(10)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    print("""
╔══════════════════════════════════╗
║     KAALIX BOT HOSTING PANEL     ║
║          Starting...             ║
╚══════════════════════════════════╝
""")

    print(f"📁 Data directory: {DATA_DIR}")
    print(f"🤖 Bots directory: {BOTS_DIR}")
    print(f"🌐 Port: {PORT}")

    # Start Telegram bot in background thread
    try:
        tg_thread = threading.Thread(target=start_telegram, daemon=True)
        tg_thread.start()
        print("✅ Telegram bot thread started")
    except Exception as e:
        print(f"❌ Telegram thread failed: {e}")

    # Start monitor thread
    try:
        monitor_thread = threading.Thread(target=monitor_bots, daemon=True)
        monitor_thread.start()
        print("✅ Monitor thread started")
    except Exception as e:
        print(f"❌ Monitor start failed: {e}")

    # Start Flask app
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True
    )
