"""
End-to-end tests for LaunchKit (standard library only).

Run from the project folder:   python -m unittest discover -s tests -v
or double-click run_tests.bat.

The app runs as a real server against throwaway databases. Resend, the AI API and an
SMTP server are replaced by local fakes, so no real emails or API calls are made.
"""

import http.client
import json
import os
import re
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
APP = APP_DIR / "launchkit.py"
ADMIN_TOKEN = "test-admin-token-123"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def base_env(tmp_dir, **overrides):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("RESEND_", "SMTP_", "AI_", "BSKY_", "DISCORD_")) or key in ("TIMEZONE", "LAUNCHKIT_ENV"):
            env.pop(key)
    env.update(
        LAUNCHKIT_ENV=str(Path(tmp_dir) / "no.env"),  # never read the developer's real .env
        DB_PATH=str(Path(tmp_dir) / "app.db"),
        BACKUP_DIR=str(Path(tmp_dir) / "backups"),
        ADMIN_TOKEN=ADMIN_TOKEN,
        EMAIL_SEND_DELAY="0",
        NO_PROXY="*",
    )
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def run_python(code, env, timeout=30):
    return subprocess.run([sys.executable, "-c", code], cwd=APP_DIR, env=env,
                          capture_output=True, text=True, timeout=timeout)


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    return predicate()


# ----------------------------------------------------------------------------
# Fake external services
# ----------------------------------------------------------------------------
class FakeApis:
    """Fake Resend (/emails) and OpenAI-compatible chat API (/v1/chat/completions)."""

    def __init__(self):
        self.emails = []
        self.ai_prompts = []
        apis = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/emails":
                    apis.emails.append({"auth": self.headers.get("Authorization"), **payload})
                    response = {"id": f"email-{len(apis.emails)}"}
                else:
                    prompt = payload["messages"][1]["content"]
                    apis.ai_prompts.append(prompt)
                    content = "Repurposed for you {link}" if "source material" in prompt else '"AI wrote this, {count} waiting"'
                    response = {"choices": [{"message": {"content": content}}]}
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def emails_to(self, address, subject_part=""):
        return [mail for mail in self.emails if mail["to"] == [address] and subject_part in mail["subject"]]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeSmtp:
    def __init__(self):
        self.messages = []
        smtp = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"220 fake\r\n")
                while True:
                    line = self.rfile.readline().decode(errors="replace").strip().upper()
                    if not line:
                        return
                    if line.startswith("DATA"):
                        self.wfile.write(b"354 go\r\n")
                        lines = []
                        while (data_line := self.rfile.readline().decode(errors="replace")).rstrip("\r\n") != ".":
                            lines.append(data_line)
                        smtp.messages.append("".join(lines))
                        self.wfile.write(b"250 ok\r\n")
                    elif line.startswith("QUIT"):
                        self.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        self.wfile.write(b"250 ok\r\n")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ----------------------------------------------------------------------------
# App server + HTTP helpers
# ----------------------------------------------------------------------------
class AppServer:
    def __init__(self, tmp_dir, **env_overrides):
        self.tmp_dir = tmp_dir
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = base_env(tmp_dir, PORT=self.port, BASE_URL=self.base, **env_overrides)
        self.db_path = env["DB_PATH"]
        self.backup_dir = env["BACKUP_DIR"]
        self.log = open(Path(tmp_dir) / "server.log", "w")
        self.process = subprocess.Popen([sys.executable, "-u", str(APP)], cwd=APP_DIR, env=env,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        if not wait_for(self._accepting, timeout=15):
            self.stop()
            raise RuntimeError("LaunchKit did not start:\n" + (Path(tmp_dir) / "server.log").read_text())

    def _accepting(self):
        with socket.socket() as probe:
            return probe.connect_ex(("127.0.0.1", self.port)) == 0

    def request(self, method, path, form=None, json_body=None, headers=None):
        headers = dict(headers or {})
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form, doseq=True)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if json_body is not None:
            body = json.dumps(json_body)
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        text = response.read().decode("utf-8", "replace")
        connection.close()
        return response, text

    def join(self, email, ip, ref="", post=""):
        _, text = self.request("POST", "/api/join", json_body={"email": email, "ref": ref, "post": post},
                               headers={"X-Forwarded-For": ip})
        return json.loads(text)

    def login(self, ip="9.9.9.9"):
        response, _ = self.request("POST", "/admin/login", form={"token": ADMIN_TOKEN}, headers={"X-Forwarded-For": ip})
        cookie = response.getheader("Set-Cookie") or ""
        return cookie.split(";")[0]  # "lk_admin=<session>"

    def admin(self, method, path, cookie, form=None):
        return self.request(method, path, form=form, headers={"Cookie": cookie, "X-Forwarded-For": "9.9.9.9"})

    def db(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return closing(connection)

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=10)
        self.log.close()


def path_of(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


# ----------------------------------------------------------------------------
# Main end-to-end suite (behind a trusted proxy, confirmation on, Resend + AI faked)
# ----------------------------------------------------------------------------
class LaunchKitEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.apis = FakeApis()
        cls.app = AppServer(
            cls.tmp.name,
            TRUST_PROXY=1,
            REQUIRE_VERIFICATION=1,
            BACKUP_KEEP=3,
            RESEND_API_KEY="re_test",
            RESEND_API_URL=f"{cls.apis.url}/emails",
            AI_API_KEY="sk-test",
            AI_BASE_URL=f"{cls.apis.url}/v1",
        )
        cls.cookie = cls.app.login()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.apis.close()
        cls.tmp.cleanup()

    def confirm(self, email):
        mail = wait_for(lambda: self.apis.emails_to(email, "Confirm"))
        self.assertTrue(mail, f"no confirmation email for {email}")
        link = re.search(r"(http://\S+/verify/[\w-]+)", mail[-1]["text"]).group(1)
        self.app.request("GET", path_of(link))

    def status_page(self, joined):
        return self.app.request("GET", path_of(joined["status_url"]))[1]

    # ---- waitlist + referrals --------------------------------------------
    def test_referral_counts_only_after_friend_confirms(self):
        alice = self.app.join("alice@example.com", "10.0.0.1")
        self.assertEqual(alice["status"], "pending_confirmation")
        self.confirm("alice@example.com")
        self.assertTrue(wait_for(lambda: self.apis.emails_to("alice@example.com", "You're #")))
        code = alice["referral_link"].split("ref=")[1]

        self.app.join("bob@example.com", "10.0.0.2", ref=code)
        self.assertIn("0 friends referred", self.status_page(alice))
        self.confirm("bob@example.com")
        self.assertTrue(wait_for(lambda: "1 friend referred" in self.status_page(alice)))
        self.assertTrue(wait_for(lambda: self.apis.emails_to("alice@example.com", "You unlocked")))
        self.assertEqual(self.apis.emails_to("alice@example.com")[0]["auth"], "Bearer re_test")

    def test_same_network_referral_is_not_credited(self):
        carol = self.app.join("carol@example.com", "10.0.1.1")
        self.confirm("carol@example.com")
        code = carol["referral_link"].split("ref=")[1]
        self.app.join("carol-sock@example.com", "10.0.1.1", ref=code)
        self.confirm("carol-sock@example.com")
        time.sleep(0.5)
        self.assertIn("0 friends referred", self.status_page(carol))

    def test_existing_email_never_reveals_status_page(self):
        first = self.app.join("dave@example.com", "10.0.2.1")
        again = self.app.join("Dave@Example.com", "10.0.2.2")
        self.assertEqual(again["status"], "existing")
        self.assertNotIn("status_url", again)
        self.assertNotIn(first["referral_link"].split("ref=")[1], first["status_url"])

    def test_unsubscribe_needs_post(self):
        self.app.join("erin@example.com", "10.0.3.1")
        self.confirm("erin@example.com")
        welcome = wait_for(lambda: self.apis.emails_to("erin@example.com", "You're #"))
        self.assertIn("List-Unsubscribe", welcome[0]["headers"])
        link = path_of(re.search(r"(http://\S+/unsubscribe/[\w-]+)", welcome[0]["text"]).group(1))
        self.assertIn("Unsubscribe?", self.app.request("GET", link)[1])
        self.assertIn("Unsubscribed", self.app.request("POST", link, form={"List-Unsubscribe": "One-Click"})[1])

    def test_signup_rate_limit_per_ip(self):
        results = [self.app.join(f"spam{i}@example.com", "10.0.4.1") for i in range(6)]
        self.assertIn("error", results[-1])

    # ---- admin security ----------------------------------------------------
    def test_admin_cookie_is_a_session_not_the_password(self):
        cookie = self.app.login(ip="9.9.9.10")
        self.assertTrue(cookie.startswith("lk_admin="))
        self.assertNotIn(ADMIN_TOKEN, cookie)
        response, _ = self.app.admin("GET", "/admin", cookie)
        self.assertIn("pre-launch dashboard", _)
        self.app.request("GET", "/admin/logout", headers={"Cookie": cookie})
        _, page = self.app.admin("GET", "/admin", cookie)
        self.assertIn("Admin token", page, "session should be invalid after logout")

    def test_login_locks_out_after_five_failures(self):
        ip = {"X-Forwarded-For": "66.66.66.66"}
        for _ in range(5):
            response, _ = self.app.request("POST", "/admin/login", form={"token": "wrong"}, headers=ip)
            self.assertEqual(response.status, 401)
        response, _ = self.app.request("POST", "/admin/login", form={"token": ADMIN_TOKEN}, headers=ip)
        self.assertEqual(response.status, 429)

    def test_admin_pages_require_login(self):
        for path in ("/admin/marketing", "/admin/export.csv", "/admin/backup.db"):
            response, _ = self.app.request("GET", path)
            self.assertEqual(response.status, 303, path)

    # ---- AI + broadcast ----------------------------------------------------
    def test_ai_compose_and_repurpose(self):
        self.app.admin("POST", "/admin/compose", self.cookie,
                       form={"platform": "x", "template": "progress", "body": "shipped dark mode", "use_ai": "on"})
        self.assertIn("shipped dark mode", self.apis.ai_prompts[-1])
        with self.app.db() as db:
            post = db.execute("SELECT * FROM posts WHERE body LIKE 'AI wrote this%' ORDER BY id DESC").fetchone()
        self.assertIsNotNone(post)
        self.assertIn("{link}", post["body"], "link placeholder must be appended")

        self.app.admin("POST", "/admin/repurpose", self.cookie,
                       form={"source": "Changelog: Stripe billing", "platforms": ["x", "linkedin"]})
        def repurposed():
            with self.app.db() as db:
                return db.execute("SELECT COUNT(*) FROM posts WHERE body LIKE 'Repurposed%'").fetchone()[0] == 2
        self.assertTrue(wait_for(repurposed))

    def test_broadcast_skips_unconfirmed_and_unsubscribed(self):
        self.app.join("fan@example.com", "10.0.5.1")
        self.confirm("fan@example.com")
        self.app.join("ghost@example.com", "10.0.5.2")  # never confirms
        quitter = self.app.join("quitter@example.com", "10.0.5.3")
        self.confirm("quitter@example.com")
        unsubscribe_path = path_of(quitter["status_url"]).replace("/s/", "/unsubscribe/")
        self.app.request("POST", unsubscribe_path, form={"List-Unsubscribe": "One-Click"})

        self.app.admin("POST", "/admin/broadcast", self.cookie,
                       form={"subject": "Big news {product}", "body": "Hi {name}, you're #{position}"})
        self.assertTrue(wait_for(lambda: self.apis.emails_to("fan@example.com", "Big news")))
        time.sleep(0.5)
        self.assertFalse(self.apis.emails_to("ghost@example.com", "Big news"))
        self.assertFalse(self.apis.emails_to("quitter@example.com", "Big news"))

    # ---- marketing ----------------------------------------------------------
    def test_launch_campaign_tracking_and_attribution(self):
        launch_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        self.app.admin("POST", "/admin/settings", self.cookie, form={"launch_date": launch_date, "product": "ShipFast"})
        self.app.admin("POST", "/admin/launch", self.cookie, form={"platforms": ["x", "linkedin"], "time": "09:00"})
        with self.app.db() as db:
            campaign_id = db.execute("SELECT id FROM campaigns WHERE name LIKE 'Launch week%'").fetchone()[0]
            posts = db.execute("SELECT * FROM posts WHERE campaign_id = ?", (campaign_id,)).fetchall()
            tasks = db.execute("SELECT * FROM tasks WHERE campaign_id = ?", (campaign_id,)).fetchall()
        self.assertEqual(len(posts), 10, "launch-7 is in the past, so 5 slots x 2 platforms")
        self.assertEqual(len(tasks), 7)
        code = next(p["link_code"] for p in posts if p["platform"] == "x" and "is live" in p["body"])

        def click(user_agent, ip):
            response, _ = self.app.request("GET", f"/go/{code}", headers={"User-Agent": user_agent, "X-Forwarded-For": ip})
            return response

        first = click(BROWSER_UA, "20.0.0.1")
        self.assertTrue(first.getheader("Location").startswith("/?utm_source=x&utm_medium=social"))
        self.assertTrue(first.getheader("Set-Cookie").startswith(f"lk_post={code}"))
        click(BROWSER_UA, "20.0.0.2")
        click(BROWSER_UA, "20.0.0.2")  # same visitor again
        click("Twitterbot/1.0", "20.0.0.3")  # link preview bot

        self.app.request("POST", "/join", form={"email": "clicker1@example.com", "post": code},
                         headers={"X-Forwarded-For": "20.0.0.1"})
        self.app.request("POST", "/join", form={"email": "clicker2@example.com"},
                         headers={"X-Forwarded-For": "20.0.0.2", "Cookie": f"lk_post={code}"})
        self.confirm("clicker1@example.com")
        self.confirm("clicker2@example.com")

        _, marketing = self.app.admin("GET", "/admin/marketing", self.cookie)
        row = re.search(r"<tr><td>Launch week[^<]*</td><td>(\d+)</td><td>(\d+)</td><td>(\d+)</td><td>([^<]+)</td>", marketing)
        self.assertEqual(row.groups(), ("10", "2", "2", "100%"))
        self.assertIn("Schedule your Product Hunt launch", marketing)

        self.app.admin("POST", "/admin/settings", self.cookie, form={"post_link_target": "https://app.shipfast.dev/"})
        self.assertTrue(click(BROWSER_UA, "20.0.0.9").getheader("Location").startswith("https://app.shipfast.dev/?utm_source=x"))

    # ---- backups + privacy ----------------------------------------------------
    def test_backup_download_and_retention(self):
        self.app.join("backed-up@example.com", "10.0.6.1")
        for _ in range(4):
            response, _ = self.app.admin("GET", "/admin/backup.db", self.cookie)
            self.assertEqual(response.status, 200)
        backups = [name for name in os.listdir(self.app.backup_dir) if name.endswith(".db")]
        self.assertEqual(len(backups), 3, "BACKUP_KEEP=3 keeps the newest three")
        with closing(sqlite3.connect(Path(self.app.backup_dir) / sorted(backups)[-1])) as backup:
            emails = [row[0] for row in backup.execute("SELECT email FROM subscribers")]
        self.assertIn("backed-up@example.com", emails)

    def test_privacy_notice_and_consent_line(self):
        _, landing = self.app.request("GET", "/")
        self.assertIn("By joining you agree to receive emails", landing)
        self.assertIn('href="/privacy"', landing)
        response, privacy = self.app.request("GET", "/privacy")
        self.assertEqual(response.status, 200)
        self.assertIn("lk_post", privacy)


# ----------------------------------------------------------------------------
# Without TRUST_PROXY a forged X-Forwarded-For must not bypass the rate limit
# ----------------------------------------------------------------------------
class ForwardedHeaderNotTrusted(unittest.TestCase):
    def test_forged_forwarded_for_is_ignored(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            app = AppServer(tmp, TRUST_PROXY=0, REQUIRE_VERIFICATION=0)
            try:
                results = [app.join(f"user{i}@example.com", f"30.0.0.{i}") for i in range(6)]
                self.assertIn("error", results[-1], "6th signup from one real IP must be blocked")
            finally:
                app.stop()


# ----------------------------------------------------------------------------
# Configuration, startup and upgrade checks (module imported in a subprocess)
# ----------------------------------------------------------------------------
class ConfigurationAndUpgrade(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_timezone_setting(self):
        result = run_python(
            "import launchkit, datetime\n"
            "print(abs((launchkit.local_now() - datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)).total_seconds()) < 120)\n",
            base_env(self.tmp.name, TIMEZONE="UTC"),
        )
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("UTC")
            zoneinfo_available = True
        except Exception:  # noqa: BLE001
            zoneinfo_available = False
        if zoneinfo_available:
            self.assertEqual(result.stdout.strip(), "True", result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tzdata", result.stderr)

    def test_invalid_timezone_stops_with_clear_message(self):
        result = run_python("import launchkit", base_env(self.tmp.name, TIMEZONE="Not/AZone"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not be loaded", result.stderr)

    def test_weak_admin_token_refused_when_listening_publicly(self):
        env = base_env(self.tmp.name, HOST="0.0.0.0", ADMIN_TOKEN="change-me", PORT=free_port(), BACKUP_KEEP=0)
        result = subprocess.run([sys.executable, str(APP)], cwd=APP_DIR, env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing", result.stderr + result.stdout)

    def test_upgrade_from_first_version_database(self):
        db_path = Path(self.tmp.name) / "app.db"
        with closing(sqlite3.connect(db_path)) as legacy:
            legacy.executescript(
                """
                CREATE TABLE subscribers (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT,
                    code TEXT UNIQUE NOT NULL, referred_by TEXT, referrals INTEGER NOT NULL DEFAULT 0, source TEXT,
                    ip_hash TEXT, created_at TEXT NOT NULL);
                CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, body TEXT NOT NULL,
                    scheduled_at TEXT, status TEXT NOT NULL DEFAULT 'draft', result TEXT, created_at TEXT NOT NULL);
                INSERT INTO subscribers(email, code, referrals, created_at) VALUES ('old@example.com', 'abc', 4, '2026-10-01');
                INSERT INTO posts(platform, body, created_at) VALUES ('x', 'old post {link}', '2026-10-01');
                """
            )
        result = run_python(
            "import launchkit as lk\n"
            "lk.init_db()\n"
            "s = lk.get_subscriber('email', 'old@example.com')\n"
            "p = lk.query('SELECT * FROM posts', one=True)\n"
            "print(s['verified'], s['referral_credited'], s['rewards_sent'], bool(s['private_token']), bool(p['link_code']))\n",
            base_env(self.tmp.name),
        )
        self.assertEqual(result.stdout.strip(), "1 1 3 True True", result.stderr)

    def test_smtp_delivery(self):
        smtp = FakeSmtp()
        try:
            result = run_python(
                "import launchkit as lk\n"
                "lk.init_db()\n"
                "print(lk.send_email_now('x@example.com', 'Hello', '<b>hi</b>', 'hi', 'http://u/unsub'))\n",
                base_env(self.tmp.name, SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none",
                         EMAIL_FROM="me@example.com"),
            )
            self.assertEqual(result.stdout.strip(), "sent", result.stderr)
            self.assertIn("multipart/alternative", smtp.messages[0])
            self.assertIn("List-Unsubscribe: <http://u/unsub>", smtp.messages[0])
        finally:
            smtp.close()


# ----------------------------------------------------------------------------
# Desktop launcher (app.py): first run creates .env and the login link works
# ----------------------------------------------------------------------------
class DesktopLauncher(unittest.TestCase):
    def test_first_run_creates_env_and_logs_in(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            env_file = Path(tmp) / "first-run.env"
            port = free_port()
            env = base_env(tmp, PORT=port, LAUNCHKIT_ENV=env_file, BACKUP_KEEP=0)
            env.pop("ADMIN_TOKEN")  # must come from the generated .env
            process = subprocess.Popen([sys.executable, "-u", str(APP_DIR / "app.py"), "--no-window"], cwd=tmp,
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            lines = []
            reader = threading.Thread(target=lambda: lines.extend(iter(process.stdout.readline, "")), daemon=True)
            reader.start()
            try:
                login_line = wait_for(lambda: next((l for l in lines if l.startswith("LOGIN_URL ")), None), timeout=15)
                self.assertIsNotNone(login_line, "".join(lines))
                generated = env_file.read_text(encoding="utf-8")
                token = re.search(r"^ADMIN_TOKEN=(.+)$", generated, re.M).group(1)
                self.assertGreaterEqual(len(token), 20)
                self.assertNotIn("pick-a-long-random-secret", generated)

                login_path = path_of(login_line.split(" ", 1)[1].strip())
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                connection.request("GET", login_path)
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 303)
                cookie = response.getheader("Set-Cookie").split(";")[0]
                connection.request("GET", "/admin", headers={"Cookie": cookie})
                page = connection.getresponse().read().decode("utf-8")
                connection.close()
                self.assertIn("pre-launch dashboard", page)
                self.assertIn("AI writer: off", page, "a copied .env.example must not switch AI on without a key")

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                connection.request("GET", "/admin/enter?key=forged")
                forged = connection.getresponse()
                forged.read()
                connection.close()
                self.assertIsNone(forged.getheader("Set-Cookie"), "an invalid key must not log in")
            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
