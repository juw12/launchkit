"""
LaunchKit - pre-launch growth app for developers.

  * Viral waitlist: every signup gets a referral link; each friend who joins
    (and confirms their email) moves the referrer up BUMP_PER_REFERRAL spots,
    with milestone rewards.
  * Emails: confirmation (double opt-in), welcome, "a friend joined",
    reward-unlocked, broadcasts with unsubscribe - via Resend, SMTP, or a
    console outbox for local development.
  * Social media manager: AI-written or template build-in-public posts,
    2-week content plan, calendar, live placeholders ({count}, {days_left},
    {link}), per-platform character checks, auto-posting to Bluesky / Discord,
    one-click share for X / LinkedIn / Threads / Reddit.
  * Growth analytics: signups per day, by source (utm), viral K-factor,
    top referrers, CSV export, embeddable widget for any existing website.

Run:   python launchkit.py            (then open http://localhost:8000)
Admin: http://localhost:8000/admin    (password = ADMIN_TOKEN)
Config: environment variables or a .env file next to this script
        (see .env.example). Only the Python standard library is used.
"""

import csv
import hashlib
import html
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path):
    """Minimal .env loader. Real environment variables take precedence."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(os.environ.get("LAUNCHKIT_ENV", os.path.join(SCRIPT_DIR, ".env")))


def env_flag(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", "8000"))
BASE_URL = os.environ.get("BASE_URL", f"http://localhost:{PORT}").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change-me")
DB_PATH = os.path.join(SCRIPT_DIR, os.environ.get("DB_PATH", "launchkit.db"))
BUMP_PER_REFERRAL = int(os.environ.get("BUMP_PER_REFERRAL", "5"))
REQUIRE_VERIFICATION = env_flag("REQUIRE_VERIFICATION", "1")
SECRET_SALT = os.environ.get("SECRET_SALT", "launchkit-salt")
HOST = os.environ.get("HOST", "127.0.0.1")  # use 0.0.0.0 only when a host/container requires it
TRUST_PROXY = env_flag("TRUST_PROXY", "0")  # 1 = behind Caddy/nginx: trust its X-Forwarded-For
TIMEZONE = os.environ.get("TIMEZONE", "").strip()  # IANA name, e.g. Asia/Jakarta; empty = system clock
BACKUP_DIR = os.path.join(SCRIPT_DIR, os.environ.get("BACKUP_DIR", "backups"))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "14"))  # daily backups to keep; 0 = off
SECURE_COOKIES = BASE_URL.startswith("https://")
ADMIN_SESSION_SECONDS = 30 * 24 * 3600


def load_timezone(name):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as error:  # noqa: BLE001
        raise SystemExit(
            f"TIMEZONE={name!r} could not be loaded ({error}). Use an IANA name such as Asia/Jakarta. "
            "On Windows run 'pip install tzdata' first, or leave TIMEZONE empty to use this computer's clock."
        )


APP_TZ = load_timezone(TIMEZONE)


def local_now():
    """Current wall-clock time in TIMEZONE (naive), used for scheduling, stats and timestamps."""
    if APP_TZ is None:
        return datetime.now()
    return datetime.now(APP_TZ).replace(tzinfo=None)

# Email delivery: Resend (if RESEND_API_KEY) > SMTP (if SMTP_HOST) > console outbox
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_API_URL = os.environ.get("RESEND_API_URL", "https://api.resend.com/emails")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_SECURITY = os.environ.get(
    "SMTP_SECURITY", "ssl" if SMTP_PORT == 465 else "starttls" if SMTP_PORT == 587 else "none"
).lower()
EMAIL_FROM = os.environ.get("EMAIL_FROM", "") or SMTP_USER or "onboarding@resend.dev"
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO", "")
EMAIL_SEND_DELAY = float(os.environ.get("EMAIL_SEND_DELAY", "0.6"))  # seconds between sends

# AI writer: any OpenAI-compatible chat-completions API
# (OpenAI, OpenRouter, Groq, Together, local Ollama at http://localhost:11434/v1 ...)
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
AI_ENABLED = bool(AI_API_KEY) or not AI_BASE_URL.startswith("https://api.openai.com")

# Social auto-posting
BSKY_HANDLE = os.environ.get("BSKY_HANDLE", "")
BSKY_APP_PASSWORD = os.environ.get("BSKY_APP_PASSWORD", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

# Referral count -> reward unlocked
MILESTONES = [
    (1, "Early-supporter badge"),
    (3, "Skip ahead + private beta invite"),
    (10, "Founding member: 50% off for life"),
]
# Waitlist sizes that trigger an automatic "we hit N" draft post
SIGNUP_MILESTONES = [10, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

PLATFORM_LIMITS = {
    "x": 280,
    "bluesky": 300,
    "threads": 500,
    "linkedin": 3000,
    "discord": 2000,
    "reddit": 40000,
}
AUTO_POST_PLATFORMS = {"bluesky", "discord"}

DEFAULT_SETTINGS = {
    "product": "MyProduct",
    "tagline": "The fastest way to ship your side project",
    "problem": "spending weeks on boilerplate",
    "audience": "indie developers",
    "launch_date": (local_now() + timedelta(days=60)).strftime("%Y-%m-%d"),
    "top_reward": "lifetime early access",
    "share_image_url": "",  # image shown in link previews on X / LinkedIn / Slack ...
    "post_link_target": "",  # where tracked post links go; empty = waitlist page (set to your app after launch)
    "contact_email": "",  # shown on the privacy page for data requests
    "privacy_url": "",  # your own privacy policy; empty = built-in /privacy page (a template - review it)
}

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
db_lock = threading.RLock()
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row

SUBSCRIBER_COLUMNS = [
    ("verified", "INTEGER NOT NULL DEFAULT 0"),
    ("verify_token", "TEXT"),
    ("access_email_at", "REAL"),
    ("private_token", "TEXT"),
    ("referral_credited", "INTEGER NOT NULL DEFAULT 0"),  # 0 pending, 1 credited, -1 rejected
    ("rewards_sent", "INTEGER NOT NULL DEFAULT 0"),
    ("unsubscribed", "INTEGER NOT NULL DEFAULT 0"),
    ("post_code", "TEXT"),  # which social post brought this signup
]
POST_COLUMNS = [
    ("campaign_id", "INTEGER"),
    ("link_code", "TEXT"),  # tracked short link /go/<link_code>
]


def init_db():
    with db_lock:
        DB.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                code TEXT UNIQUE NOT NULL,
                referred_by TEXT,
                referrals INTEGER NOT NULL DEFAULT 0,
                source TEXT,
                ip_hash TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                body TEXT NOT NULL,
                scheduled_at TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                result TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS milestones_hit (
                size INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                to_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                html TEXT NOT NULL,
                text TEXT NOT NULL,
                kind TEXT NOT NULL,
                unsubscribe_url TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            );
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_code TEXT NOT NULL,
                ip_hash TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY,
                expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER,
                due_date TEXT,
                title TEXT NOT NULL,
                details TEXT,
                action_url TEXT,
                done INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # Upgrade databases created by the first LaunchKit version
        existing = {row["name"] for row in DB.execute("PRAGMA table_info(subscribers)")}
        legacy_database = "verified" not in existing
        for column, definition in SUBSCRIBER_COLUMNS:
            if column not in existing:
                DB.execute(f"ALTER TABLE subscribers ADD COLUMN {column} {definition}")
        if legacy_database:
            # Old signups were already counted and credited; keep them that way
            DB.execute("UPDATE subscribers SET verified = 1, referral_credited = 1")
            for row in DB.execute("SELECT id, referrals FROM subscribers").fetchall():
                reached = max([n for n, _ in MILESTONES if row["referrals"] >= n] or [0])
                DB.execute("UPDATE subscribers SET rewards_sent = ? WHERE id = ?", (reached, row["id"]))
        for row in DB.execute("SELECT id FROM subscribers WHERE private_token IS NULL").fetchall():
            DB.execute("UPDATE subscribers SET private_token = ? WHERE id = ?", (secrets.token_urlsafe(16), row["id"]))
        existing_post_columns = {row["name"] for row in DB.execute("PRAGMA table_info(posts)")}
        for column, definition in POST_COLUMNS:
            if column not in existing_post_columns:
                DB.execute(f"ALTER TABLE posts ADD COLUMN {column} {definition}")
        for row in DB.execute("SELECT id FROM posts WHERE link_code IS NULL").fetchall():
            DB.execute("UPDATE posts SET link_code = ? WHERE id = ?", (secrets.token_urlsafe(5), row["id"]))
        DB.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_link_code ON posts(link_code)")
        DB.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_private_token ON subscribers(private_token)")
        DB.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_verify_token ON subscribers(verify_token)")
        for key, value in DEFAULT_SETTINGS.items():
            DB.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        DB.commit()


def query(sql, params=(), one=False):
    with db_lock:
        rows = DB.execute(sql, params).fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, params=()):
    with db_lock:
        cursor = DB.execute(sql, params)
        DB.commit()
        return cursor


def settings():
    return {row["key"]: row["value"] for row in query("SELECT key, value FROM settings")}


def now_str():
    return local_now().strftime("%Y-%m-%dT%H:%M")


# --------------------------------------------------------------------------
# Admin sessions + login throttling
# --------------------------------------------------------------------------
failed_logins = {}
MAX_FAILED_LOGINS = 5
LOGIN_WINDOW_SECONDS = 900


def session_hash(token):
    # Salting with ADMIN_TOKEN means changing the password logs out every session
    return hashlib.sha256((ADMIN_TOKEN + token).encode()).hexdigest()


def create_admin_session():
    token = secrets.token_urlsafe(32)
    execute("DELETE FROM admin_sessions WHERE expires < ?", (time.time(),))
    execute("INSERT INTO admin_sessions(token_hash, expires) VALUES (?, ?)",
            (session_hash(token), time.time() + ADMIN_SESSION_SECONDS))
    return token


def valid_admin_session(token):
    if not token:
        return False
    row = query("SELECT 1 FROM admin_sessions WHERE token_hash = ? AND expires > ?",
                (session_hash(token), time.time()), one=True)
    return row is not None


def delete_admin_session(token):
    if token:
        execute("DELETE FROM admin_sessions WHERE token_hash = ?", (session_hash(token),))


def login_blocked(ip):
    current = time.time()
    attempts = [stamp for stamp in failed_logins.get(ip, []) if current - stamp < LOGIN_WINDOW_SECONDS]
    failed_logins[ip] = attempts
    return len(attempts) >= MAX_FAILED_LOGINS


def record_failed_login(ip):
    failed_logins.setdefault(ip, []).append(time.time())


# --------------------------------------------------------------------------
# Backups (SQLite online backup - safe while the app is running)
# --------------------------------------------------------------------------
def backup_database():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"launchkit-{local_now():%Y%m%d-%H%M%S-%f}.db")
    target = sqlite3.connect(path)
    with db_lock:
        DB.backup(target)
    target.close()
    backups = sorted(name for name in os.listdir(BACKUP_DIR) if name.startswith("launchkit-") and name.endswith(".db"))
    for old_name in backups[:-BACKUP_KEEP] if BACKUP_KEEP > 0 else []:
        os.remove(os.path.join(BACKUP_DIR, old_name))
    return path


def backup_loop():
    """Makes a backup when the newest one is older than 24 h; checks hourly."""
    while True:
        try:
            existing = [
                os.path.getmtime(os.path.join(BACKUP_DIR, name))
                for name in (os.listdir(BACKUP_DIR) if os.path.isdir(BACKUP_DIR) else [])
                if name.startswith("launchkit-") and name.endswith(".db")
            ]
            if time.time() - max(existing, default=0) > 24 * 3600:
                print("[backup]", backup_database())
        except Exception as error:  # noqa: BLE001
            print("[backup] failed:", error)
        time.sleep(3600)


def esc(value):
    return html.escape(str(value if value is not None else ""))


# --------------------------------------------------------------------------
# Waitlist logic
# --------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
recent_signups_by_ip = {}
ACCESS_EMAIL_COOLDOWN = 120  # seconds between confirmation / "your link" emails


def normalize_email(email):
    email = email.strip().lower()
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    local = local.split("+", 1)[0]  # strip +tags to stop duplicate farming
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
    return f"{local}@{domain}"


def hash_ip(ip):
    return hashlib.sha256((SECRET_SALT + ip).encode()).hexdigest()[:16]


def rate_limited(ip, max_signups=5, window_seconds=600):
    current = time.time()
    stamps = [stamp for stamp in recent_signups_by_ip.get(ip, []) if current - stamp < window_seconds]
    recent_signups_by_ip[ip] = stamps
    if len(stamps) >= max_signups:
        return True
    stamps.append(current)
    return False


def get_subscriber(column, value):
    if column not in ("id", "code", "private_token", "verify_token", "email"):
        raise ValueError(column)
    return query(f"SELECT * FROM subscribers WHERE {column} = ?", (value,), one=True)


def waitlist_count():
    if REQUIRE_VERIFICATION:
        return query("SELECT COUNT(*) AS n FROM subscribers WHERE verified = 1", one=True)["n"]
    return query("SELECT COUNT(*) AS n FROM subscribers", one=True)["n"]


def position_of(subscriber):
    """Rank among confirmed subscribers by (signup order - referrals * bump)."""
    score = subscriber["id"] - subscriber["referrals"] * BUMP_PER_REFERRAL
    visibility = "verified = 1" if REQUIRE_VERIFICATION else "1 = 1"
    row = query(
        f"""
        SELECT COUNT(*) AS ahead FROM subscribers
        WHERE {visibility} AND id != ?
          AND ((id - referrals * ?) < ? OR ((id - referrals * ?) = ? AND id < ?))
        """,
        (subscriber["id"], BUMP_PER_REFERRAL, score, BUMP_PER_REFERRAL, score, subscriber["id"]),
        one=True,
    )
    return row["ahead"] + 1


def ranked_subscribers():
    rows = query("SELECT * FROM subscribers ORDER BY (id - referrals * ?), id", (BUMP_PER_REFERRAL,))
    rank = 0
    ranked = []
    for row in rows:
        counts = bool(row["verified"]) or not REQUIRE_VERIFICATION
        if counts:
            rank += 1
        ranked.append((rank if counts else "", row))
    return ranked


def referral_link(subscriber):
    return f"{BASE_URL}/?ref={subscriber['code']}"


def status_url(subscriber):
    return f"{BASE_URL}/s/{subscriber['private_token']}"


def unsubscribe_url(subscriber):
    return f"{BASE_URL}/unsubscribe/{subscriber['private_token']}"


def join_waitlist(email, name, ref_code, source, ip, post_code=None):
    """Returns (subscriber, outcome, error). outcome: 'new' or 'existing'."""
    email = normalize_email(email or "")
    if len(email) > 254 or not EMAIL_RE.match(email):
        return None, None, "Please enter a valid email address."
    if rate_limited(ip):
        return None, None, "Too many signups from your network. Try again later."

    existing = get_subscriber("email", email)
    if existing:
        # Never reveal someone's spot to whoever types their email - mail it instead
        send_access_email(existing)
        return existing, "existing", None

    referrer = get_subscriber("code", ref_code) if ref_code else None
    if post_code and not query("SELECT 1 FROM posts WHERE link_code = ?", (post_code,), one=True):
        post_code = None
    execute(
        """
        INSERT INTO subscribers(email, name, code, referred_by, source, ip_hash, created_at,
                                verified, verify_token, private_token, post_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email,
            (name or "").strip()[:80],
            secrets.token_urlsafe(6),
            referrer["code"] if referrer else None,
            (source or ("referral" if referrer else "direct"))[:40],
            hash_ip(ip),
            local_now().isoformat(timespec="seconds"),
            0 if REQUIRE_VERIFICATION else 1,
            secrets.token_urlsafe(24),
            secrets.token_urlsafe(16),
            post_code,
        ),
    )
    subscriber = get_subscriber("email", email)
    if REQUIRE_VERIFICATION:
        send_access_email(subscriber, force=True)
    else:
        on_confirmed(subscriber)
    return subscriber, "new", None


def confirm_email(token):
    """Handles the link in the confirmation email. Returns the subscriber or None."""
    subscriber = get_subscriber("verify_token", token) if token else None
    if not subscriber:
        return None
    newly_confirmed = execute(
        "UPDATE subscribers SET verified = 1 WHERE id = ? AND verified = 0", (subscriber["id"],)
    ).rowcount == 1
    if newly_confirmed:
        on_confirmed(get_subscriber("id", subscriber["id"]))
    return get_subscriber("id", subscriber["id"])


def on_confirmed(subscriber):
    queue_email(subscriber["email"], *welcome_email(subscriber), kind="welcome",
                unsubscribe=unsubscribe_url(subscriber))
    credit_referral(subscriber)
    check_signup_milestones()


def credit_referral(subscriber):
    """Referrers are credited only once the friend has confirmed their email."""
    if not subscriber["referred_by"] or subscriber["referral_credited"] != 0:
        return
    referrer = get_subscriber("code", subscriber["referred_by"])
    if not referrer or referrer["ip_hash"] == subscriber["ip_hash"]:
        execute("UPDATE subscribers SET referral_credited = -1 WHERE id = ?", (subscriber["id"],))
        return
    execute("UPDATE subscribers SET referrals = referrals + 1 WHERE id = ?", (referrer["id"],))
    execute("UPDATE subscribers SET referral_credited = 1 WHERE id = ?", (subscriber["id"],))
    notify_referrer(referrer["id"])


def notify_referrer(referrer_id):
    referrer = get_subscriber("id", referrer_id)
    if not referrer or referrer["unsubscribed"]:
        return
    unlocked = [(n, reward) for n, reward in MILESTONES if referrer["referrals"] >= n > referrer["rewards_sent"]]
    if unlocked:
        execute("UPDATE subscribers SET rewards_sent = ? WHERE id = ?", (unlocked[-1][0], referrer_id))
        queue_email(referrer["email"], *reward_email(referrer, unlocked), kind="reward",
                    unsubscribe=unsubscribe_url(referrer))
    else:
        queue_email(referrer["email"], *friend_joined_email(referrer), kind="friend_joined",
                    unsubscribe=unsubscribe_url(referrer))


def send_access_email(subscriber, force=False):
    """Confirmation email (if unconfirmed) or 'here is your link' email, rate limited."""
    last_sent = subscriber["access_email_at"] or 0
    if not force and time.time() - last_sent < ACCESS_EMAIL_COOLDOWN:
        return False
    execute("UPDATE subscribers SET access_email_at = ? WHERE id = ?", (time.time(), subscriber["id"]))
    if REQUIRE_VERIFICATION and not subscriber["verified"]:
        queue_email(subscriber["email"], *confirmation_email(subscriber), kind="confirm")
    else:
        queue_email(subscriber["email"], *your_link_email(subscriber), kind="your_link")
    return True


def check_signup_milestones():
    total = waitlist_count()
    for size in SIGNUP_MILESTONES:
        if total >= size and not query("SELECT 1 FROM milestones_hit WHERE size = ?", (size,), one=True):
            execute("INSERT INTO milestones_hit(size) VALUES (?)", (size,))
            for platform in ("x", "bluesky", "linkedin"):
                body = render_template("milestone", platform, extra={"count": str(size)})
                insert_post(platform, body, None, "draft", f"Auto-drafted: waitlist reached {size}")


# --------------------------------------------------------------------------
# Email content
# --------------------------------------------------------------------------
def email_button(url, label):
    return (
        f'<p style="margin:24px 0"><a href="{esc(url)}" style="background:#5b8cff;color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:600">{esc(label)}</a></p>'
    )


def email_layout(inner_html, footer_html):
    product = settings()["product"]
    return (
        '<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:560px;margin:0 auto;'
        f'padding:24px;color:#1b1f29;line-height:1.5"><h2 style="margin:0 0 16px">{esc(product)}</h2>'
        f'{inner_html}<hr style="border:0;border-top:1px solid #e3e6ec;margin:28px 0 12px">'
        f'<p style="font-size:12px;color:#8b93a5">{footer_html}</p></div>'
    )


def unsubscribe_footer(subscriber):
    return f'You joined the waitlist. <a href="{esc(unsubscribe_url(subscriber))}">Unsubscribe</a>'


def share_block(subscriber):
    link = referral_link(subscriber)
    return (
        f"<p>Your referral link - every friend who joins moves you up {BUMP_PER_REFERRAL} spots:</p>"
        f'<p style="background:#f2f4f8;padding:12px;border-radius:8px;font-family:monospace">{esc(link)}</p>'
    )


def confirmation_email(subscriber):
    product = settings()["product"]
    link = f"{BASE_URL}/verify/{subscriber['verify_token']}"
    subject = f"Confirm your spot on the {product} waitlist"
    html_body = email_layout(
        f"<p>Hi{(' ' + esc(subscriber['name'])) if subscriber['name'] else ''},</p>"
        f"<p>One click to lock in your spot on the {esc(product)} waitlist:</p>"
        f"{email_button(link, 'Confirm my email')}"
        f'<p style="font-size:13px;color:#8b93a5">Or paste this link: {esc(link)}</p>',
        "You received this because this address was entered on our waitlist. If it wasn't you, ignore this email.",
    )
    text = f"Confirm your spot on the {product} waitlist:\n{link}\n\nIf this wasn't you, ignore this email."
    return subject, html_body, text


def welcome_email(subscriber):
    cfg = settings()
    position = position_of(subscriber)
    rewards = "".join(f"<li>{n} invite{'s' if n > 1 else ''}: {esc(r)}</li>" for n, r in MILESTONES)
    subject = f"You're #{position} on the {cfg['product']} waitlist"
    html_body = email_layout(
        f"<p>You're in! You are <b>#{position}</b> in line for {esc(cfg['product'])} - {esc(cfg['tagline'])}.</p>"
        f"{share_block(subscriber)}<p>Rewards for inviting friends:</p><ul>{rewards}</ul>"
        f"{email_button(status_url(subscriber), 'Check my spot')}",
        unsubscribe_footer(subscriber),
    )
    text = (
        f"You're #{position} on the {cfg['product']} waitlist.\n\n"
        f"Share your link to move up {BUMP_PER_REFERRAL} spots per friend:\n{referral_link(subscriber)}\n\n"
        f"Check your spot: {status_url(subscriber)}\nUnsubscribe: {unsubscribe_url(subscriber)}"
    )
    return subject, html_body, text


def friend_joined_email(referrer):
    product = settings()["product"]
    position = position_of(referrer)
    next_goal = next(((n, r) for n, r in MILESTONES if referrer["referrals"] < n), None)
    goal_html = goal_text = ""
    if next_goal:
        remaining = next_goal[0] - referrer["referrals"]
        goal_text = f"{remaining} more invite{'s' if remaining > 1 else ''} unlocks: {next_goal[1]}."
        goal_html = f"<p>{esc(goal_text)}</p>"
    subject = f"A friend joined {product} - you moved up to #{position}"
    html_body = email_layout(
        f"<p>Someone just joined with your link. You're now <b>#{position}</b> "
        f"({referrer['referrals']} friend{'s' if referrer['referrals'] != 1 else ''} referred).</p>"
        f"{goal_html}{share_block(referrer)}{email_button(status_url(referrer), 'See my spot')}",
        unsubscribe_footer(referrer),
    )
    text = (
        f"A friend joined with your link - you're now #{position}.\n{goal_text}\n\n"
        f"Your link: {referral_link(referrer)}\nUnsubscribe: {unsubscribe_url(referrer)}"
    )
    return subject, html_body, text


def reward_email(referrer, unlocked):
    product = settings()["product"]
    position = position_of(referrer)
    newest = unlocked[-1][1]
    items = "".join(f"<li><b>{esc(reward)}</b></li>" for _, reward in unlocked)
    subject = f"You unlocked: {newest}"
    html_body = email_layout(
        f"<p>Thanks for spreading the word about {esc(product)}! With {referrer['referrals']} referrals "
        f"you're now <b>#{position}</b> and you've unlocked:</p><ul>{items}</ul>"
        f"{share_block(referrer)}{email_button(status_url(referrer), 'See my rewards')}",
        unsubscribe_footer(referrer),
    )
    text = (
        f"You unlocked: {', '.join(r for _, r in unlocked)}. You're now #{position}.\n\n"
        f"Your link: {referral_link(referrer)}\nUnsubscribe: {unsubscribe_url(referrer)}"
    )
    return subject, html_body, text


def your_link_email(subscriber):
    product = settings()["product"]
    position = position_of(subscriber)
    subject = f"Your spot on the {product} waitlist (#{position})"
    html_body = email_layout(
        f"<p>You're already on the list - currently <b>#{position}</b>.</p>"
        f"{share_block(subscriber)}{email_button(status_url(subscriber), 'Check my spot')}",
        unsubscribe_footer(subscriber),
    )
    text = f"You're #{position}. Your page: {status_url(subscriber)}\nYour link: {referral_link(subscriber)}"
    return subject, html_body, text


def text_to_html(text):
    linked = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc(text))
    return "<p>" + linked.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


def render_broadcast(template, subscriber):
    values = {
        "name": subscriber["name"] or "there",
        "position": str(position_of(subscriber)),
        "referral_link": referral_link(subscriber),
        "status_link": status_url(subscriber),
        "product": settings()["product"],
    }
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def queue_broadcast(subject, body, test_address=""):
    if test_address:
        sample = query("SELECT * FROM subscribers ORDER BY id LIMIT 1", one=True)
        if sample is None:
            return 0
        queue_email(test_address, "[TEST] " + render_broadcast(subject, sample),
                    email_layout(text_to_html(render_broadcast(body, sample)), unsubscribe_footer(sample)),
                    render_broadcast(body, sample), kind="broadcast_test")
        return 1
    recipients = query("SELECT * FROM subscribers WHERE verified = 1 AND unsubscribed = 0 ORDER BY id")
    for subscriber in recipients:
        personal_body = render_broadcast(body, subscriber)
        queue_email(
            subscriber["email"],
            render_broadcast(subject, subscriber),
            email_layout(text_to_html(personal_body), unsubscribe_footer(subscriber)),
            personal_body + f"\n\nUnsubscribe: {unsubscribe_url(subscriber)}",
            kind="broadcast",
            unsubscribe=unsubscribe_url(subscriber),
        )
    return len(recipients)


# --------------------------------------------------------------------------
# Email delivery
# --------------------------------------------------------------------------
def email_provider():
    if RESEND_API_KEY:
        return "resend"
    if SMTP_HOST:
        return "smtp"
    return "console"


def from_address():
    if "<" in EMAIL_FROM:
        return EMAIL_FROM
    return f"{settings()['product']} <{EMAIL_FROM}>"


def queue_email(to_address, subject, html_body, text_body, kind, unsubscribe=None):
    execute(
        """INSERT INTO emails(to_email, subject, html, text, kind, unsubscribe_url, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)""",
        (to_address, subject, html_body, text_body, kind, unsubscribe, now_str()),
    )


def send_email_now(to_address, subject, html_body, text_body, unsubscribe=None):
    provider = email_provider()
    if provider == "console":
        print(f"\n[email] To: {to_address}\n[email] Subject: {subject}\n{text_body}\n")
        return "console"
    if provider == "resend":
        payload = {
            "from": from_address(),
            "to": [to_address],
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }
        if EMAIL_REPLY_TO:
            payload["reply_to"] = EMAIL_REPLY_TO
        if unsubscribe:
            payload["headers"] = {
                "List-Unsubscribe": f"<{unsubscribe}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            }
        result = http_json(RESEND_API_URL, payload, headers={"Authorization": f"Bearer {RESEND_API_KEY}"})
        return result.get("id", "sent")

    message = EmailMessage()
    message["From"] = from_address()
    message["To"] = to_address
    message["Subject"] = subject
    if EMAIL_REPLY_TO:
        message["Reply-To"] = EMAIL_REPLY_TO
    if unsubscribe:
        message["List-Unsubscribe"] = f"<{unsubscribe}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    if SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    with server:
        if SMTP_SECURITY == "starttls":
            server.starttls(context=ssl.create_default_context())
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)
    return "sent"


def email_worker_loop():
    while True:
        try:
            batch = query("SELECT * FROM emails WHERE status = 'queued' ORDER BY id LIMIT 20")
            if not batch:
                time.sleep(2)
                continue
            for row in batch:
                try:
                    send_email_now(row["to_email"], row["subject"], row["html"], row["text"], row["unsubscribe_url"])
                    execute("UPDATE emails SET status = 'sent', sent_at = ?, error = NULL WHERE id = ?",
                            (now_str(), row["id"]))
                except Exception as error:  # noqa: BLE001 - record any delivery failure
                    execute("UPDATE emails SET status = 'failed', error = ? WHERE id = ?",
                            (str(error)[:300], row["id"]))
                time.sleep(EMAIL_SEND_DELAY)
        except Exception as error:  # noqa: BLE001
            print("[email worker]", error)
            time.sleep(5)


# --------------------------------------------------------------------------
# Content: templates, live placeholders, AI writer
# --------------------------------------------------------------------------
# {link}, {count} and {days_left} stay as live placeholders in stored posts and are
# filled in when the post is shown, shared or published - so numbers are never stale.
LIVE_TOKENS = {"link": "{link}", "count": "{count}", "days_left": "{days_left}"}

TEMPLATES = {
    "announce": "I'm building {product} - {tagline}.\n\nFor {audience} who are tired of {problem}.\n\nJoin the waitlist: {link}",
    "problem": "Honest question for {audience}: how much time do you lose to {problem}?\n\nI'm building {product} to fix that. Early access: {link}",
    "progress": "Building {product} in public - update #{n}\n\nShipped this week:\n- [feature]\n- [fix]\n\n{count} people on the waitlist so far. Join them: {link}",
    "sneak_peek": "Sneak peek of {product} [attach screenshot/GIF]\n\n{tagline}.\n\nWant in before launch? {link}",
    "countdown": "{days_left} days until {product} launches.\n\nWaitlist members get {top_reward}. Grab your spot: {link}",
    "referral_push": "Waitlist tip: every friend you invite to {product} moves you up {bump} spots.\n\nTop referrers get {top_reward}. Your link is on your welcome page: {link}",
    "milestone": "{count} people have joined the {product} waitlist. Thank you!\n\nIf you're one of the {audience} tired of {problem}, there's still room: {link}",
    "lesson": "Lesson from building {product}: [one thing you learned this week]\n\nFollowing along? Join the waitlist: {link}",
    "teaser": "Something I've been building for months launches in {days_left} days: {product}.\n\n{tagline}.\n\nGet early access: {link}",
    "launch_tomorrow": "Tomorrow is launch day for {product}.\n\nWaitlist members get {top_reward} - last chance to join: {link}",
    "launch_day": "{product} is live!\n\n{tagline}. Built for {audience} who are tired of {problem}.\n\nTry it: {link}",
    "thanks": "Launch day recap for {product}: thank you to everyone who tried it [add numbers/highlights].\n\nStill catching up on feedback - keep it coming: {link}",
    "recap": "One week since launching {product}. What worked and what didn't:\n- [lesson 1]\n- [lesson 2]\n\n{link}",
}
AI_INTENTS = {
    "announce": "Announce that you're building the product and invite people to join the waitlist.",
    "problem": "Ask the audience an engaging question about the problem the product solves, then mention the product.",
    "progress": "A build-in-public progress update: what was shipped recently (use the author's notes; otherwise use [placeholders]) and the live waitlist size.",
    "sneak_peek": "Tease a sneak peek of the product; include '[attach screenshot]' so the author remembers to add an image.",
    "countdown": "Launch countdown: days left until launch and what waitlist members get.",
    "referral_push": "Remind waitlist members that inviting friends moves them up the list and unlocks rewards.",
    "milestone": "Celebrate the waitlist size and thank supporters.",
    "lesson": "Share one genuine lesson learned while building (use the author's notes; otherwise a [placeholder]).",
    "teaser": "Tease the upcoming launch: days left and why it matters to the audience.",
    "launch_tomorrow": "Tomorrow is launch day; last call to join the waitlist and get the early-access perk.",
    "launch_day": "Announce that the product is live today and invite people to try it.",
    "thanks": "Day-after thank-you: thank people who tried it; leave [placeholders] for real numbers and highlights.",
    "recap": "One-week-after-launch recap: honest lessons ([placeholders] for specifics) and an invitation to try it.",
    "repurpose": "Turn the source material (a changelog, blog post or notes) into a standalone post native to this platform. Pick the single most interesting point for this audience; don't summarize everything.",
    "free": "Write a post based on the author's notes.",
}
PLATFORM_STYLE = {
    "x": "punchy and conversational, short lines, at most one hashtag (preferably none)",
    "bluesky": "friendly and conversational, no hashtags",
    "threads": "casual and personal",
    "linkedin": "professional but personal storytelling, short paragraphs, clear call to action, up to 3 hashtags at the very end",
    "discord": "community announcement tone; **bold** markdown is fine",
    "reddit": "humble and value-first, not salesy; the first line works as the post title",
}
CONTENT_PLAN = [
    "announce", "problem", "sneak_peek", "progress", "referral_push", "lesson", "countdown",
    "progress", "sneak_peek", "problem", "lesson", "referral_push", "progress", "countdown",
]
# Launch-week campaign: (days relative to launch_date, template/intent)
LAUNCH_SCHEDULE = [(-7, "teaser"), (-3, "sneak_peek"), (-1, "launch_tomorrow"), (0, "launch_day"), (1, "thanks"), (7, "recap")]
LAUNCH_TASKS = [
    (-7, "Prepare launch assets", "3-5 screenshots or a short GIF/video, a one-line pitch, and a first comment explaining why you built {product}."),
    (-5, "Schedule your Product Hunt launch", "Product Hunt's day starts at 12:01 AM Pacific Time - schedule for then. Prepare tagline, gallery and maker comment."),
    (-1, "Draft your Show HN post", "Show HN is for things people can try right away - link the working product, not a waitlist page. Title: 'Show HN: {product} - <what it does>'. Add a first comment with the backstory."),
    (-1, "Set post_link_target to your live product URL", "Settings -> post_link_target. Tracked post links will then send people to the product (clicks are still counted)."),
    (0, "Send the launch email to your waitlist", "Opens the broadcast form pre-filled. Edit the [placeholders] before sending."),
    (0, "Reply to every comment in the first hours", "Early replies on Product Hunt, HN, Reddit and X drive most launch-day reach."),
    (0, "Share in communities you're active in", "Follow each subreddit's / Discord's self-promotion rules and lead with what's useful to them."),
]
LAUNCH_EMAIL_SUBJECT = "{product} is live!"
LAUNCH_EMAIL_BODY = (
    "Hi {name},\n\n{product} is live today. You were #{position} on the waitlist - [your early-access perk].\n\n"
    "Try it: [product link]\n\nIf you like it, sharing it today helps a lot: {referral_link}\n\nThanks for being early!"
)
BOT_UA_RE = re.compile(
    r"bot|crawl|spider|preview|facebookexternalhit|embedly|slurp|whatsapp|telegram|skype|discord|slack|curl|wget|python-requests",
    re.I,
)
ai_jobs = {"pending": 0, "failed": 0}
ai_jobs_lock = threading.Lock()


def tracked_link(platform):
    return f"{BASE_URL}/?utm_source={platform}"


def post_link(link_code):
    return f"{BASE_URL}/go/{link_code}"


def days_until_launch():
    try:
        launch = datetime.strptime(settings().get("launch_date", ""), "%Y-%m-%d")
    except ValueError:
        return "?"
    return max((launch.date() - local_now().date()).days, 0)


def render_live(body, platform, link_code=None):
    replacements = {
        "{link}": post_link(link_code) if link_code else tracked_link(platform),
        "{count}": str(waitlist_count()),
        "{days_left}": str(days_until_launch()),
    }
    for token, value in replacements.items():
        body = body.replace(token, value)
    return body


def render_template(name, platform, extra=None):
    values = dict(settings())
    values.update(LIVE_TOKENS)
    values.update(
        {
            "bump": str(BUMP_PER_REFERRAL),
            "n": str(query("SELECT COUNT(*) AS n FROM posts WHERE body LIKE '%update #%'", one=True)["n"] + 1),
        }
    )
    values.update(extra or {})
    return TEMPLATES[name].format(**values)


def clean_ai_text(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def ai_complete(system_prompt, user_prompt):
    if not AI_ENABLED:
        raise RuntimeError("AI is off - set AI_API_KEY (and optionally AI_BASE_URL / AI_MODEL)")
    headers = {"Authorization": f"Bearer {AI_API_KEY}"} if AI_API_KEY else {}
    result = http_json(
        f"{AI_BASE_URL}/chat/completions",
        {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        headers=headers,
        timeout=120,
    )
    return clean_ai_text(result["choices"][0]["message"]["content"] or "")


def ai_write_post(platform, intent="free", notes="", existing_post=None):
    """Write (or rewrite) a post. Keeps {link}/{count}/{days_left} as live placeholders."""
    cfg = settings()
    limit = PLATFORM_LIMITS[platform]
    target = int(limit * 0.85)
    facts = (
        f"Product: {cfg['product']}\nTagline: {cfg['tagline']}\nAudience: {cfg['audience']}\n"
        f"Problem it solves: {cfg['problem']}\nLaunch date: {cfg['launch_date']}\n"
        f"Referral program: each friend invited moves you up {BUMP_PER_REFERRAL} spots; "
        f"top referrers get {cfg['top_reward']}."
    )
    system_prompt = (
        "You are the social media manager for an indie developer building a product in public before launch. "
        "Write authentic, specific, non-hype posts in the first person singular. Never invent features, metrics, "
        "customers or testimonials that are not in the facts or notes; use [placeholders] for anything the author "
        "must fill in. Output only the post text: no preamble, no quotes, no explanations."
    )
    rules = (
        f"Style for {platform}: {PLATFORM_STYLE[platform]}.\n"
        "Use these placeholder tokens literally; they are filled in automatically when posting:\n"
        "  {link} = the waitlist link (include it exactly once)\n"
        "  {count} = current waitlist size (only if you mention it)\n"
        "  {days_left} = days until launch (only if you mention it)\n"
        f"Hard limit: {limit} characters; aim for under {target}."
    )
    if existing_post:
        user_prompt = f"Improve this {platform} post. Keep its facts, placeholders and intent.\n\nPOST:\n{existing_post}\n\n{rules}\n\nFACTS:\n{facts}"
    else:
        user_prompt = f"Write one {platform} post.\nGoal: {AI_INTENTS.get(intent, AI_INTENTS['free'])}\n"
        if notes:
            user_prompt += f"Author's notes: {notes}\n"
        user_prompt += f"\n{rules}\n\nFACTS:\n{facts}"

    text = ai_complete(system_prompt, user_prompt)
    if "{link}" not in text:
        text = f"{text}\n\n{{link}}"
    if len(render_live(text, platform)) > limit:
        text = ai_complete(
            system_prompt,
            f"Shorten this post to under {target} characters. Keep the token {{link}} exactly once. "
            f"Output only the post.\n\n{text}",
        )
        if "{link}" not in text:
            text = f"{text}\n\n{{link}}"
    return text


def generate_content_plan(platforms, start_day, post_time, use_ai=False):
    campaign_id = create_campaign(f"Build-in-public plan from {start_day:%Y-%m-%d}")
    jobs = []
    for day_offset, template_name in enumerate(CONTENT_PLAN):
        when = f"{(start_day + timedelta(days=day_offset)).strftime('%Y-%m-%d')}T{post_time}"
        for platform in platforms:
            jobs.append({"platform": platform, "intent": template_name, "when": when, "campaign_id": campaign_id})
    return queue_post_jobs(jobs, use_ai)


def create_campaign(name):
    return execute("INSERT INTO campaigns(name, created_at) VALUES (?, ?)", (name.strip()[:120], now_str())).lastrowid


def create_launch_campaign(platforms, post_time, use_ai=False):
    """Posts from launch-7 to launch+7 plus a launch checklist. Raises ValueError on a bad launch_date."""
    cfg = settings()
    launch = datetime.strptime(cfg["launch_date"], "%Y-%m-%d")
    campaign_id = create_campaign(f"Launch week ({cfg['launch_date']})")
    today = local_now().date()
    jobs = []
    for day_offset, intent in LAUNCH_SCHEDULE:
        day = launch + timedelta(days=day_offset)
        if day.date() < today:
            continue
        for platform in platforms:
            jobs.append({"platform": platform, "intent": intent, "when": f"{day:%Y-%m-%d}T{post_time}", "campaign_id": campaign_id})
    for day_offset, title, details in LAUNCH_TASKS:
        action_url = ""
        if title.startswith("Send the launch email"):
            action_url = "/admin?" + urllib.parse.urlencode({"bs": LAUNCH_EMAIL_SUBJECT, "bb": LAUNCH_EMAIL_BODY}) + "#emails"
        execute(
            "INSERT INTO tasks(campaign_id, due_date, title, details, action_url) VALUES (?, ?, ?, ?, ?)",
            (campaign_id, f"{launch + timedelta(days=day_offset):%Y-%m-%d}", title,
             details.replace("{product}", cfg["product"]), action_url),
        )
    return queue_post_jobs(jobs, use_ai)


def repurpose_content(source_text, platforms, campaign_id=None):
    jobs = [
        {"platform": platform, "intent": "repurpose", "when": None, "campaign_id": campaign_id, "notes": source_text[:6000]}
        for platform in platforms
    ]
    return queue_post_jobs(jobs, use_ai=True)


def queue_post_jobs(jobs, use_ai):
    if not use_ai:
        for job in jobs:
            insert_post(job["platform"], render_template(job["intent"], job["platform"]), job["when"],
                        "scheduled" if job["when"] else "draft", "Template", job["campaign_id"])
        return len(jobs)
    with ai_jobs_lock:
        ai_jobs["pending"] += len(jobs)
    threading.Thread(target=run_ai_plan, args=(jobs,), daemon=True).start()
    return len(jobs)


def run_ai_plan(jobs):
    for job in jobs:
        platform = job["platform"]
        try:
            body = ai_write_post(platform, job["intent"], notes=job.get("notes", ""))
            note = f"AI-written ({AI_MODEL})"
        except Exception as error:  # noqa: BLE001 - fall back to template
            with ai_jobs_lock:
                ai_jobs["failed"] += 1
            if job["intent"] not in TEMPLATES:  # e.g. repurpose has no template to fall back to
                print("[ai]", error)
                with ai_jobs_lock:
                    ai_jobs["pending"] -= 1
                continue
            body = render_template(job["intent"], platform)
            note = f"AI failed, used template: {str(error)[:150]}"
        insert_post(platform, body, job["when"], "scheduled" if job["when"] else "draft", note, job["campaign_id"])
        with ai_jobs_lock:
            ai_jobs["pending"] -= 1


def insert_post(platform, body, scheduled_at, status, note=None, campaign_id=None):
    execute(
        """INSERT INTO posts(platform, body, scheduled_at, status, result, created_at, campaign_id, link_code)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (platform, body, scheduled_at, status, note, now_str(), campaign_id, secrets.token_urlsafe(5)),
    )


def share_url(platform, body, link):
    encoded = urllib.parse.quote(body)
    if platform == "x":
        return f"https://twitter.com/intent/tweet?text={encoded}"
    if platform == "linkedin":
        return f"https://www.linkedin.com/feed/?shareActive=true&text={encoded}"
    if platform == "threads":
        return f"https://www.threads.net/intent/post?text={encoded}"
    if platform == "reddit":
        title = urllib.parse.quote(body.splitlines()[0] if body else "")
        return f"https://www.reddit.com/submit?title={title}&url={urllib.parse.quote(link)}"
    if platform == "bluesky":
        return f"https://bsky.app/intent/compose?text={encoded}"
    return ""


# --------------------------------------------------------------------------
# HTTP client + social auto-posting
# --------------------------------------------------------------------------
def http_json(url, payload, headers=None, timeout=30):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "LaunchKit/2.0", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {error.code} from {urllib.parse.urlparse(url).netloc}: {detail}") from None
    return json.loads(raw) if raw else {}


def post_to_bluesky(text):
    if not (BSKY_HANDLE and BSKY_APP_PASSWORD):
        raise RuntimeError("Set BSKY_HANDLE and BSKY_APP_PASSWORD to auto-post")
    session = http_json(
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        {"identifier": BSKY_HANDLE, "password": BSKY_APP_PASSWORD},
    )
    facets = []
    for match in re.finditer(rb"https?://[^\s]+", text.encode()):
        facets.append(
            {
                "index": {"byteStart": match.start(), "byteEnd": match.end()},
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": match.group().decode()}],
            }
        )
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "facets": facets,
    }
    result = http_json(
        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        {"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
        headers={"Authorization": f"Bearer {session['accessJwt']}"},
    )
    return result.get("uri", "posted")


def post_to_discord(text):
    if not DISCORD_WEBHOOK:
        raise RuntimeError("Set DISCORD_WEBHOOK to auto-post")
    http_json(DISCORD_WEBHOOK, {"content": text})
    return "posted"


def publish_post(post):
    text = render_live(post["body"], post["platform"], post["link_code"])
    if post["platform"] == "bluesky":
        return post_to_bluesky(text)
    if post["platform"] == "discord":
        return post_to_discord(text)
    raise RuntimeError("manual")


def scheduler_loop():
    """Every 30 s: publish due posts (auto platforms) or flag them 'due' (manual)."""
    while True:
        try:
            due_posts = query(
                "SELECT * FROM posts WHERE status = 'scheduled' AND scheduled_at <= ?", (now_str(),)
            )
            for post in due_posts:
                if post["platform"] in AUTO_POST_PLATFORMS:
                    try:
                        result = publish_post(post)
                        execute("UPDATE posts SET status='posted', result=? WHERE id=?", (str(result), post["id"]))
                    except Exception as error:  # noqa: BLE001
                        execute("UPDATE posts SET status='failed', result=? WHERE id=?", (str(error)[:300], post["id"]))
                else:
                    execute("UPDATE posts SET status='due', result='Ready - click Share to post' WHERE id=?",
                            (post["id"],))
        except Exception as error:  # noqa: BLE001
            print("[scheduler]", error)
        time.sleep(30)


# --------------------------------------------------------------------------
# HTML pages
# --------------------------------------------------------------------------
CSS = """
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#0b0d12;color:#e7e9ee}
a{color:#8ab4ff}.wrap{max-width:1000px;margin:0 auto;padding:32px 20px}
.hero{text-align:center;padding:60px 0 30px}.hero h1{font-size:44px;margin:0 0 12px}
.hero p{font-size:19px;color:#a9b0bf;margin:0 0 28px}
.card{background:#151924;border:1px solid #242a38;border-radius:14px;padding:22px;margin:14px 0}
input,textarea,select{width:100%;padding:12px;border-radius:10px;border:1px solid #2c3446;background:#0f121a;color:#e7e9ee;font-size:15px;margin:6px 0}
input[type=checkbox]{width:auto}textarea{min-height:110px;font-family:inherit}
button,.btn{display:inline-block;padding:10px 16px;border-radius:10px;border:0;background:#5b8cff;color:#fff;font-weight:600;cursor:pointer;text-decoration:none;font-size:14px;margin:4px 4px 4px 0}
.btn.alt{background:#2a3247}.btn.red{background:#b3404a}.btn.green{background:#2f9e62}.btn.purple{background:#7b5cff}
.form{max-width:460px;margin:0 auto}.big{font-size:56px;font-weight:800;color:#5b8cff;margin:4px 0}
.muted{color:#8b93a5}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px}
.stat .num{font-size:30px;font-weight:700}table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:8px;border-bottom:1px solid #242a38;text-align:left;vertical-align:top}
.pill{padding:2px 9px;border-radius:99px;font-size:12px;background:#2a3247}
.pill.posted,.pill.sent{background:#1f5e3c}.pill.failed{background:#6e2a31}.pill.due,.pill.queued{background:#7a5a12}.pill.scheduled{background:#24427a}
.over{color:#ff6b6b;font-weight:700}.bar{background:#5b8cff;height:14px;border-radius:4px}
nav a{margin-right:16px}pre{white-space:pre-wrap;margin:0;font-family:inherit}
.reward{padding:8px 0;border-bottom:1px solid #242a38}.reward.done{color:#6fe3a1}
.notice{padding:12px 16px;border-radius:10px;background:#24427a;margin:12px auto;max-width:460px}
.notice.ok{background:#1f5e3c}.notice.warn{background:#5e4a12}.notice.wide{max-width:none}
details summary{cursor:pointer;color:#8ab4ff;margin:6px 0}code{background:#0f121a;padding:2px 6px;border-radius:6px}
"""


def page(title, body, head_extra=""):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>{head_extra}<style>{CSS}</style></head>
<body><div class="wrap">{body}</div></body></html>"""


def message_page(title, message_html):
    return page(title, f'<div class="hero"><h1>{esc(title)}</h1><p>{message_html}</p><a class="btn alt" href="/">Back</a></div>')


def landing_page(params, error=""):
    cfg = settings()
    total = waitlist_count()
    ref = esc(params.get("ref", [""])[0])
    source = esc(params.get("utm_source", [""])[0])
    post_code = esc(params.get("utm_content", [""])[0])
    error_html = f'<p class="over">{esc(error)}</p>' if error else ""
    rewards = "".join(f'<div class="reward">Invite {n}: {esc(r)}</div>' for n, r in MILESTONES)
    body = f"""
<div class="hero">
  <div class="muted">Launching {esc(cfg['launch_date'])}</div>
  <h1>{esc(cfg['product'])}</h1>
  <p>{esc(cfg['tagline'])}</p>
  <form class="form" method="post" action="/join">
    <input name="name" placeholder="Your name (optional)">
    <input name="email" type="email" placeholder="you@example.com" required>
    <input type="hidden" name="ref" value="{ref}">
    <input type="hidden" name="source" value="{source}">
    <input type="hidden" name="post" value="{post_code}">
    <button style="width:100%">Join the waitlist</button>
    <p class="muted" style="font-size:13px">By joining you agree to receive emails about {esc(cfg['product'])}. Unsubscribe anytime. <a href="{esc(cfg.get('privacy_url') or '/privacy')}">Privacy</a></p>
    {error_html}
  </form>
  <p class="muted" style="margin-top:18px"><b>{total}</b> {'person has' if total == 1 else 'people have'} already joined</p>
</div>
<div class="card form"><b>Move up the line</b><p class="muted">Every friend who joins with your link moves you up {BUMP_PER_REFERRAL} spots.</p>{rewards}</div>
"""
    # Open Graph / Twitter tags make shared links render as rich previews
    image = cfg.get("share_image_url", "").strip()
    head = (
        f'<meta property="og:title" content="{esc(cfg["product"])}">'
        f'<meta property="og:description" content="{esc(cfg["tagline"])}">'
        f'<meta property="og:url" content="{esc(BASE_URL)}/"><meta property="og:type" content="website">'
        f'<meta name="description" content="{esc(cfg["tagline"])}">'
        f'<meta name="twitter:card" content="{"summary_large_image" if image else "summary"}">'
    )
    if image:
        head += f'<meta property="og:image" content="{esc(image)}"><meta name="twitter:image" content="{esc(image)}">'
    return page(cfg["product"] + " - Join the waitlist", body, head)


def status_page(subscriber, params):
    cfg = settings()
    total = waitlist_count()
    position = position_of(subscriber)
    link = referral_link(subscriber)
    private = esc(subscriber["private_token"])
    pending_friends = query(
        "SELECT COUNT(*) AS n FROM subscribers WHERE referred_by = ? AND referral_credited = 0",
        (subscriber["code"],),
        one=True,
    )["n"]
    share_text = f"I just joined the waitlist for {cfg['product']} - {cfg['tagline']}. Join me: {link}"
    encoded = urllib.parse.quote(share_text)

    notices = ""
    if "confirmed" in params:
        notices += '<div class="notice ok">Email confirmed - your spot is locked in.</div>'
    if "resent" in params:
        notices += '<div class="notice">Confirmation email sent. Check your inbox (and spam folder).</div>'
    if REQUIRE_VERIFICATION and not subscriber["verified"]:
        notices += f"""<div class="notice warn">Please confirm your email to lock in your spot - we sent you a link.
<form method="post" action="/s/{private}/resend" style="margin-top:8px"><button class="btn alt">Resend confirmation email</button></form></div>"""
    if subscriber["unsubscribed"]:
        notices += '<div class="notice">You have unsubscribed from updates. You keep your spot.</div>'

    rewards = "".join(
        f'<div class="reward {"done" if subscriber["referrals"] >= n else ""}">'
        f'{"✓" if subscriber["referrals"] >= n else "○"} {n} invite{"s" if n > 1 else ""}: {esc(r)}</div>'
        for n, r in MILESTONES
    )
    next_goal = next((n for n, _ in MILESTONES if subscriber["referrals"] < n), None)
    next_text = (
        f"Invite {next_goal - subscriber['referrals']} more to unlock the next reward."
        if next_goal else "You've unlocked every reward. Legend."
    )
    pending_text = (
        f"<br>{pending_friends} friend{'s' if pending_friends != 1 else ''} still need to confirm their email."
        if pending_friends else ""
    )
    greeting = f", {esc(subscriber['name'])}" if subscriber["name"] else ""
    body = f"""
{notices}
<div class="hero">
  <div class="muted">You're on the {esc(cfg['product'])} waitlist{greeting}!</div>
  <div class="big">#{position}</div>
  <div class="muted">out of {max(total, position)} &middot; {subscriber['referrals']} friend{'s' if subscriber['referrals'] != 1 else ''} referred{pending_text}</div>
</div>
<div class="card form">
  <b>Your referral link</b>
  <input id="reflink" value="{esc(link)}" readonly onclick="this.select()">
  <button onclick="navigator.clipboard.writeText(document.getElementById('reflink').value);this.innerText='Copied!'">Copy link</button>
  <p class="muted">Each friend who joins and confirms moves you up {BUMP_PER_REFERRAL} spots. {next_text}</p>
  <a class="btn alt" target="_blank" href="https://twitter.com/intent/tweet?text={encoded}">Share on X</a>
  <a class="btn alt" target="_blank" href="https://www.linkedin.com/feed/?shareActive=true&text={encoded}">LinkedIn</a>
  <a class="btn alt" target="_blank" href="https://bsky.app/intent/compose?text={encoded}">Bluesky</a>
  <a class="btn alt" target="_blank" href="https://wa.me/?text={encoded}">WhatsApp</a>
  <a class="btn alt" href="mailto:?subject={urllib.parse.quote(cfg['product'])}&body={encoded}">Email</a>
</div>
<div class="card form"><b>Rewards</b>{rewards}</div>
<p class="muted" style="text-align:center">Bookmark this private page to check your position (don't share it - share your referral link instead).</p>
"""
    return page("Your spot - " + cfg["product"], body)


def privacy_page():
    cfg = settings()
    product = esc(cfg['product'])
    contact = esc(cfg.get('contact_email') or from_address())
    sender = {'resend': 'Resend', 'smtp': 'our email provider', 'console': 'our email provider'}[email_provider()]
    body = f"""<div class="card" style="max-width:720px;margin:40px auto"><h1>{product} privacy notice</h1>
<p><b>What we collect when you join the waitlist:</b> your email address, your name if you give it, which referral link
or social post brought you, and a one-way hash of your IP address (used only to prevent abuse).</p>
<p><b>When you click a link we posted on social media:</b> we record the time and a one-way hash of your IP address, and
set a cookie (<code>lk_post</code>, 30 days) so we know which post brought you if you sign up later.</p>
<p><b>Why:</b> to run the waitlist and referral program, send you the confirmation email and updates about {product},
and measure which posts work.</p>
<p><b>Who processes it:</b> emails are delivered through {sender}. We don't sell your data.</p>
<p><b>Your choices:</b> every update email has an unsubscribe link. To see or delete your data, contact {contact}.</p>
</div>"""
    return page(f"{cfg['product']} - Privacy", body)


def unsubscribe_page(subscriber, done=False):
    if done or subscriber["unsubscribed"]:
        return message_page("Unsubscribed", "You won't receive further updates. You keep your spot on the waitlist.")
    body = f"""<div class="hero"><h1>Unsubscribe?</h1><p>Stop receiving waitlist updates at {esc(subscriber['email'])}.</p>
<form method="post" action="/unsubscribe/{esc(subscriber['private_token'])}"><button class="btn red">Unsubscribe</button></form></div>"""
    return page("Unsubscribe", body)


def admin_login_page(error=""):
    body = f"""<div class="card form" style="margin-top:80px"><h2>LaunchKit admin</h2>
<form method="post" action="/admin/login"><input type="password" name="token" placeholder="Admin token">
<button>Log in</button></form><p class="over">{esc(error)}</p></div>"""
    return page("Admin login", body)


def admin_page(message="", prefill_subject="", prefill_body=""):
    cfg = settings()
    total = waitlist_count()
    pending_confirmation = query("SELECT COUNT(*) AS n FROM subscribers WHERE verified = 0", one=True)["n"]
    credited = query("SELECT COUNT(*) AS n FROM subscribers WHERE referral_credited = 1", one=True)["n"]
    today = query(
        "SELECT COUNT(*) AS n FROM subscribers WHERE created_at >= ?", (local_now().strftime("%Y-%m-%d"),), one=True
    )["n"]
    sharers = query("SELECT COUNT(*) AS n FROM subscribers WHERE referrals > 0", one=True)["n"]
    unsubscribed = query("SELECT COUNT(*) AS n FROM subscribers WHERE unsubscribed = 1", one=True)["n"]
    k_factor = credited / max(total - credited, 1)

    daily = query(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM subscribers GROUP BY day ORDER BY day DESC LIMIT 14"
    )
    max_daily = max([row["n"] for row in daily] or [1])
    daily_html = "".join(
        f'<tr><td>{row["day"]}</td><td style="width:70%"><div class="bar" style="width:{100 * row["n"] / max_daily:.0f}%"></div></td><td>{row["n"]}</td></tr>'
        for row in reversed(daily)
    ) or '<tr><td class="muted">No signups yet</td></tr>'
    sources = query("SELECT COALESCE(source,'direct') AS s, COUNT(*) AS n FROM subscribers GROUP BY s ORDER BY n DESC")
    sources_html = "".join(f"<tr><td>{esc(r['s'])}</td><td>{r['n']}</td></tr>" for r in sources) or '<tr><td class="muted">-</td></tr>'
    top = query("SELECT email, referrals FROM subscribers WHERE referrals > 0 ORDER BY referrals DESC LIMIT 10")
    top_html = "".join(f"<tr><td>{esc(r['email'])}</td><td>{r['referrals']}</td></tr>" for r in top) or '<tr><td class="muted">No referrals yet</td></tr>'

    # ---- posts ----
    clicks_by_post, signups_by_post = post_stats()
    campaign_names = {row["id"]: row["name"] for row in query("SELECT id, name FROM campaigns")}
    posts = query(
        """SELECT * FROM posts ORDER BY
           CASE status WHEN 'due' THEN 0 WHEN 'failed' THEN 1 WHEN 'draft' THEN 2 WHEN 'scheduled' THEN 3 ELSE 4 END,
           scheduled_at, id LIMIT 200"""
    )
    post_rows = []
    for post in posts:
        rendered = render_live(post["body"], post["platform"], post["link_code"])
        limit = PLATFORM_LIMITS.get(post["platform"], 99999)
        length_html = f'<span class="{"over" if len(rendered) > limit else "muted"}">{len(rendered)}/{limit}</span>'
        performance = (
            f'<br><span class="muted">{clicks_by_post.get(post["link_code"], 0)} clicks &middot; '
            f'{signups_by_post.get(post["link_code"], 0)} signups</span>'
        )
        campaign_html = (
            f'<br><span class="muted">{esc(campaign_names[post["campaign_id"]])}</span>'
            if post["campaign_id"] in campaign_names else ""
        )
        actions = ""
        if post["status"] != "posted":
            if post["platform"] in AUTO_POST_PLATFORMS:
                actions += f'<form method="post" action="/admin/post/{post["id"]}/publish" style="display:inline"><button class="btn green">Post now</button></form>'
            else:
                actions += f'<a class="btn green" target="_blank" href="{esc(share_url(post["platform"], rendered, post_link(post["link_code"])))}">Share</a>'
                actions += f'<form method="post" action="/admin/post/{post["id"]}/done" style="display:inline"><button class="btn alt">Mark posted</button></form>'
            if AI_ENABLED:
                actions += f'<form method="post" action="/admin/post/{post["id"]}/ai" style="display:inline"><button class="btn purple">AI rewrite</button></form>'
        actions += f'<form method="post" action="/admin/post/{post["id"]}/delete" style="display:inline"><button class="btn red">Delete</button></form>'
        edit_form = f"""<details><summary>Edit</summary><form method="post" action="/admin/post/{post['id']}/edit">
<textarea name="body">{esc(post['body'])}</textarea><input type="datetime-local" name="scheduled_at" value="{esc(post['scheduled_at'] or '')}">
<button class="btn alt">Save</button></form></details>"""
        post_rows.append(
            f"""<tr><td><b>{esc(post['platform'])}</b><br><span class="pill {esc(post['status'])}">{esc(post['status'])}</span>
<br><span class="muted">{esc(post['scheduled_at'] or 'unscheduled')}</span>{campaign_html}{performance}</td>
<td><pre>{esc(rendered)}</pre>{length_html} <span class="muted">{esc(post['result'] or '')}</span>{edit_form}</td>
<td style="white-space:nowrap">{actions}</td></tr>"""
        )
    posts_html = "".join(post_rows) or '<tr><td class="muted">No posts yet - generate a content plan above.</td></tr>'

    platform_checks = "".join(
        f'<label style="margin-right:12px"><input type="checkbox" name="platforms" value="{p}" {"checked" if p in ("x", "bluesky", "linkedin") else ""}> {p}</label>'
        for p in PLATFORM_LIMITS
    )
    template_options = "".join(f'<option value="{t}">{t}</option>' for t in TEMPLATES)
    platform_options = "".join(f'<option value="{p}">{p}</option>' for p in PLATFORM_LIMITS)
    ai_status = f"AI writer: <b>on</b> ({esc(AI_MODEL)})" if AI_ENABLED else "AI writer: off (set AI_API_KEY)"
    with ai_jobs_lock:
        pending_ai, failed_ai = ai_jobs["pending"], ai_jobs["failed"]
    ai_progress = (
        f'<div class="notice wide">AI is writing {pending_ai} more post(s) in the background - refresh to see them.</div>'
        if pending_ai else ""
    )
    if failed_ai:
        ai_progress += f'<div class="notice warn wide">{failed_ai} AI post(s) fell back to templates - see the note under each post.</div>'
    ai_checkbox = (
        '<label><input type="checkbox" name="use_ai" checked> Write with AI</label>' if AI_ENABLED else ""
    )
    integrations = (
        f"{ai_status} &middot; Bluesky auto-post: {'<b>on</b>' if BSKY_HANDLE and BSKY_APP_PASSWORD else 'off'} &middot; "
        f"Discord auto-post: {'<b>on</b>' if DISCORD_WEBHOOK else 'off'} &middot; X / LinkedIn / Threads / Reddit: one-click share"
    )

    # ---- emails ----
    provider = email_provider()
    provider_html = {
        "resend": f"Resend, from <code>{esc(from_address())}</code>",
        "smtp": f"SMTP {esc(SMTP_HOST)}:{SMTP_PORT}, from <code>{esc(from_address())}</code>",
        "console": "<b>Console only</b> - emails are printed in the terminal and listed below, not delivered. Set RESEND_API_KEY or SMTP_HOST.",
    }[provider]
    email_counts = {row["status"]: row["n"] for row in query("SELECT status, COUNT(*) AS n FROM emails GROUP BY status")}
    email_rows = []
    for row in query("SELECT * FROM emails ORDER BY id DESC LIMIT 25"):
        preview = f"<details><summary>view</summary><pre>{esc(row['text'])}</pre></details>" if provider == "console" else ""
        email_rows.append(
            f"<tr><td>{esc(row['created_at'])}</td><td>{esc(row['to_email'])}</td><td>{esc(row['kind'])}</td>"
            f"<td>{esc(row['subject'])}{preview}<div class='over'>{esc(row['error'] or '')}</div></td>"
            f"<td><span class='pill {esc(row['status'])}'>{esc(row['status'])}</span></td></tr>"
        )
    emails_html = "".join(email_rows) or '<tr><td class="muted">No emails yet</td></tr>'
    broadcast_audience = query(
        "SELECT COUNT(*) AS n FROM subscribers WHERE verified = 1 AND unsubscribed = 0", one=True
    )["n"]

    settings_inputs = "".join(f'<label class="muted">{k}</label><input name="{k}" value="{esc(v)}">' for k, v in cfg.items())
    tomorrow = (local_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    flash = f'<div class="notice ok wide">{esc(message)}</div>' if message else ""

    body = f"""
<nav><b>LaunchKit</b> &nbsp; <a href="/" target="_blank">Landing page</a><a href="/admin/marketing">Marketing</a><a href="/admin/export.csv">Export CSV</a><a href="/admin/backup.db">Download backup</a>
<a href="#social">Social</a><a href="#emails">Emails</a><a href="#settings">Settings</a><a href="/admin/logout">Log out</a></nav>
{flash}
<h2>{esc(cfg['product'])} pre-launch dashboard</h2>
<div class="grid">
 <div class="card stat"><div class="muted">Waitlist{' (confirmed)' if REQUIRE_VERIFICATION else ''}</div><div class="num">{total}</div></div>
 <div class="card stat"><div class="muted">Awaiting confirmation</div><div class="num">{pending_confirmation}</div></div>
 <div class="card stat"><div class="muted">Signups today</div><div class="num">{today}</div></div>
 <div class="card stat"><div class="muted">Via referrals</div><div class="num">{credited}</div></div>
 <div class="card stat"><div class="muted">Viral K-factor</div><div class="num">{k_factor:.2f}</div><div class="muted">&gt;1 = self-sustaining</div></div>
 <div class="card stat"><div class="muted">Active sharers</div><div class="num">{sharers}</div></div>
</div>
<div class="grid">
 <div class="card"><b>Signups / day</b><table>{daily_html}</table></div>
 <div class="card"><b>By source</b><table>{sources_html}</table></div>
 <div class="card"><b>Top referrers</b><table>{top_html}</table></div>
</div>

<h2 id="social">Social media manager</h2>
<p class="muted">{integrations}</p>{ai_progress}
<div class="grid2">
<div class="card"><b>Generate 2-week build-in-public plan</b>
<form method="post" action="/admin/plan">{platform_checks}
<label class="muted">Start date</label><input type="date" name="start" value="{tomorrow}">
<label class="muted">Post time</label><input type="time" name="time" value="09:00">
{ai_checkbox}<br><button>Generate {len(CONTENT_PLAN)}-day plan</button></form></div>
<div class="card"><b>Compose a post</b>
<form method="post" action="/admin/compose">
<select name="platform">{platform_options}</select>
<select name="template"><option value="free">free-form</option>{template_options}</select>
<select name="campaign_id">{campaign_options()}</select>
<textarea name="body" placeholder="Your post - or, with AI on, notes like 'shipped dark mode + Stripe billing today'. Live placeholders: {{link}} {{count}} {{days_left}}"></textarea>
<input type="datetime-local" name="scheduled_at">
{ai_checkbox}<br><button>Save post</button></form></div>
</div>
<div class="card"><table><tr><th>Platform</th><th>Post (as it will be published)</th><th></th></tr>{posts_html}</table></div>

<h2 id="emails">Emails</h2>
<p class="muted">Delivery: {provider_html}<br>Confirmation before referrals count: <b>{'on' if REQUIRE_VERIFICATION else 'off'}</b>
&middot; queued {email_counts.get('queued', 0)} &middot; sent {email_counts.get('sent', 0)} &middot; failed {email_counts.get('failed', 0)} &middot; unsubscribed {unsubscribed}</p>
<div class="grid2">
<div class="card"><b>Broadcast to the waitlist</b> <span class="muted">({broadcast_audience} confirmed, subscribed)</span>
<form method="post" action="/admin/broadcast">
<input name="subject" value="{esc(prefill_subject)}" placeholder="Subject, e.g. {{product}} is live - you're #{{position}}" required>
<textarea name="body" required placeholder="Hi {{name}},&#10;&#10;We're launching! ...&#10;&#10;Your spot: {{status_link}}&#10;Your invite link: {{referral_link}}">{esc(prefill_body)}</textarea>
<input name="test_address" type="email" placeholder="Send a test to this address only (optional)">
<button onclick="return this.form.test_address.value || confirm('Send to {broadcast_audience} people?')">Send</button></form>
<p class="muted">Placeholders: {{name}} {{position}} {{referral_link}} {{status_link}} {{product}}. An unsubscribe link is added automatically.</p></div>
<div class="card"><b>Delivery check</b>
<form method="post" action="/admin/test-email"><input name="to" type="email" placeholder="you@example.com" required>
<button class="btn alt">Send test email</button></form>
<form method="post" action="/admin/emails/retry"><button class="btn alt">Retry failed emails</button></form></div>
</div>
<div class="card"><table><tr><th>Queued</th><th>To</th><th>Type</th><th>Subject</th><th>Status</th></tr>{emails_html}</table></div>

<h2 id="settings">Settings</h2>
<div class="card"><form method="post" action="/admin/settings">{settings_inputs}<button>Save settings</button></form>
<p class="muted">Embed the waitlist on any site: <code>&lt;div id="launchkit"&gt;&lt;/div&gt;&lt;script src="{esc(BASE_URL)}/embed.js"&gt;&lt;/script&gt;</code></p></div>
"""
    return page("LaunchKit admin", body)


def post_stats():
    """Unique clicks and signups per tracked post link."""
    clicks = {
        row["post_code"]: row["n"]
        for row in query("SELECT post_code, COUNT(DISTINCT ip_hash) AS n FROM clicks GROUP BY post_code")
    }
    confirmed = "verified = 1" if REQUIRE_VERIFICATION else "1 = 1"
    signups = {
        row["post_code"]: row["n"]
        for row in query(
            f"SELECT post_code, COUNT(*) AS n FROM subscribers WHERE post_code IS NOT NULL AND {confirmed} GROUP BY post_code"
        )
    }
    return clicks, signups


def campaign_options():
    options = '<option value="">No campaign</option>'
    for row in query("SELECT id, name FROM campaigns ORDER BY id DESC"):
        options += f'<option value="{row["id"]}">{esc(row["name"])}</option>'
    return options


def conversion(signups, clicks):
    return f"{100 * signups / clicks:.0f}%" if clicks else "-"


def marketing_page(message=""):
    cfg = settings()
    clicks_by_post, signups_by_post = post_stats()
    posts = query("SELECT * FROM posts")
    campaigns = {row["id"]: row["name"] for row in query("SELECT id, name FROM campaigns")}

    # Aggregate per campaign and per platform
    by_campaign = {}
    by_platform = {}
    for post in posts:
        clicks = clicks_by_post.get(post["link_code"], 0)
        signups = signups_by_post.get(post["link_code"], 0)
        for table, key in ((by_campaign, post["campaign_id"]), (by_platform, post["platform"])):
            totals = table.setdefault(key, [0, 0, 0])
            totals[0] += 1
            totals[1] += clicks
            totals[2] += signups

    def stats_rows(table, label):
        rows = sorted(table.items(), key=lambda item: (-item[1][2], -item[1][1]))
        return "".join(
            f"<tr><td>{esc(label(key))}</td><td>{n_posts}</td><td>{clicks}</td><td>{signups}</td><td>{conversion(signups, clicks)}</td></tr>"
            for key, (n_posts, clicks, signups) in rows
        ) or '<tr><td class="muted">No posts yet</td></tr>'

    campaign_html = stats_rows(by_campaign, lambda key: campaigns.get(key, "No campaign"))
    platform_html = stats_rows(by_platform, lambda key: key)

    ranked_posts = sorted(
        (post for post in posts if clicks_by_post.get(post["link_code"]) or signups_by_post.get(post["link_code"])),
        key=lambda post: (-signups_by_post.get(post["link_code"], 0), -clicks_by_post.get(post["link_code"], 0)),
    )[:10]
    top_html = "".join(
        f"<tr><td>{esc(post['platform'])}</td><td>{esc(render_live(post['body'], post['platform'], post['link_code'])[:110])}</td>"
        f"<td>{clicks_by_post.get(post['link_code'], 0)}</td><td>{signups_by_post.get(post['link_code'], 0)}</td>"
        f"<td>{conversion(signups_by_post.get(post['link_code'], 0), clicks_by_post.get(post['link_code'], 0))}</td></tr>"
        for post in ranked_posts
    ) or '<tr><td class="muted">No clicks yet - share posts from the Social section to start collecting data.</td></tr>'

    since = (local_now() - timedelta(days=30)).isoformat(timespec="seconds")
    hourly = {row["hour"]: row["n"] for row in query(
        "SELECT substr(created_at, 12, 2) AS hour, COUNT(*) AS n FROM clicks WHERE created_at >= ? GROUP BY hour", (since,)
    )}
    max_hourly = max(hourly.values() or [1])
    hours_html = "".join(
        f'<tr><td>{hour:02d}:00</td><td style="width:80%"><div class="bar" style="width:{100 * hourly.get(f"{hour:02d}", 0) / max_hourly:.0f}%"></div></td>'
        f'<td>{hourly.get(f"{hour:02d}", 0)}</td></tr>'
        for hour in range(24)
    )
    total_clicks = sum(hourly.values())
    if total_clicks >= 30:
        best = sorted(hourly.items(), key=lambda item: -item[1])[:3]
        best_text = "Your audience clicks most at " + ", ".join(f"{hour}:00" for hour, _ in best) + " (server time). Schedule posts just before these hours."
    else:
        best_text = f"Only {total_clicks} clicks in the last 30 days - recommendations appear after 30. Until then, test different posting times."

    tasks = query("SELECT * FROM tasks ORDER BY done, due_date, id")
    tasks_html = "".join(
        f"""<tr><td>{esc(task['due_date'])}</td><td><b style="{'text-decoration:line-through;color:#8b93a5' if task['done'] else ''}">{esc(task['title'])}</b>
<div class="muted">{esc(task['details'] or '')}</div>{f'<a href="{esc(task["action_url"])}">Open</a>' if task['action_url'] else ''}</td>
<td><form method="post" action="/admin/task/{task['id']}/toggle"><button class="btn alt">{'Undo' if task['done'] else 'Done'}</button></form></td></tr>"""
        for task in tasks
    ) or '<tr><td class="muted">No tasks yet - generate a launch campaign.</td></tr>'

    platform_checks = "".join(
        f'<label style="margin-right:12px"><input type="checkbox" name="platforms" value="{p}" {"checked" if p in ("x", "linkedin", "reddit") else ""}> {p}</label>'
        for p in PLATFORM_LIMITS
    )
    with ai_jobs_lock:
        pending_ai = ai_jobs["pending"]
    notices = f'<div class="notice ok wide">{esc(message)}</div>' if message else ""
    if pending_ai:
        notices += f'<div class="notice wide">AI is writing {pending_ai} more post(s) in the background - they appear in Admin -> Social.</div>'
    ai_checkbox = '<label><input type="checkbox" name="use_ai" checked> Write with AI</label><br>' if AI_ENABLED else ""
    repurpose_html = (
        f"""<form method="post" action="/admin/repurpose">
<textarea name="source" required placeholder="Paste a changelog, blog post, release notes or a long brain-dump..."></textarea>
{platform_checks}<select name="campaign_id">{campaign_options()}</select><button>Create drafts</button></form>"""
        if AI_ENABLED else '<p class="muted">Needs the AI writer - set AI_API_KEY (or AI_BASE_URL for Ollama).</p>'
    )

    body = f"""
<nav><b>LaunchKit</b> &nbsp; <a href="/admin">Dashboard</a><a href="/admin#social">Posts</a><a href="/" target="_blank">Landing page</a></nav>
{notices}
<h2>Marketing</h2>
<p class="muted">Every post uses its own tracked link (<code>{esc(BASE_URL)}/go/...</code>). Clicks from link-preview bots are ignored;
signups are attributed to the post the visitor came from{' (confirmed signups only)' if REQUIRE_VERIFICATION else ''}.</p>
<div class="grid2">
 <div class="card"><b>Campaigns</b><table><tr><th>Campaign</th><th>Posts</th><th>Clicks</th><th>Signups</th><th>Conv.</th></tr>{campaign_html}</table></div>
 <div class="card"><b>Platforms</b><table><tr><th>Platform</th><th>Posts</th><th>Clicks</th><th>Signups</th><th>Conv.</th></tr>{platform_html}</table></div>
</div>
<div class="card"><b>Top posts</b><table><tr><th>Platform</th><th>Post</th><th>Clicks</th><th>Signups</th><th>Conv.</th></tr>{top_html}</table></div>

<div class="grid2">
<div class="card"><b>Launch-week campaign</b>
<p class="muted">Posts from 7 days before to 7 days after your launch date ({esc(cfg['launch_date'])}) on the selected platforms,
plus a launch checklist (Product Hunt, Show HN, launch email). Past dates are skipped.</p>
<form method="post" action="/admin/launch">{platform_checks}
<label class="muted">Post time</label><input type="time" name="time" value="09:00">
{ai_checkbox}<button>Generate launch campaign</button></form></div>
<div class="card"><b>Repurpose content with AI</b>
<p class="muted">One piece of content in, a native draft for each selected platform out.</p>{repurpose_html}</div>
</div>
<div class="grid2">
<div class="card"><b>Launch checklist</b><table>{tasks_html}</table></div>
<div class="card"><b>New campaign</b><form method="post" action="/admin/campaign"><input name="name" placeholder="e.g. Beta announcement" required>
<button class="btn alt">Create</button></form>
<p class="muted">Assign posts to it when composing. Content plans and launch campaigns create their own.</p>
<b>Best time to post</b><p class="muted">{esc(best_text)}</p><table>{hours_html}</table></div>
</div>
"""
    return page("LaunchKit marketing", body)


EMBED_JS = """(function(){
var base=%s;var box=document.getElementById('launchkit');if(!box)return;
var qs=new URLSearchParams(location.search);var ref=qs.get('ref')||'';var post=qs.get('utm_content')||'';
box.innerHTML='<form style="display:flex;gap:8px;flex-wrap:wrap"><input type="email" required placeholder="you@example.com" style="flex:1;padding:10px;border-radius:8px;border:1px solid #ccc"><button style="padding:10px 16px;border-radius:8px;border:0;background:#5b8cff;color:#fff;font-weight:600">Join waitlist</button></form><div class="lk-msg" style="margin-top:8px"></div>';
var form=box.querySelector('form'),msg=box.querySelector('.lk-msg');
form.onsubmit=function(e){e.preventDefault();
fetch(base+'/api/join',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({email:form.querySelector('input').value,ref:ref,source:qs.get('utm_source')||'embed',post:post})})
.then(function(r){return r.json()}).then(function(d){
if(d.error){msg.textContent=d.error;return;}
if(d.status==='existing'){msg.textContent=d.message;return;}
var a=document.createElement('a');a.href=d.status_url;a.textContent='See your spot and referral link';
msg.textContent=(d.status==='pending_confirmation'?'Check your inbox to confirm. ':'')+'You are #'+d.position+'! ';msg.appendChild(a);});};
})();"""


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "LaunchKit/2.0"

    def client_ip(self):
        if TRUST_PROXY:
            hops = [hop.strip() for hop in self.headers.get("X-Forwarded-For", "").split(",") if hop.strip()]
            if hops:
                return hops[-1]
        return self.client_address[0]

    def send(self, status, content, content_type="text/html; charset=utf-8", extra_headers=None):
        data = content.encode() if isinstance(content, str) else content
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location, extra_headers=None):
        self.send_response(303)
        self.send_header("Location", location)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def admin_redirect(self, message="", anchor=""):
        target = "/admin" + (f"?msg={urllib.parse.quote(message)}" if message else "") + anchor
        self.redirect(target)

    def marketing_redirect(self, message=""):
        self.redirect("/admin/marketing" + (f"?msg={urllib.parse.quote(message)}" if message else ""))

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(length, 1_000_000)).decode("utf-8", "replace")
        if self.headers.get("Content-Type", "").startswith("application/json"):
            try:
                data = json.loads(raw or "{}")
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: (values if key == "platforms" else values[0]) for key, values in parsed.items()}

    def is_admin(self):
        return valid_admin_session(self.cookie("lk_admin"))

    def cookie(self, name):
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get(name)
        return morsel.value if morsel else None

    def log_message(self, fmt, *args):
        print(f"[{local_now():%H:%M:%S}] {self.address_string()} {fmt % args}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ---- GET -------------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        path = url.path

        if path == "/":
            return self.send(200, landing_page(params))
        if path.startswith("/go/"):
            code = path[len("/go/"):]
            post = query("SELECT * FROM posts WHERE link_code = ?", (code,), one=True)
            if not post:
                return self.redirect("/")
            if not BOT_UA_RE.search(self.headers.get("User-Agent", "") or "bot"):
                execute(
                    "INSERT INTO clicks(post_code, ip_hash, created_at) VALUES (?, ?, ?)",
                    (code, hash_ip(self.client_ip()), local_now().isoformat(timespec="seconds")),
                )
            utm = urllib.parse.urlencode({
                "utm_source": post["platform"],
                "utm_medium": "social",
                "utm_campaign": f"campaign-{post['campaign_id']}" if post["campaign_id"] else "none",
                "utm_content": code,
            })
            target = settings().get("post_link_target", "").strip()
            destination = f"{target}{'&' if '?' in target else '?'}{utm}" if target else f"/?{utm}"
            # Remember the post for 30 days so a later signup is still attributed
            return self.redirect(destination, {"Set-Cookie": f"lk_post={code}; Path=/; Max-Age=2592000; SameSite=Lax"})
        if path.startswith("/s/"):
            subscriber = get_subscriber("private_token", path[3:])
            if not subscriber:
                return self.send(404, message_page("Link not found", "This link is invalid. Check the email we sent you."))
            return self.send(200, status_page(subscriber, params))
        if path.startswith("/verify/"):
            subscriber = confirm_email(path[len("/verify/"):])
            if not subscriber:
                return self.send(404, message_page("Link expired", "This confirmation link is invalid."))
            return self.redirect(f"/s/{subscriber['private_token']}?confirmed")
        if path.startswith("/unsubscribe/"):
            subscriber = get_subscriber("private_token", path[len("/unsubscribe/"):])
            if not subscriber:
                return self.send(404, message_page("Link not found", "This link is invalid."))
            return self.send(200, unsubscribe_page(subscriber))
        if path == "/embed.js":
            return self.send(200, EMBED_JS % json.dumps(BASE_URL), "application/javascript",
                             {"Access-Control-Allow-Origin": "*"})
        if path == "/api/stats":
            return self.send(200, json.dumps({"waitlist": waitlist_count()}), "application/json",
                             {"Access-Control-Allow-Origin": "*"})
        if path == "/admin":
            if not self.is_admin():
                return self.send(200, admin_login_page())
            return self.send(200, admin_page(
                params.get("msg", [""])[0], params.get("bs", [""])[0], params.get("bb", [""])[0]
            ))
        if path == "/admin/marketing":
            if not self.is_admin():
                return self.redirect("/admin")
            return self.send(200, marketing_page(params.get("msg", [""])[0]))
        if path == "/admin/logout":
            delete_admin_session(self.cookie("lk_admin"))
            return self.redirect("/admin", {"Set-Cookie": "lk_admin=; Path=/; Max-Age=0"})
        if path == "/admin/enter":
            key = params.get("key", [""])[0]
            if not valid_admin_session(key):
                return self.redirect("/admin")
            cookie = (f"lk_admin={key}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ADMIN_SESSION_SECONDS}"
                      + ("; Secure" if SECURE_COOKIES else ""))
            return self.redirect("/admin", {"Set-Cookie": cookie})
        if path == "/privacy":
            return self.send(200, privacy_page())
        if path == "/admin/backup.db":
            if not self.is_admin():
                return self.redirect("/admin")
            with open(backup_database(), "rb") as backup_file:
                data = backup_file.read()
            return self.send(200, data, "application/octet-stream", {
                "Content-Disposition": f"attachment; filename=launchkit-backup-{local_now():%Y%m%d}.db"
            })
        if path == "/admin/export.csv":
            if not self.is_admin():
                return self.redirect("/admin")
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["position", "email", "name", "confirmed", "referrals", "referred_by",
                             "source", "unsubscribed", "created_at", "code"])
            for rank, row in ranked_subscribers():
                writer.writerow([rank, row["email"], row["name"], row["verified"], row["referrals"],
                                 row["referred_by"], row["source"], row["unsubscribed"], row["created_at"], row["code"]])
            return self.send(200, buffer.getvalue(), "text/csv",
                             {"Content-Disposition": "attachment; filename=waitlist.csv"})
        return self.send(404, message_page("Not found", ""))

    # ---- POST ------------------------------------------------------------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        form = self.read_body()

        if path == "/join":
            subscriber, outcome, error = join_waitlist(
                form.get("email"), form.get("name"), form.get("ref"), form.get("source"), self.client_ip(),
                form.get("post") or self.cookie("lk_post"),
            )
            if error:
                return self.send(400, landing_page({"ref": [form.get("ref", "")]}, error))
            if outcome == "existing":
                return self.send(200, message_page(
                    "You're already on the list",
                    "We've emailed you a link to your spot. Check your inbox (and spam folder).",
                ))
            return self.redirect(f"/s/{subscriber['private_token']}")

        if path == "/api/join":
            subscriber, outcome, error = join_waitlist(
                form.get("email"), form.get("name"), form.get("ref"), form.get("source", "api"), self.client_ip(),
                form.get("post") or self.cookie("lk_post"),
            )
            cors = {"Access-Control-Allow-Origin": "*"}
            if error:
                return self.send(400, json.dumps({"error": error}), "application/json", cors)
            if outcome == "existing":
                payload = {"status": "existing", "message": "You're already on the list - we've emailed you your link."}
            else:
                payload = {
                    "status": "pending_confirmation" if not subscriber["verified"] else "joined",
                    "position": position_of(subscriber),
                    "referral_link": referral_link(subscriber),
                    "status_url": status_url(subscriber),
                }
            return self.send(200, json.dumps(payload), "application/json", cors)

        match = re.match(r"^/s/([\w-]+)/resend$", path)
        if match:
            subscriber = get_subscriber("private_token", match.group(1))
            if not subscriber:
                return self.send(404, "")
            send_access_email(subscriber)
            return self.redirect(f"/s/{subscriber['private_token']}?resent")

        if path.startswith("/unsubscribe/"):
            subscriber = get_subscriber("private_token", path[len("/unsubscribe/"):])
            if not subscriber:
                return self.send(404, "")
            execute("UPDATE subscribers SET unsubscribed = 1 WHERE id = ?", (subscriber["id"],))
            return self.send(200, unsubscribe_page(subscriber, done=True))

        if path == "/admin/login":
            ip = self.client_ip()
            if login_blocked(ip):
                return self.send(429, admin_login_page("Too many attempts. Try again in 15 minutes."))
            if secrets.compare_digest(str(form.get("token", "")), ADMIN_TOKEN):
                cookie = (f"lk_admin={create_admin_session()}; Path=/; HttpOnly; SameSite=Strict; "
                          f"Max-Age={ADMIN_SESSION_SECONDS}" + ("; Secure" if SECURE_COOKIES else ""))
                return self.redirect("/admin", {"Set-Cookie": cookie})
            record_failed_login(ip)
            return self.send(401, admin_login_page("Wrong token"))

        if not path.startswith("/admin/"):
            return self.send(404, "")
        if not self.is_admin():
            return self.redirect("/admin")
        return self.handle_admin_post(path, form)

    def handle_admin_post(self, path, form):
        if path == "/admin/settings":
            for key in DEFAULT_SETTINGS:
                if key in form:
                    execute("UPDATE settings SET value = ? WHERE key = ?", (form[key].strip(), key))
            return self.admin_redirect("Settings saved", "#settings")

        if path == "/admin/plan":
            platforms = [p for p in form.get("platforms", []) if p in PLATFORM_LIMITS] or ["x"]
            try:
                start = datetime.strptime(form.get("start", ""), "%Y-%m-%d")
            except ValueError:
                start = local_now() + timedelta(days=1)
            use_ai = AI_ENABLED and "use_ai" in form
            count = generate_content_plan(platforms, start, form.get("time") or "09:00", use_ai)
            message = f"AI is writing {count} posts in the background" if use_ai else f"Created {count} posts"
            return self.admin_redirect(message, "#social")

        if path == "/admin/compose":
            platform = form.get("platform") if form.get("platform") in PLATFORM_LIMITS else "x"
            template = form.get("template", "free")
            text = form.get("body", "").strip()
            scheduled = form.get("scheduled_at") or None
            note = None
            campaign_id = int(form["campaign_id"]) if form.get("campaign_id", "").isdigit() else None
            try:
                if AI_ENABLED and "use_ai" in form:
                    text = ai_write_post(platform, template, notes=text)
                    note = f"AI-written ({AI_MODEL})"
                elif not text and template in TEMPLATES:
                    text = render_template(template, platform)
                    note = "Template"
            except Exception as error:  # noqa: BLE001
                return self.admin_redirect(f"AI error: {error}", "#social")
            if not text:
                return self.admin_redirect("Write something or pick a template", "#social")
            insert_post(platform, text, scheduled, "scheduled" if scheduled else "draft", note, campaign_id)
            return self.admin_redirect("Post saved", "#social")

        if path == "/admin/launch":
            platforms = [p for p in form.get("platforms", []) if p in PLATFORM_LIMITS] or ["x"]
            use_ai = AI_ENABLED and "use_ai" in form
            try:
                count = create_launch_campaign(platforms, form.get("time") or "09:00", use_ai)
            except ValueError:
                return self.marketing_redirect("Set a valid launch_date (YYYY-MM-DD) in Settings first")
            action = "AI is writing" if use_ai else "Created"
            return self.marketing_redirect(f"Launch campaign: {action} {count} posts + checklist")

        if path == "/admin/repurpose":
            platforms = [p for p in form.get("platforms", []) if p in PLATFORM_LIMITS]
            source = form.get("source", "").strip()
            if not (AI_ENABLED and platforms and source):
                return self.marketing_redirect("Repurposing needs AI, some text and at least one platform")
            campaign_id = int(form["campaign_id"]) if form.get("campaign_id", "").isdigit() else None
            count = repurpose_content(source, platforms, campaign_id)
            return self.marketing_redirect(f"AI is writing {count} draft(s)")

        if path == "/admin/campaign":
            name = form.get("name", "").strip()
            if name:
                create_campaign(name)
            return self.marketing_redirect(f"Campaign '{name}' created" if name else "Name required")

        task_match = re.match(r"^/admin/task/(\d+)/toggle$", path)
        if task_match:
            execute("UPDATE tasks SET done = 1 - done WHERE id = ?", (int(task_match.group(1)),))
            return self.marketing_redirect()

        if path == "/admin/broadcast":
            subject = form.get("subject", "").strip()
            body = form.get("body", "").strip()
            if not subject or not body:
                return self.admin_redirect("Subject and message are required", "#emails")
            count = queue_broadcast(subject, body, form.get("test_address", "").strip())
            return self.admin_redirect(f"Queued {count} email(s)", "#emails")

        if path == "/admin/test-email":
            product = settings()["product"]
            queue_email(
                form.get("to", "").strip(),
                f"{product}: LaunchKit test email",
                email_layout("<p>Email delivery works.</p>", "Sent from your LaunchKit admin."),
                "Email delivery works.",
                kind="test",
            )
            return self.admin_redirect("Test email queued - check the list below", "#emails")

        if path == "/admin/emails/retry":
            count = execute("UPDATE emails SET status = 'queued', error = NULL WHERE status = 'failed'").rowcount
            return self.admin_redirect(f"Re-queued {count} email(s)", "#emails")

        match = re.match(r"^/admin/post/(\d+)/(publish|done|delete|ai|edit)$", path)
        if match:
            post_id, action = int(match.group(1)), match.group(2)
            post = query("SELECT * FROM posts WHERE id = ?", (post_id,), one=True)
            if not post:
                return self.admin_redirect("Post not found", "#social")
            if action == "delete":
                execute("DELETE FROM posts WHERE id = ?", (post_id,))
            elif action == "done":
                execute("UPDATE posts SET status='posted', result='marked manually' WHERE id=?", (post_id,))
            elif action == "edit":
                scheduled = form.get("scheduled_at") or None
                status = post["status"]
                if status in ("draft", "scheduled", "due", "failed"):
                    status = "scheduled" if scheduled else "draft"
                execute("UPDATE posts SET body=?, scheduled_at=?, status=? WHERE id=?",
                        (form.get("body", post["body"]), scheduled, status, post_id))
            elif action == "ai":
                try:
                    new_body = ai_write_post(post["platform"], existing_post=post["body"])
                    execute("UPDATE posts SET body=?, result=? WHERE id=?", (new_body, f"AI-rewritten ({AI_MODEL})", post_id))
                except Exception as error:  # noqa: BLE001
                    return self.admin_redirect(f"AI error: {error}", "#social")
            elif action == "publish":
                try:
                    result = publish_post(post)
                    execute("UPDATE posts SET status='posted', result=? WHERE id=?", (str(result), post_id))
                except Exception as error:  # noqa: BLE001
                    execute("UPDATE posts SET status='failed', result=? WHERE id=?", (str(error)[:300], post_id))
            return self.admin_redirect(anchor="#social")

        return self.send(404, "")


def check_admin_token():
    if ADMIN_TOKEN in ("", "change-me") or len(ADMIN_TOKEN) < 12:
        if HOST not in ("127.0.0.1", "localhost"):
            raise SystemExit("Refusing to listen publicly with a weak ADMIN_TOKEN - set one with 12+ characters.")
        print("WARNING: ADMIN_TOKEN is weak - fine on this computer, set a strong one before deploying.")


def start_background_services():
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=email_worker_loop, daemon=True).start()
    if BACKUP_KEEP > 0:
        threading.Thread(target=backup_loop, daemon=True).start()


def main():
    init_db()
    check_admin_token()
    start_background_services()
    print(f"LaunchKit running at {BASE_URL}  (admin: {BASE_URL}/admin)")
    print(f"Email: {email_provider()} | AI: {'on, ' + AI_MODEL if AI_ENABLED else 'off'} | "
          f"Confirmation required: {REQUIRE_VERIFICATION}")
    print(f"Listening on {HOST}:{PORT} | trust proxy: {TRUST_PROXY} | time zone: {TIMEZONE or 'system'} | "
          f"backups: {BACKUP_DIR if BACKUP_KEEP > 0 else 'off'}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
