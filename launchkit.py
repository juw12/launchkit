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


EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u200D\u20E3]"
)


def strip_emoji(text):
    """Remove emoji and tidy the spaces they leave behind."""
    text = EMOJI_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def clean_ai_text(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = strip_emoji(text)
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
        "must fill in. Never use emoji. Output only the post text: no preamble, no quotes, no explanations."
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
            action_url = "/admin/emails?" + urllib.parse.urlencode({"bs": LAUNCH_EMAIL_SUBJECT, "bb": LAUNCH_EMAIL_BODY})
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
:root{--bg:#f6f7f9;--surface:#fff;--surface-2:#f8fafc;--border:#e5e7eb;--border-strong:#d6dae1;--text:#0f172a;--text-2:#334155;
--muted:#64748b;--accent:#4f46e5;--accent-hover:#4338ca;--accent-soft:#eef2ff;--accent-text:#4338ca;--success:#047857;
--success-soft:#ecfdf5;--warn:#b45309;--warn-soft:#fffbeb;--danger:#dc2626;--danger-soft:#fef2f2;--radius:14px;
--shadow:0 1px 2px rgba(15,23,42,.04),0 2px 8px rgba(15,23,42,.04)}
@media (prefers-color-scheme:dark){:root{--bg:#0b0d12;--surface:#11141b;--surface-2:#161a23;--border:#222734;--border-strong:#2e3445;
--text:#f1f5f9;--text-2:#cbd5e1;--muted:#94a3b8;--accent:#6366f1;--accent-hover:#4f46e5;--accent-soft:rgba(129,140,248,.13);
--accent-text:#a5b4fc;--success:#34d399;--success-soft:rgba(52,211,153,.1);--warn:#fbbf24;--warn-soft:rgba(251,191,36,.1);
--danger:#f87171;--danger-soft:rgba(248,113,113,.1);--shadow:none}}
*{box-sizing:border-box}
body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:14px;
line-height:1.55;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased}
a{color:var(--accent-text);text-decoration:none}a:hover{text-decoration:underline}
h1,h2,h3{margin:0;letter-spacing:-.02em;color:var(--text)}h1{font-size:26px;font-weight:700}h2{font-size:18px;font-weight:650}
h3{font-size:15px;font-weight:600}p{margin:0}.muted{color:var(--muted)}.small{font-size:12.5px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;background:var(--surface-2);
border:1px solid var(--border);padding:1px 6px;border-radius:6px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;height:36px;padding:0 14px;border-radius:9px;
border:1px solid transparent;background:var(--accent);color:#fff;font:inherit;font-weight:600;font-size:13.5px;cursor:pointer;
text-decoration:none!important;white-space:nowrap;transition:background .15s,border-color .15s,box-shadow .15s}
.btn:hover{background:var(--accent-hover)}.btn:focus-visible{outline:none;box-shadow:0 0 0 3px var(--accent-soft)}
.btn-secondary{background:var(--surface);color:var(--text-2);border-color:var(--border-strong)}.btn-secondary:hover{background:var(--surface-2)}
.btn-ghost{background:transparent;color:var(--text-2)}.btn-ghost:hover{background:var(--surface-2)}
.btn-danger{background:transparent;color:var(--danger)}.btn-danger:hover{background:var(--danger-soft)}
.btn-sm{height:30px;padding:0 10px;font-size:12.5px}.btn-lg{height:46px;padding:0 22px;font-size:15px}.btn-block{width:100%}
input,textarea,select{width:100%;font:inherit;font-size:14px;color:var(--text);background:var(--surface);border:1px solid var(--border-strong);
border-radius:9px;padding:9px 12px;transition:border-color .15s,box-shadow .15s}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
input::placeholder,textarea::placeholder{color:var(--muted);opacity:.8}
textarea{min-height:110px;resize:vertical;line-height:1.5}input[type=checkbox]{width:16px;height:16px;padding:0;accent-color:var(--accent)}
.field{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}.field label,.label{font-size:13px;font-weight:600;color:var(--text-2)}
.help{font-size:12.5px;color:var(--muted)}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.grow{flex:1;min-width:160px}
.between{justify-content:space-between}.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border:1px solid var(--border-strong);border-radius:999px;
font-size:13px;cursor:pointer;background:var(--surface);user-select:none;color:var(--text-2)}
.chip:has(input:checked){border-color:var(--accent);background:var(--accent-soft);color:var(--accent-text)}
.toggle{display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--text-2);cursor:pointer}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}
.card-body{padding:20px}.card-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;
align-items:center;gap:12px}.card-head p{margin-top:2px;font-size:13px;color:var(--muted)}
.stack>*+*{margin-top:18px}.grid{display:grid;gap:18px}.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.g3{grid-template-columns:repeat(3,minmax(0,1fr))}.g4{grid-template-columns:repeat(4,minmax(0,1fr))}@media(max-width:960px){.g2,.g3{grid-template-columns:1fr}.g4{grid-template-columns:repeat(2,minmax(0,1fr))}}
.stat{padding:18px 20px}.stat-label{font-size:12.5px;color:var(--muted);font-weight:500}
.stat-value{font-size:28px;font-weight:650;letter-spacing:-.03em;margin-top:4px;font-variant-numeric:tabular-nums}
.stat-hint{font-size:12px;color:var(--muted);margin-top:2px}
table{width:100%;border-collapse:collapse}th{font-size:11.5px;font-weight:600;color:var(--muted);text-transform:uppercase;
letter-spacing:.05em;text-align:left;padding:10px 20px;border-bottom:1px solid var(--border);background:var(--surface-2)}
td{padding:12px 20px;border-bottom:1px solid var(--border);vertical-align:top;color:var(--text-2)}tr:last-child td{border-bottom:0}
.num-table td:not(:first-child),.num-table th:not(:first-child){text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;
background:var(--surface-2);color:var(--text-2);border:1px solid var(--border);white-space:nowrap}
.badge.dot::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.b-posted,.b-sent,.b-on{background:var(--success-soft);color:var(--success);border-color:transparent}
.b-due,.b-queued,.b-off{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.b-failed{background:var(--danger-soft);color:var(--danger);border-color:transparent}
.b-scheduled{background:var(--accent-soft);color:var(--accent-text);border-color:transparent}
.b-neutral{background:var(--surface-2);color:var(--muted);border-color:var(--border)}
.alert{padding:12px 16px;border-radius:11px;font-size:13.5px;border:1px solid var(--border);background:var(--surface)}
.alert-success{background:var(--success-soft);border-color:transparent;color:var(--success)}
.alert-warn{background:var(--warn-soft);border-color:transparent;color:var(--warn)}
.alert-danger{background:var(--danger-soft);border-color:transparent;color:var(--danger)}
.alert-info{background:var(--accent-soft);border-color:transparent;color:var(--accent-text)}
.empty{padding:40px 20px;text-align:center;color:var(--muted)}.empty h3{margin-bottom:6px}.empty .btn{margin-top:14px}
.app{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:100vh}
.sidebar{position:sticky;top:0;height:100vh;padding:20px 14px;border-right:1px solid var(--border);background:var(--surface);
display:flex;flex-direction:column}.brand{display:flex;align-items:center;gap:10px;padding:2px 10px 20px;font-weight:700;font-size:15px}
.logo{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#6366f1,#a855f7);display:grid;place-items:center;
color:#fff;font-size:14px;font-weight:800;flex:none}.nav{display:flex;flex-direction:column;gap:2px}
.nav a{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:9px;color:var(--text-2);font-weight:500;text-decoration:none!important}
.nav a:hover{background:var(--surface-2)}.nav a.active{background:var(--accent-soft);color:var(--accent-text)}
.nav svg{width:17px;height:17px;flex:none;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.nav-section{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);padding:16px 10px 6px}
.ico{width:13px;height:13px;flex:none;stroke:currentColor;fill:none;stroke-width:3;stroke-linecap:round;stroke-linejoin:round}
.savebar{position:sticky;bottom:0;display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:0 -4px 16px rgba(15,23,42,.06)}
.spacer{flex:1}.topbar{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;padding:30px 36px 0;max-width:1200px}
.content{padding:22px 36px 56px;max-width:1200px}
@media(max-width:860px){.app{grid-template-columns:minmax(0,1fr)}
.sidebar{position:sticky;top:0;z-index:10;height:auto;padding:10px 12px 8px;border-right:0;border-bottom:1px solid var(--border)}
.brand{padding:2px 6px 8px}.nav{flex-direction:row;flex-wrap:nowrap;overflow-x:auto;gap:4px;scrollbar-width:none}
.nav::-webkit-scrollbar{display:none}.nav a{white-space:nowrap;padding:7px 10px}
.nav-section,.spacer{display:none}.nav.shortcuts{margin-top:2px}.nav.shortcuts a{font-size:12.5px;color:var(--muted);padding:5px 10px}
.topbar{flex-direction:column;align-items:stretch;padding:20px 16px 0}.topbar h1{font-size:22px}
.content{padding:16px 16px 40px}.card{overflow-x:auto}
.steps li{flex-wrap:wrap;padding:12px 16px}.steps li>.btn{margin-left:36px}
.card-head{padding:14px 16px}.card-body{padding:16px}.stat{padding:14px 16px}.stat-value{font-size:24px}
th,td{padding:10px 14px}.savebar{flex-direction:column;align-items:stretch}}
.steps{list-style:none;margin:0;padding:0}.steps li{display:flex;align-items:center;gap:14px;padding:13px 20px;border-top:1px solid var(--border)}
.check{width:22px;height:22px;padding:0;border-radius:50%;border:2px solid var(--border-strong);flex:none;display:grid;place-items:center;color:#fff}
.done .check{background:var(--success);border-color:var(--success)}.done .step-title{color:var(--muted);text-decoration:line-through}
.progress{height:6px;background:var(--border);border-radius:99px;overflow:hidden}
.progress>span{display:block;height:100%;background:linear-gradient(90deg,#6366f1,#a855f7);border-radius:99px}
.chart{display:flex;align-items:flex-end;gap:6px;height:150px}.chart .col{flex:1;display:flex;flex-direction:column;align-items:center;
justify-content:flex-end;gap:6px;height:100%}.chart .bar{width:100%;max-width:26px;background:var(--accent);border-radius:6px 6px 2px 2px;opacity:.85}
.chart .bar.zero{background:var(--border);opacity:1}.chart .lbl{font-size:10.5px;color:var(--muted)}
.hbar{height:8px;border-radius:99px;background:var(--accent);opacity:.85;min-width:2px}
.post{padding:18px 20px;border-top:1px solid var(--border)}.post:first-child{border-top:0}
.post-meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12.5px;color:var(--muted)}
.post-text{white-space:pre-wrap;margin:12px 0;padding:12px 14px;background:var(--surface-2);border:1px solid var(--border);
border-radius:10px;font-size:13.5px;color:var(--text);font-family:inherit;line-height:1.55}
details summary{cursor:pointer;color:var(--accent-text);font-size:13px;font-weight:500;list-style:none}
details summary::-webkit-details-marker{display:none}details[open] summary{margin-bottom:10px}.over{color:var(--danger);font-weight:600}
.public{min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:64px 20px 40px;
background:radial-gradient(1100px 480px at 50% -12%,var(--accent-soft),transparent)}.public-inner{width:100%;max-width:520px}
.hero{text-align:center;margin-bottom:26px}.hero h1{font-size:42px;line-height:1.08;margin:20px 0 12px;letter-spacing:-.035em}
.tagline{font-size:17px;color:var(--muted);max-width:440px;margin:0 auto}
.pill{display:inline-flex;align-items:center;gap:8px;padding:5px 12px;border-radius:999px;background:var(--surface);border:1px solid var(--border);
font-size:12.5px;font-weight:500;color:var(--text-2);box-shadow:var(--shadow)}
.pill .live{width:7px;height:7px;border-radius:50%;background:#10b981;box-shadow:0 0 0 3px rgba(16,185,129,.18)}
.big-logo{width:54px;height:54px;border-radius:15px;font-size:24px;margin:0 auto}
.proof{display:flex;align-items:center;justify-content:center;gap:10px;margin-top:16px;color:var(--muted);font-size:13.5px}
.avatars{display:flex}.avatars span{width:26px;height:26px;border-radius:50%;border:2px solid var(--surface);margin-left:-8px;
background:linear-gradient(135deg,#c7d2fe,#e9d5ff)}.avatars span:first-child{margin-left:0}
.avatars span:nth-child(2){background:linear-gradient(135deg,#bae6fd,#c7d2fe)}.avatars span:nth-child(3){background:linear-gradient(135deg,#fbcfe8,#fde68a)}
.how{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center}.how b{display:block;font-size:13.5px;margin-top:8px}
.how .n{display:inline-grid;place-items:center;width:30px;height:30px;border-radius:50%;background:var(--accent-soft);color:var(--accent-text);font-weight:700;font-size:13px}
.reward{display:flex;align-items:center;gap:12px;padding:12px 0;border-top:1px solid var(--border);color:var(--text-2)}
.reward:first-child{border-top:0}.goal{flex:none;min-width:82px;display:inline-flex;align-items:center;justify-content:center;gap:4px;font-size:12px;font-weight:600;color:var(--accent-text);
background:var(--accent-soft);border-radius:999px;padding:3px 10px;text-align:center}.reward.done .goal{background:var(--success-soft);color:var(--success)}
.position{font-size:68px;font-weight:800;letter-spacing:-.05em;line-height:1;background:linear-gradient(135deg,#6366f1,#a855f7);
-webkit-background-clip:text;background-clip:text;color:transparent}
.share-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
@media (max-width:440px){.share-grid{grid-template-columns:repeat(3,1fr)}}
.footer{margin-top:28px;text-align:center;font-size:12.5px;color:var(--muted)}.prose p{margin:0 0 12px;color:var(--text-2)}
"""

PLATFORM_LABELS = {"x": "X", "bluesky": "Bluesky", "threads": "Threads", "linkedin": "LinkedIn", "discord": "Discord", "reddit": "Reddit"}
STATUS_LABELS = {"draft": "Draft", "scheduled": "Scheduled", "due": "Ready to post", "posted": "Posted", "failed": "Failed",
                 "queued": "Queued", "sent": "Sent"}
ICONS = {
    "overview": '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
                '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    "posts": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "marketing": '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
    "emails": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    "settings": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/>'
                '<circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "external": '<path d="M14 3h7v7"/><path d="M10 14L21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>',
    "download": '<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/>',
}
SETTING_FIELDS = [
    ("Product", [
        ("product", "Product name", "Shown on your waitlist page, emails and posts.", "text"),
        ("tagline", "Tagline", "One sentence: what it does for people.", "text"),
        ("problem", "Problem it solves", "Used in posts, e.g. \"spending weeks on boilerplate\".", "text"),
        ("audience", "Audience", "Who it's for, e.g. \"indie developers\".", "text"),
        ("launch_date", "Launch date", "Drives the countdown and the launch-week campaign.", "date"),
        ("top_reward", "Top reward", "What your best referrers and early members get.", "text"),
    ]),
    ("Sharing", [
        ("share_image_url", "Link preview image URL", "Image shown when your page is shared on X, LinkedIn, Slack... (optional).", "url"),
        ("post_link_target", "Where post links go", "Leave empty for the waitlist page. After launch, set your app's URL.", "url"),
    ]),
    ("Privacy", [
        ("contact_email", "Contact email", "Shown on the privacy page for data requests.", "email"),
        ("privacy_url", "Your own privacy policy URL", "Leave empty to use the built-in privacy page.", "url"),
    ]),
]


CHECK_SVG = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'


def icon(name):
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'



def shorten(text, limit):
    """Trim text to about `limit` characters at a word boundary."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:-") + "\u2026"


def status_badge(state, important=False):
    """On / Off / Default badge. Only important integrations show "Off" as a warning."""
    if state is None:
        return '<span class="badge b-neutral">Default</span>'
    if state:
        return '<span class="badge dot b-on">Connected</span>'
    if important:
        return '<span class="badge dot b-off">Not set up</span>'
    return '<span class="badge b-neutral">Off</span>'

def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def nice_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%B %d, %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return value or ""


def badge(status):
    return f'<span class="badge dot b-{esc(status)}">{esc(STATUS_LABELS.get(status, status))}</span>'


def flash_html(message):
    if not message:
        return ""
    lowered = message.lower()
    kind = "danger" if ("error" in lowered or "required" in lowered or "not found" in lowered) else "success"
    return f'<div class="alert alert-{kind}">{esc(message)}</div>'


def platform_chips(selected):
    return "".join(
        f'<label class="chip"><input type="checkbox" name="platforms" value="{p}" {"checked" if p in selected else ""}>'
        f'{PLATFORM_LABELS[p]}</label>'
        for p in PLATFORM_LIMITS
    )


def page(title, body, head_extra=""):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">
<title>{esc(title)}</title>{head_extra}<style>{CSS}</style></head>
<body>{body}</body></html>"""


def public_shell(title, inner, head_extra=""):
    product = settings()["product"]
    privacy = esc(settings().get("privacy_url") or "/privacy")
    body = f"""<div class="public"><div class="public-inner">{inner}
<div class="footer">{esc(product)} &middot; <a href="{privacy}">Privacy</a> &middot; Powered by LaunchKit</div></div></div>"""
    return page(title, body, head_extra)


def message_page(title, message_html):
    inner = f"""<div class="card"><div class="card-body" style="text-align:center;padding:40px 28px">
<h1 style="font-size:26px">{esc(title)}</h1><p class="muted" style="margin:10px 0 22px">{message_html}</p>
<a class="btn btn-secondary" href="/">Back to the waitlist</a></div></div>"""
    return public_shell(title, inner)


def landing_page(params, error=""):
    cfg = settings()
    total = waitlist_count()
    ref = esc(params.get("ref", [""])[0])
    source = esc(params.get("utm_source", [""])[0])
    post_code = esc(params.get("utm_content", [""])[0])
    error_html = f'<div class="alert alert-danger" style="margin-bottom:12px">{esc(error)}</div>' if error else ""
    initial = esc((cfg["product"] or "L")[:1].upper())
    rewards = "".join(
        f'<div class="reward"><span class="goal">{plural(n, "invite")}</span><span>{esc(r)}</span></div>'
        for n, r in MILESTONES
    )
    if total >= 3:
        proof = (
            f'<div class="proof"><span class="avatars"><span></span><span></span><span></span></span>'
            f'<span><b>{total}</b> people have already joined</span></div>'
        )
    else:
        proof = '<div class="proof">Early access opens soon - be one of the first in line.</div>'

    inner = f"""
<div class="hero">
  <div class="logo big-logo">{initial}</div>
  <h1>{esc(cfg['product'])}</h1>
  <p class="tagline">{esc(cfg['tagline'])}</p>
  <div style="margin-top:18px"><span class="pill"><span class="live"></span>Launching {esc(nice_date(cfg['launch_date']))}</span></div>
</div>
<div class="card"><div class="card-body">
  {error_html}
  <form method="post" action="/join">
    <div class="field"><label for="email">Email</label><input id="email" name="email" type="email" placeholder="you@example.com" required autofocus></div>
    <div class="field"><label for="name">Name <span class="muted" style="font-weight:400">(optional)</span></label><input id="name" name="name" placeholder="Ada Lovelace"></div>
    <input type="hidden" name="ref" value="{ref}"><input type="hidden" name="source" value="{source}"><input type="hidden" name="post" value="{post_code}">
    <button class="btn btn-lg btn-block">Join the waitlist</button>
    <p class="help" style="text-align:center;margin-top:10px">By joining you agree to receive emails about {esc(cfg['product'])}. Unsubscribe anytime. <a href="{esc(cfg.get('privacy_url') or '/privacy')}">Privacy</a></p>
  </form>
</div></div>
{proof}
<div class="card" style="margin-top:26px"><div class="card-body">
  <div class="how">
    <div><span class="n">1</span><b>Join</b><span class="muted small">Get your spot in line</span></div>
    <div><span class="n">2</span><b>Share</b><span class="muted small">Send friends your link</span></div>
    <div><span class="n">3</span><b>Move up</b><span class="muted small">+{BUMP_PER_REFERRAL} spots per friend</span></div>
  </div>
</div></div>
<div class="card" style="margin-top:14px"><div class="card-head"><div><h3>Rewards for inviting friends</h3></div></div>
<div class="card-body" style="padding-top:6px;padding-bottom:6px">{rewards}</div></div>
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
    return public_shell(cfg["product"] + " - Join the waitlist", inner, head)


def status_page(subscriber, params):
    cfg = settings()
    total = waitlist_count()
    position = position_of(subscriber)
    link = referral_link(subscriber)
    private = esc(subscriber["private_token"])
    referrals = subscriber["referrals"]
    pending_friends = query(
        "SELECT COUNT(*) AS n FROM subscribers WHERE referred_by = ? AND referral_credited = 0",
        (subscriber["code"],),
        one=True,
    )["n"]
    share_text = f"I just joined the waitlist for {cfg['product']} - {cfg['tagline']}. Join me: {link}"
    encoded = urllib.parse.quote(share_text)

    notices = ""
    if "confirmed" in params:
        notices += '<div class="alert alert-success">Email confirmed - your spot is locked in.</div>'
    if "resent" in params:
        notices += '<div class="alert alert-info">Confirmation email sent. Check your inbox (and spam folder).</div>'
    if REQUIRE_VERIFICATION and not subscriber["verified"]:
        notices += f"""<div class="alert alert-warn"><div class="row between"><span>Please confirm your email to lock in your spot - we sent you a link.</span>
<form method="post" action="/s/{private}/resend"><button class="btn btn-secondary btn-sm">Resend email</button></form></div></div>"""
    if subscriber["unsubscribed"]:
        notices += '<div class="alert alert-info">You have unsubscribed from updates. You keep your spot.</div>'

    next_goal = next(((n, r) for n, r in MILESTONES if referrals < n), None)
    previous_goal = max([n for n, _ in MILESTONES if referrals >= n] or [0])
    if next_goal:
        remaining = next_goal[0] - referrals
        span = max(next_goal[0] - previous_goal, 1)
        progress = 100 * (referrals - previous_goal) / span
        next_html = (f'<div class="row between small"><span>Next reward: <b>{esc(next_goal[1])}</b></span>'
                     f'<span class="muted">{plural(remaining, "more invite")}</span></div>'
                     f'<div class="progress" style="margin-top:8px"><span style="width:{progress:.0f}%"></span></div>')
    else:
        next_html = '<div class="alert alert-success">You\'ve unlocked every reward. Thank you for spreading the word.</div>'
    rewards = "".join(
        f'<div class="reward {"done" if referrals >= n else ""}"><span class="goal">{CHECK_SVG if referrals >= n else ""}{plural(n, "invite")}</span>'
        f'<span>{esc(r)}</span></div>'
        for n, r in MILESTONES
    )
    pending_text = (
        f'<p class="muted small" style="margin-top:6px">{plural(pending_friends, "friend")} still need to confirm their email.</p>'
        if pending_friends else ""
    )
    greeting = f", {esc(subscriber['name'])}" if subscriber["name"] else ""
    share_buttons = "".join(
        f'<a class="btn btn-secondary btn-sm" target="_blank" href="{href}">{label}</a>'
        for label, href in (
            ("X", f"https://twitter.com/intent/tweet?text={encoded}"),
            ("LinkedIn", f"https://www.linkedin.com/feed/?shareActive=true&text={encoded}"),
            ("Bluesky", f"https://bsky.app/intent/compose?text={encoded}"),
            ("WhatsApp", f"https://wa.me/?text={encoded}"),
            ("Email", f"mailto:?subject={urllib.parse.quote(cfg['product'])}&body={encoded}"),
        )
    )
    inner = f"""
<div class="stack">{notices}</div>
<div class="card" style="margin-top:{'16px' if notices else '0'}"><div class="card-body" style="text-align:center;padding:34px 24px">
  <p class="muted">You're on the {esc(cfg['product'])} waitlist{greeting}!</p>
  <div class="position" style="margin:14px 0 8px">#{position}</div>
  <p class="muted">of {max(total, position)} &middot; <span>{referrals} friend{'s' if referrals != 1 else ''} referred</span></p>
  {pending_text}
</div></div>
<div class="card" style="margin-top:14px"><div class="card-head"><div><h3>Move up the line</h3>
<p>Each friend who joins and confirms moves you up {BUMP_PER_REFERRAL} spots.</p></div></div>
<div class="card-body stack">
  <div class="row"><input id="reflink" class="grow" value="{esc(link)}" readonly onclick="this.select()">
  <button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('reflink').value);this.innerText='Copied!'">Copy link</button></div>
  <div class="share-grid">{share_buttons}</div>
  {next_html}
</div></div>
<div class="card" style="margin-top:14px"><div class="card-head"><h3>Rewards</h3></div>
<div class="card-body" style="padding-top:6px;padding-bottom:6px">{rewards}</div></div>
<p class="muted small" style="text-align:center;margin-top:16px">Bookmark this private page to check your spot. Share your invite link, not this page.</p>
"""
    return public_shell("Your spot - " + cfg["product"], inner)


def privacy_page():
    cfg = settings()
    product = esc(cfg['product'])
    contact = esc(cfg.get('contact_email') or from_address())
    sender = {'resend': 'Resend', 'smtp': 'our email provider', 'console': 'our email provider'}[email_provider()]
    inner = f"""<div class="card"><div class="card-body prose" style="padding:32px">
<h1 style="font-size:24px;margin-bottom:18px">{product} privacy notice</h1>
<p><b>What we collect when you join the waitlist:</b> your email address, your name if you give it, which referral link
or social post brought you, and a one-way hash of your IP address (used only to prevent abuse).</p>
<p><b>When you click a link we posted on social media:</b> we record the time and a one-way hash of your IP address, and
set a cookie (<code>lk_post</code>, 30 days) so we know which post brought you if you sign up later.</p>
<p><b>Why:</b> to run the waitlist and referral program, send you the confirmation email and updates about {product},
and measure which posts work.</p>
<p><b>Who processes it:</b> emails are delivered through {sender}. We don't sell your data.</p>
<p><b>Your choices:</b> every update email has an unsubscribe link. To see or delete your data, contact {contact}.</p>
</div></div>"""
    return public_shell(f"{cfg['product']} - Privacy", inner)


def unsubscribe_page(subscriber, done=False):
    if done or subscriber["unsubscribed"]:
        return message_page("Unsubscribed", "You won't receive further updates. You keep your spot on the waitlist.")
    inner = f"""<div class="card"><div class="card-body" style="text-align:center;padding:40px 28px">
<h1 style="font-size:26px">Unsubscribe?</h1><p class="muted" style="margin:10px 0 22px">Stop receiving waitlist updates at {esc(subscriber['email'])}. You keep your spot.</p>
<form method="post" action="/unsubscribe/{esc(subscriber['private_token'])}"><button class="btn">Unsubscribe</button></form></div></div>"""
    return public_shell("Unsubscribe", inner)


def admin_login_page(error=""):
    error_html = f'<div class="alert alert-danger" style="margin-bottom:14px">{esc(error)}</div>' if error else ""
    body = f"""<div class="public"><div class="public-inner" style="max-width:400px;margin-top:40px">
<div class="hero"><div class="logo big-logo">L</div><h1 style="font-size:28px;margin:18px 0 6px">LaunchKit</h1>
<p class="muted">Sign in to your dashboard</p></div>
<div class="card"><div class="card-body">{error_html}
<form method="post" action="/admin/login"><div class="field"><label for="token">Admin password</label>
<input id="token" type="password" name="token" placeholder="Admin token" autofocus required></div>
<button class="btn btn-block btn-lg">Sign in</button></form>
<p class="help" style="margin-top:12px">Your password is <code>ADMIN_TOKEN</code> in the <code>.env</code> file.</p></div></div></div></div>"""
    return page("Sign in - LaunchKit", body)


def admin_shell(active, title, subtitle, content, message="", actions=""):
    nav_items = [
        ("overview", "/admin", "Overview"),
        ("posts", "/admin/posts", "Posts"),
        ("marketing", "/admin/marketing", "Marketing"),
        ("emails", "/admin/emails", "Emails"),
        ("settings", "/admin/settings", "Settings"),
    ]
    nav = "".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">{icon(key)}{label}</a>'
        for key, href, label in nav_items
    )
    body = f"""<div class="app">
<aside class="sidebar"><div class="brand"><span class="logo">L</span>LaunchKit</div>
<nav class="nav">{nav}</nav><div class="spacer"></div>
<div class="nav-section">Shortcuts</div>
<nav class="nav shortcuts"><a href="/" target="_blank">{icon('external')}Public page</a>
<a href="/admin/export.csv">{icon('download')}Export CSV</a><a href="/admin/backup.db">{icon('download')}Download backup</a>
<a href="/admin/logout">{icon('logout')}Log out</a></nav></aside>
<main><header class="topbar"><div><h1>{esc(title)}</h1><p class="muted" style="margin-top:4px">{subtitle}</p></div>
<div class="row">{actions}</div></header>
<div class="content stack">{flash_html(message)}{content}</div></main></div>"""
    return page(f"{title} - LaunchKit admin", body)


def ai_progress_html():
    with ai_jobs_lock:
        pending_ai, failed_ai = ai_jobs["pending"], ai_jobs["failed"]
    html = ""
    if pending_ai:
        html += f'<div class="alert alert-info">AI is writing {plural(pending_ai, "more post")} in the background - refresh in a moment.</div>'
    if failed_ai:
        html += f'<div class="alert alert-warn">{plural(failed_ai, "AI post")} fell back to templates - see the note on each post.</div>'
    return html


def overview_page(message=""):
    cfg = settings()
    total = waitlist_count()
    pending_confirmation = query("SELECT COUNT(*) AS n FROM subscribers WHERE verified = 0", one=True)["n"]
    credited = query("SELECT COUNT(*) AS n FROM subscribers WHERE referral_credited = 1", one=True)["n"]
    today_date = local_now().date()
    today = query("SELECT COUNT(*) AS n FROM subscribers WHERE created_at >= ?", (today_date.isoformat(),), one=True)["n"]
    sharers = query("SELECT COUNT(*) AS n FROM subscribers WHERE referrals > 0", one=True)["n"]
    post_count = query("SELECT COUNT(*) AS n FROM posts", one=True)["n"]

    steps = [
        (cfg["product"] != DEFAULT_SETTINGS["product"], "Describe your product", "Name, tagline and launch date.", "/admin/settings", "Open settings"),
        (email_provider() != "console", "Connect email delivery", "Add a Resend key or SMTP details to .env so confirmation emails reach people.", "/admin/emails", "Email setup"),
        (post_count > 0, "Plan your first posts", "Generate a 2-week build-in-public plan in one click.", "/admin/posts", "Create posts"),
        (total > 0, "Get your first signup", "Share your public waitlist page.", "/", "Open public page"),
        (AI_ENABLED, "Turn on the AI writer (optional)", "Add AI_API_KEY to .env to have posts written for you.", "/admin/settings", "See how"),
    ]
    done_count = sum(1 for done, *_ in steps if done)
    checklist = ""
    if done_count < len(steps):
        items = "".join(
            f"""<li class="{'done' if done else ''}"><span class="check">{CHECK_SVG if done else ''}</span>
<div class="grow"><div class="step-title" style="font-weight:600">{esc(title)}</div><div class="muted small">{esc(detail)}</div></div>
{'' if done else f'<a class="btn btn-secondary btn-sm" href="{href}" {"target=_blank" if href == "/" else ""}>{esc(label)}</a>'}</li>"""
            for done, title, detail, href, label in steps
        )
        checklist = f"""<div class="card"><div class="card-head"><div><h3>Get set up</h3><p>{done_count} of {len(steps)} done</p></div>
<div style="width:180px" class="progress"><span style="width:{100 * done_count / len(steps):.0f}%"></span></div></div>
<ul class="steps" style="margin-top:-1px">{items}</ul></div>"""

    stats = [
        ("On the waitlist", total, "Confirmed sign-ups" if REQUIRE_VERIFICATION else "People in line"),
        ("Awaiting confirmation", pending_confirmation, "Haven't clicked the email link yet"),
        ("Joined today", today, today_date.strftime("%B %d").replace(" 0", " ")),
        ("Invited by friends", credited, f"{sharers} {'person' if sharers == 1 else 'people'} shared their link"),
    ]
    stats_html = "".join(
        f'<div class="card stat"><div class="stat-label">{esc(label)}</div><div class="stat-value">{value}</div>'
        f'<div class="stat-hint">{esc(hint)}</div></div>'
        for label, value, hint in stats
    )

    counts = {row["day"]: row["n"] for row in query(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM subscribers WHERE created_at >= ? GROUP BY day",
        ((today_date - timedelta(days=13)).isoformat(),),
    )}
    days = [today_date - timedelta(days=offset) for offset in range(13, -1, -1)]
    peak = max([counts.get(day.isoformat(), 0) for day in days] + [1])
    chart = "".join(
        f'<div class="col" title="{day:%b %d}: {plural(counts.get(day.isoformat(), 0), "signup")}">'
        f'<div class="bar {"zero" if not counts.get(day.isoformat()) else ""}" style="height:{max(3, 100 * counts.get(day.isoformat(), 0) / peak):.0f}%"></div>'
        f'<span class="lbl">{day.day}</span></div>'
        for day in days
    )
    signups_14 = sum(counts.values())

    sources = query("SELECT COALESCE(source,'direct') AS s, COUNT(*) AS n FROM subscribers GROUP BY s ORDER BY n DESC LIMIT 8")
    sources_html = "".join(f"<tr><td>{esc(r['s'])}</td><td>{r['n']}</td></tr>" for r in sources)
    top = query("SELECT email, referrals FROM subscribers WHERE referrals > 0 ORDER BY referrals DESC LIMIT 8")
    top_html = "".join(f"<tr><td>{esc(r['email'])}</td><td>{r['referrals']}</td></tr>" for r in top)

    provider_label = {
        "resend": "Sending through Resend",
        "smtp": "Sending through your SMTP server",
        "console": "Emails are not sent yet - they only appear in the outbox",
    }[email_provider()]
    status_rows = [
        ("Email delivery", email_provider() != "console", provider_label, True),
        ("AI writer", AI_ENABLED, f"AI writer: {'on (' + esc(AI_MODEL) + ')' if AI_ENABLED else 'off - posts use ready-made templates'}", False),
        ("Bluesky", bool(BSKY_HANDLE and BSKY_APP_PASSWORD), "Posts publish automatically" if BSKY_HANDLE else "Optional - post automatically to Bluesky", False),
        ("Discord", bool(DISCORD_WEBHOOK), "Posts publish automatically" if DISCORD_WEBHOOK else "Optional - post automatically to a Discord channel", False),
    ]
    status_html = "".join(
        f'<tr><td><b style="color:var(--text)">{name}</b><div class="muted small">{detail}</div></td>'
        f'<td style="text-align:right">{status_badge(on, important)}</td></tr>'
        for name, on, detail, important in status_rows
    )

    def table_or_empty(rows_html, heads, empty_text):
        if not rows_html:
            return f'<div class="empty small">{empty_text}</div>'
        return f'<table class="num-table"><tr><th>{heads[0]}</th><th>{heads[1]}</th></tr>{rows_html}</table>'

    if signups_14:
        chart_html = f'<div class="card-body"><div class="chart">{chart}</div></div>'
    else:
        chart_html = ('<div class="empty"><h3>No sign-ups yet</h3><p>Share your public page and new sign-ups will show up here day by day.</p>'
                      '<a class="btn btn-secondary" href="/" target="_blank">Open public page</a></div>')

    content = f"""{checklist}
<div class="grid g4">{stats_html}</div>
<div class="grid g2" style="align-items:start">
<div class="card"><div class="card-head"><div><h3>Sign-ups</h3><p>Last 14 days &middot; {plural(signups_14, "sign-up")}</p></div></div>
{chart_html}</div>
<div class="card"><div class="card-head"><div><h3>Connections</h3><p>Set these up in the .env file, then restart the app</p></div></div>
<table>{status_html}</table></div>
</div>
<div class="grid g2">
<div class="card"><div class="card-head"><div><h3>Where sign-ups come from</h3><p>Which links and sites bring people in</p></div></div>{table_or_empty(sources_html, ("Source", "Sign-ups"), "Nothing yet - this fills in as people join.")}</div>
<div class="card"><div class="card-head"><div><h3>Top referrers</h3><p>Your most active supporters</p></div></div>{table_or_empty(top_html, ("Subscriber", "Friends"), "Nobody has invited a friend yet.")}</div>
</div>"""
    actions = '<a class="btn btn-secondary" href="/" target="_blank">View public page</a><a class="btn" href="/admin/posts">Create posts</a>'
    return admin_shell("overview", "Overview", f"{esc(cfg['product'])} pre-launch dashboard", content, message, actions)


def posts_page(message=""):
    clicks_by_post, signups_by_post = post_stats()
    campaign_names = {row["id"]: row["name"] for row in query("SELECT id, name FROM campaigns")}
    posts = query(
        """SELECT * FROM posts ORDER BY
           CASE status WHEN 'due' THEN 0 WHEN 'failed' THEN 1 WHEN 'draft' THEN 2 WHEN 'scheduled' THEN 3 ELSE 4 END,
           scheduled_at, id LIMIT 200"""
    )
    post_items = []
    for post in posts:
        rendered = render_live(post["body"], post["platform"], post["link_code"])
        limit = PLATFORM_LIMITS.get(post["platform"], 99999)
        length_html = (f'<span class="{"over" if len(rendered) > limit else ""}">{len(rendered)}/{limit} characters</span>'
                       if limit < 10000 else "")
        meta = [f'<b style="color:var(--text)">{PLATFORM_LABELS.get(post["platform"], post["platform"])}</b>', badge(post["status"])]
        if post["scheduled_at"]:
            meta.append(esc(post["scheduled_at"].replace("T", " ")))
        if post["campaign_id"] in campaign_names:
            meta.append(esc(campaign_names[post["campaign_id"]]))
        meta.append(f'{plural(clicks_by_post.get(post["link_code"], 0), "click")} &middot; '
                    f'{plural(signups_by_post.get(post["link_code"], 0), "signup")}')
        actions = ""
        if post["status"] != "posted":
            if post["platform"] in AUTO_POST_PLATFORMS:
                actions += f'<form method="post" action="/admin/post/{post["id"]}/publish"><button class="btn btn-sm">Post now</button></form>'
            else:
                actions += f'<a class="btn btn-sm" target="_blank" href="{esc(share_url(post["platform"], rendered, post_link(post["link_code"])))}">Share</a>'
                actions += f'<form method="post" action="/admin/post/{post["id"]}/done"><button class="btn btn-secondary btn-sm">Mark posted</button></form>'
            if AI_ENABLED:
                actions += f'<form method="post" action="/admin/post/{post["id"]}/ai"><button class="btn btn-secondary btn-sm">AI rewrite</button></form>'
        actions += (f'<form method="post" action="/admin/post/{post["id"]}/delete" onsubmit="return confirm(\'Delete this post?\')">'
                    f'<button class="btn btn-danger btn-sm">Delete</button></form>')
        edit_form = f"""<details style="margin-top:10px"><summary>Edit post</summary><form method="post" action="/admin/post/{post['id']}/edit">
<div class="field"><textarea name="body">{esc(post['body'])}</textarea><span class="help">Placeholders {{link}}, {{count}} and {{days_left}} fill in when you post.</span></div>
<div class="row"><input type="datetime-local" name="scheduled_at" value="{esc(post['scheduled_at'] or '')}" style="max-width:240px">
<button class="btn btn-secondary btn-sm">Save changes</button></div></form></details>"""
        note = f'<span class="muted small">{esc(post["result"])}</span>' if post["result"] else ""
        post_items.append(
            f"""<div class="post"><div class="row between"><div class="post-meta">{' &middot; '.join(meta)}</div><div class="row" style="gap:6px">{actions}</div></div>
<div class="post-text">{esc(rendered)}</div><div class="row between small muted">{length_html}{note}</div>{edit_form}</div>"""
        )
    if post_items:
        posts_html = "".join(post_items)
    else:
        posts_html = """<div class="empty"><h3>No posts yet</h3><p>Generate a 2-week plan above, or write your first post.</p></div>"""

    template_options = "".join(f'<option value="{t}">{t.replace("_", " ").capitalize()}</option>' for t in TEMPLATES)
    platform_options = "".join(f'<option value="{p}">{PLATFORM_LABELS[p]}</option>' for p in PLATFORM_LIMITS)
    ai_toggle = ('<label class="toggle"><input type="checkbox" name="use_ai" checked> Write with AI</label>' if AI_ENABLED
                 else '<span class="help">Tip: add AI_API_KEY to .env and the AI writer will draft posts for you.</span>')
    tomorrow = (local_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    content = f"""{ai_progress_html()}
<div class="grid g2">
<div class="card"><div class="card-head"><div><h3>Generate a 2-week plan</h3><p>{len(CONTENT_PLAN)} days of build-in-public posts, scheduled for you</p></div></div>
<div class="card-body"><form method="post" action="/admin/plan">
<div class="field"><span class="label">Platforms</span><div class="chips">{platform_chips(("x", "bluesky", "linkedin"))}</div></div>
<div class="row"><div class="field grow"><label>Start date</label><input type="date" name="start" value="{tomorrow}"></div>
<div class="field grow"><label>Post time</label><input type="time" name="time" value="09:00"></div></div>
<div class="row between">{ai_toggle}<button class="btn">Generate plan</button></div></form></div></div>
<div class="card"><div class="card-head"><div><h3>Write a post</h3><p>Start from a template or write your own</p></div></div>
<div class="card-body"><form method="post" action="/admin/compose">
<div class="row"><div class="field grow"><label>Platform</label><select name="platform">{platform_options}</select></div>
<div class="field grow"><label>Template</label><select name="template"><option value="free">Free-form</option>{template_options}</select></div></div>
<div class="field"><label>Text {('or notes for the AI' if AI_ENABLED else '')}</label>
<textarea name="body" placeholder="e.g. Shipped dark mode today. {{count}} people on the waitlist: {{link}}"></textarea>
<span class="help">Leave empty to use the template. {{link}}, {{count}} and {{days_left}} fill in automatically.</span></div>
<div class="row"><div class="field grow"><label>Campaign</label><select name="campaign_id">{campaign_options()}</select></div>
<div class="field grow"><label>Schedule (optional)</label><input type="datetime-local" name="scheduled_at"></div></div>
<div class="row between">{ai_toggle if AI_ENABLED else '<span></span>'}<button class="btn">Save post</button></div></form></div></div>
</div>
<div class="card"><div class="card-head"><div><h3>Your posts</h3><p>"Ready to post" items are due now - click Share to publish</p></div>
<span class="muted small">{plural(len(posts), "post")}</span></div>{posts_html}</div>"""
    return admin_shell("posts", "Posts", "Plan, write and publish your build-in-public content", content, message)


def emails_page(message="", prefill_subject="", prefill_body=""):
    provider = email_provider()
    if provider == "console":
        delivery = ('<div class="alert alert-warn"><b>Emails are not being delivered yet.</b> They are printed in the LaunchKit console '
                    'window and listed below. Add <code>RESEND_API_KEY</code> or <code>SMTP_HOST</code> to <code>.env</code> and restart to send real emails.</div>')
    else:
        via = "Resend" if provider == "resend" else f"SMTP ({esc(SMTP_HOST)})"
        delivery = f'<div class="alert alert-success">Emails are delivered via {via} from <code>{esc(from_address())}</code>.</div>'
    email_counts = {row["status"]: row["n"] for row in query("SELECT status, COUNT(*) AS n FROM emails GROUP BY status")}
    unsubscribed = query("SELECT COUNT(*) AS n FROM subscribers WHERE unsubscribed = 1", one=True)["n"]
    audience = query("SELECT COUNT(*) AS n FROM subscribers WHERE verified = 1 AND unsubscribed = 0", one=True)["n"]
    stats = [("Sent", email_counts.get("sent", 0)), ("Queued", email_counts.get("queued", 0)),
             ("Failed", email_counts.get("failed", 0)), ("Unsubscribed", unsubscribed)]
    stats_html = "".join(f'<div class="card stat"><div class="stat-label">{l}</div><div class="stat-value">{v}</div></div>' for l, v in stats)

    rows = []
    for row in query("SELECT * FROM emails ORDER BY id DESC LIMIT 30"):
        preview = (f'<details><summary>Preview</summary><div class="post-text">{esc(row["text"])}</div></details>'
                   if provider == "console" else "")
        error = f'<div class="over small">{esc(row["error"])}</div>' if row["error"] else ""
        rows.append(
            f"<tr><td style='white-space:nowrap'>{esc(row['created_at'].replace('T', ' '))}</td><td>{esc(row['to_email'])}</td>"
            f"<td>{esc(row['kind'].replace('_', ' '))}</td><td>{esc(row['subject'])}{preview}{error}</td><td>{badge(row['status'])}</td></tr>"
        )
    outbox = (f'<table><tr><th>When</th><th>To</th><th>Type</th><th>Subject</th><th>Status</th></tr>{"".join(rows)}</table>'
              if rows else '<div class="empty"><h3>No emails yet</h3><p>Confirmation and welcome emails appear here as people join.</p></div>')
    content = f"""{delivery}
<div class="grid g4">{stats_html}</div>
<div class="grid g2">
<div class="card"><div class="card-head"><div><h3>Email your waitlist</h3><p>Goes to {plural(audience, "confirmed subscriber")} &middot; unsubscribe link added automatically</p></div></div>
<div class="card-body"><form method="post" action="/admin/broadcast">
<div class="field"><label>Subject</label><input name="subject" value="{esc(prefill_subject)}" placeholder="{{product}} is live - you're #{{position}}" required></div>
<div class="field"><label>Message</label><textarea name="body" required style="min-height:170px" placeholder="Hi {{name}},&#10;&#10;We're launching!&#10;&#10;Your spot: {{status_link}}&#10;Your invite link: {{referral_link}}">{esc(prefill_body)}</textarea>
<span class="help">Personalise with {{name}}, {{position}}, {{referral_link}}, {{status_link}} and {{product}}.</span></div>
<div class="field"><label>Send a test first (optional)</label><input name="test_address" type="email" placeholder="you@example.com">
<span class="help">If filled in, only this address gets the email.</span></div>
<div class="row" style="justify-content:flex-end"><button class="btn" onclick="return this.form.test_address.value || confirm('Send to {audience} people?')">Send email</button></div>
</form></div></div>
<div class="stack"><div class="card"><div class="card-head"><div><h3>Check delivery</h3><p>Send yourself a test email</p></div></div>
<div class="card-body"><form method="post" action="/admin/test-email" class="row"><input class="grow" name="to" type="email" placeholder="you@example.com" required>
<button class="btn btn-secondary">Send test</button></form></div></div>
<div class="card"><div class="card-head"><div><h3>Confirmation emails</h3><p>Referrals only count after a friend confirms</p></div>
<span class="badge dot {'b-on' if REQUIRE_VERIFICATION else 'b-off'}">{'On' if REQUIRE_VERIFICATION else 'Off'}</span></div>
<div class="card-body"><form method="post" action="/admin/emails/retry" class="row between"><span class="muted small">{plural(email_counts.get('failed', 0), 'failed email')}</span>
<button class="btn btn-secondary btn-sm">Retry failed</button></form></div></div></div>
</div>
<div class="card"><div class="card-head"><div><h3>Outbox</h3><p>Latest 30 emails</p></div></div>{outbox}</div>"""
    return admin_shell("emails", "Emails", "Confirmation, welcome and reward emails, plus announcements to your list", content, message)


def settings_page(message=""):
    cfg = settings()
    groups = []
    for group_title, fields in SETTING_FIELDS:
        inputs = "".join(
            f"""<div class="field"><label for="s-{key}">{esc(label)}</label>
<input id="s-{key}" name="{key}" type="{kind}" value="{esc(cfg.get(key, ''))}"><span class="help">{esc(help_text)}</span></div>"""
            for key, label, help_text, kind in fields
        )
        groups.append(f'<div class="card"><div class="card-head"><h3>{group_title}</h3></div><div class="card-body">{inputs}</div></div>')
    def needs(*keys):
        return "Add " + " and ".join(f"<code>{key}</code>" for key in keys)

    email_detail = {
        "resend": "Sending through Resend",
        "smtp": f"Sending through {esc(SMTP_HOST)}",
        "console": needs("RESEND_API_KEY") + " or " + "<code>SMTP_HOST</code>",
    }[email_provider()]
    env_rows = [
        ("Email delivery", email_provider() != "console", email_detail, True),
        ("AI writer", AI_ENABLED, esc(AI_MODEL) if AI_ENABLED else needs("AI_API_KEY"), False),
        ("Bluesky", bool(BSKY_HANDLE and BSKY_APP_PASSWORD), esc(BSKY_HANDLE) or needs("BSKY_HANDLE", "BSKY_APP_PASSWORD"), False),
        ("Discord", bool(DISCORD_WEBHOOK), "Webhook connected" if DISCORD_WEBHOOK else needs("DISCORD_WEBHOOK"), False),
        ("Time zone", True if TIMEZONE else None, esc(TIMEZONE) or "Uses this computer's clock", False),
        ("Daily backups", BACKUP_KEEP > 0, f"Keeps the last {BACKUP_KEEP}" if BACKUP_KEEP > 0 else needs("BACKUP_KEEP"), False),
    ]
    env_html = "".join(
        f'<tr><td><b style="color:var(--text)">{name}</b><div class="muted small" style="margin-top:2px">{detail}</div></td>'
        f'<td style="text-align:right">{status_badge(on, important)}</td></tr>'
        for name, on, detail, important in env_rows
    )
    embed = f'<div id="launchkit"></div><script src="{BASE_URL}/embed.js"></script>'
    content = f"""<div class="grid g2" style="align-items:start">
<form method="post" action="/admin/settings" class="stack">{''.join(groups)}
<div class="savebar"><span class="muted small">Changes apply to your public page right away.</span><button class="btn">Save changes</button></div></form>
<div class="stack">
<div class="card"><div class="card-head"><div><h3>Connections</h3><p>Set these in the <code>.env</code> file next to the app, then restart</p></div></div>
<table>{env_html}</table></div>
<div class="card"><div class="card-head"><div><h3>Embed on your website</h3><p>Adds the signup form to any page</p></div></div>
<div class="card-body"><div class="row"><input id="embed" class="grow" readonly value="{esc(embed)}" onclick="this.select()">
<button class="btn btn-secondary" onclick="navigator.clipboard.writeText(document.getElementById('embed').value);this.innerText='Copied!'">Copy</button></div></div></div>
</div></div>"""
    return admin_shell("settings", "Settings", "Your product details, sharing and privacy", content, message)


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

    def stats_table(table, label, first_heading):
        rows = sorted(table.items(), key=lambda item: (-item[1][2], -item[1][1]))
        if not rows:
            return '<div class="empty small">No posts yet - create some on the Posts page.</div>'
        body = "".join(
            f"<tr><td>{esc(label(key))}</td><td>{n_posts}</td><td>{clicks}</td><td>{signups}</td><td>{conversion(signups, clicks)}</td></tr>"
            for key, (n_posts, clicks, signups) in rows
        )
        return (f'<table class="num-table"><tr><th>{first_heading}</th><th>Posts</th><th>Clicks</th><th>Sign-ups</th><th>Conv.</th></tr>'
                f'{body}</table>')

    campaign_html = stats_table(by_campaign, lambda key: campaigns.get(key, "No campaign"), "Campaign")
    platform_html = stats_table(by_platform, lambda key: PLATFORM_LABELS.get(key, key), "Platform")

    total_clicks_all = sum(clicks_by_post.values())
    total_signups_all = sum(signups_by_post.values())
    kpis = [
        ("Tracked clicks", total_clicks_all, "Unique visitors from your posts"),
        ("Sign-ups from posts", total_signups_all, "Attributed to a specific post"),
        ("Conversion", conversion(total_signups_all, total_clicks_all), "Sign-ups per click"),
        ("Campaigns", len(campaigns), "Plans, launch weeks and your own"),
    ]
    kpi_html = "".join(
        f'<div class="card stat"><div class="stat-label">{l}</div><div class="stat-value">{v}</div><div class="stat-hint">{h}</div></div>'
        for l, v, h in kpis
    )

    ranked_posts = sorted(
        (post for post in posts if clicks_by_post.get(post["link_code"]) or signups_by_post.get(post["link_code"])),
        key=lambda post: (-signups_by_post.get(post["link_code"], 0), -clicks_by_post.get(post["link_code"], 0)),
    )[:10]
    if ranked_posts:
        top_rows = "".join(
            f"<tr><td>{PLATFORM_LABELS.get(post['platform'], post['platform'])}</td>"
            f"<td style='text-align:left'>{esc(shorten(render_live(post['body'], post['platform'], post['link_code']), 120))}</td>"
            f"<td>{clicks_by_post.get(post['link_code'], 0)}</td><td>{signups_by_post.get(post['link_code'], 0)}</td>"
            f"<td>{conversion(signups_by_post.get(post['link_code'], 0), clicks_by_post.get(post['link_code'], 0))}</td></tr>"
            for post in ranked_posts
        )
        top_html = (f'<table class="num-table"><tr><th>Platform</th><th style="text-align:left">Post</th><th>Clicks</th>'
                    f'<th>Sign-ups</th><th>Conv.</th></tr>{top_rows}</table>')
    else:
        top_html = ('<div class="empty"><h3>No clicks yet</h3><p>Share posts from the Posts page - every post has its own '
                    'tracked link, so results show up here.</p><a class="btn btn-secondary" href="/admin/posts">Go to posts</a></div>')

    since = (local_now() - timedelta(days=30)).isoformat(timespec="seconds")
    hourly = {row["hour"]: row["n"] for row in query(
        "SELECT substr(created_at, 12, 2) AS hour, COUNT(*) AS n FROM clicks WHERE created_at >= ? GROUP BY hour", (since,)
    )}
    max_hourly = max(list(hourly.values()) + [1])
    hours_chart = "".join(
        f'<div class="col" title="{hour:02d}:00 - {plural(hourly.get(f"{hour:02d}", 0), "click")}">'
        f'<div class="bar {"zero" if not hourly.get(f"{hour:02d}") else ""}" style="height:{max(3, 100 * hourly.get(f"{hour:02d}", 0) / max_hourly):.0f}%"></div>'
        f'<span class="lbl">{hour if hour % 3 == 0 else ""}</span></div>'
        for hour in range(24)
    )
    total_clicks = sum(hourly.values())
    if total_clicks >= 30:
        best = sorted(hourly.items(), key=lambda item: -item[1])[:3]
        best_text = "Your audience clicks most at " + ", ".join(f"{hour}:00" for hour, _ in best) + ". Schedule posts just before these hours."
    else:
        best_text = f"{plural(total_clicks, 'click')} in the last 30 days - a recommendation appears after 30. Until then, try different posting times."

    tasks = query("SELECT * FROM tasks ORDER BY done, due_date, id")
    if tasks:
        task_items = "".join(
            f"""<li class="{'done' if task['done'] else ''}"><form method="post" action="/admin/task/{task['id']}/toggle">
<button class="check" style="cursor:pointer;background:{'var(--success)' if task['done'] else 'transparent'}" title="{'Mark as not done' if task['done'] else 'Mark as done'}">{CHECK_SVG if task['done'] else ''}</button></form>
<div class="grow"><div class="step-title" style="font-weight:600">{esc(task['title'])}</div><div class="muted small">{esc(task['details'] or '')}</div></div>
<div style="text-align:right"><div class="muted small" style="white-space:nowrap">{esc(nice_date(task['due_date']))}</div>
{f'<a class="btn btn-secondary btn-sm" style="margin-top:6px" href="{esc(task["action_url"])}">Open</a>' if task['action_url'] else ''}</div></li>"""
            for task in tasks
        )
        tasks_html = f'<ul class="steps" style="margin-top:-1px">{task_items}</ul>'
    else:
        tasks_html = '<div class="empty small">No tasks yet - generate a launch campaign to get a step-by-step checklist.</div>'

    ai_toggle = '<label class="toggle"><input type="checkbox" name="use_ai" checked> Write with AI</label>' if AI_ENABLED else "<span></span>"
    if AI_ENABLED:
        repurpose_html = f"""<form method="post" action="/admin/repurpose">
<div class="field"><label>Source content</label><textarea name="source" required placeholder="Paste a changelog, blog post, release notes or a brain-dump..."></textarea></div>
<div class="field"><span class="label">Create drafts for</span><div class="chips">{platform_chips(("x", "linkedin", "reddit"))}</div></div>
<div class="row"><div class="field grow" style="margin:0"><select name="campaign_id">{campaign_options()}</select></div><button class="btn">Create drafts</button></div></form>"""
    else:
        repurpose_html = ('<div class="empty small" style="padding:18px 10px"><h3>Turn on the AI writer to use this</h3>'
                          '<p>Add <code>AI_API_KEY</code> (or <code>AI_BASE_URL</code> for a local Ollama) to <code>.env</code>, '
                          'then restart LaunchKit.</p></div>')

    if posts:
        results_html = f"""<div class="grid g4">{kpi_html}</div>
<div class="grid g2">
<div class="card"><div class="card-head"><div><h3>Campaigns</h3><p>Which campaigns bring sign-ups</p></div></div>{campaign_html}</div>
<div class="card"><div class="card-head"><div><h3>Platforms</h3><p>Where your audience responds</p></div></div>{platform_html}</div>
</div>
<div class="card"><div class="card-head"><div><h3>Top posts</h3><p>Ranked by sign-ups, then clicks</p></div></div>{top_html}</div>"""
    else:
        results_html = """<div class="card"><div class="empty" style="padding:44px 20px">
<h3>Your results will appear here</h3>
<p style="max-width:520px;margin:0 auto">Every post you create gets its own tracked link. Once you share posts, this page shows
which ones bring clicks and sign-ups, per campaign and per platform.</p>
<a class="btn" href="/admin/posts">Create your first posts</a></div></div>"""

    insights_cards = ""
    if tasks:
        insights_cards += f'<div class="card"><div class="card-head"><div><h3>Launch checklist</h3><p>Tick items off as you go</p></div></div>{tasks_html}</div>'
    if total_clicks:
        insights_cards += (f'<div class="card"><div class="card-head"><div><h3>Best time to post</h3><p>{esc(best_text)}</p></div></div>'
                           f'<div class="card-body"><div class="chart" style="height:110px;gap:3px">{hours_chart}</div></div></div>')
    insights_html = f'<div class="grid g2" style="align-items:start">{insights_cards}</div>' if insights_cards else ""

    content = f"""{ai_progress_html()}
{results_html}
<div class="grid g2" style="align-items:start">
<div class="card"><div class="card-head"><div><h3>Launch-week campaign</h3><p>Posts from 7 days before to 7 days after {esc(nice_date(cfg['launch_date']))}, plus a launch checklist</p></div></div>
<div class="card-body"><form method="post" action="/admin/launch">
<div class="field"><span class="label">Platforms</span><div class="chips">{platform_chips(("x", "linkedin", "reddit"))}</div></div>
<div class="field"><label>Post time</label><input type="time" name="time" value="09:00" style="max-width:180px"></div>
<div class="row between">{ai_toggle}<button class="btn">Generate launch campaign</button></div>
<p class="help" style="margin-top:10px">Dates already in the past are skipped. Change the launch date in Settings.</p></form></div></div>
<div class="stack">
<div class="card"><div class="card-head"><div><h3>Repurpose with AI</h3><p>One piece of content in, a native draft per platform out</p></div></div>
<div class="card-body">{repurpose_html}</div></div>
<div class="card"><div class="card-head"><div><h3>New campaign</h3><p>Group posts to compare results</p></div></div>
<div class="card-body"><form method="post" action="/admin/campaign" class="row"><input class="grow" name="name" placeholder="e.g. Beta announcement" required>
<button class="btn btn-secondary">Create</button></form></div></div>
</div></div>
{insights_html}"""
    subtitle = "See which posts, platforms and campaigns bring you sign-ups"
    return admin_shell("marketing", "Marketing", subtitle, content, message)


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
        target = {"#social": "/admin/posts", "#emails": "/admin/emails", "#settings": "/admin/settings"}.get(anchor, "/admin")
        self.redirect(target + (f"?msg={urllib.parse.quote(message)}" if message else ""))

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
            if "bs" in params or "bb" in params:  # launch-email links from older checklists
                prefill = urllib.parse.urlencode({"bs": params.get("bs", [""])[0], "bb": params.get("bb", [""])[0]})
                return self.redirect(f"/admin/emails?{prefill}")
            return self.send(200, overview_page(params.get("msg", [""])[0]))
        if path in ("/admin/posts", "/admin/emails", "/admin/settings"):
            if not self.is_admin():
                return self.redirect("/admin")
            message = params.get("msg", [""])[0]
            if path == "/admin/posts":
                return self.send(200, posts_page(message))
            if path == "/admin/emails":
                return self.send(200, emails_page(message, params.get("bs", [""])[0], params.get("bb", [""])[0]))
            return self.send(200, settings_page(message))
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
            return self.admin_redirect("Test email queued - check the outbox below", "#emails")

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
