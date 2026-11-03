"""
LaunchKit desktop app: double-click LaunchKit.bat (or run: python app.py).

Starts LaunchKit on this computer and opens it in its own window, already logged in.
The first run creates .env with a random admin password. Close the console window to stop.
If LaunchKit is already running, it just opens another window.

Options:  --no-window   print the login link instead of opening a window
"""

import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("LAUNCHKIT_ENV", HERE / ".env"))
ENV_EXAMPLE = HERE / ".env.example"
APP_BROWSERS = [
    shutil.which("msedge"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    shutil.which("chrome"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome"),
    shutil.which("chromium"),
]


def ensure_env_file():
    """First run: copy .env.example to .env with a random admin password and salt."""
    if ENV_FILE.exists():
        return False
    template = ENV_EXAMPLE.read_text(encoding="utf-8") if ENV_EXAMPLE.exists() else "ADMIN_TOKEN=\n"
    content = re.sub(r"^ADMIN_TOKEN=.*$", f"ADMIN_TOKEN={secrets.token_urlsafe(18)}", template, count=1, flags=re.M)
    content = re.sub(r"^SECRET_SALT=.*$", f"SECRET_SALT={secrets.token_urlsafe(18)}", content, count=1, flags=re.M)
    ENV_FILE.write_text(content, encoding="utf-8")
    return True


def port_in_use(port):
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def open_app_window(url):
    """Edge/Chrome '--app' mode gives a window without tabs or address bar; else the default browser."""
    for browser in APP_BROWSERS:
        if browser and Path(browser).exists():
            import subprocess

            subprocess.Popen([browser, f"--app={url}", "--window-size=1280,860"])
            return
    webbrowser.open(url)


def main():
    created = ensure_env_file()
    os.environ["LAUNCHKIT_ENV"] = str(ENV_FILE)
    sys.path.insert(0, str(HERE))
    import launchkit

    launchkit.init_db()
    port = launchkit.PORT
    already_running = port_in_use(port)
    if not already_running:
        launchkit.check_admin_token()
        launchkit.start_background_services()
        server = ThreadingHTTPServer((launchkit.HOST, port), launchkit.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    login_url = f"http://127.0.0.1:{port}/admin/enter?key={launchkit.create_admin_session()}"
    if created:
        print(f"First run: created {ENV_FILE} with a random admin password (ADMIN_TOKEN).")
    if "--no-window" in sys.argv:
        print(f"LOGIN_URL {login_url}", flush=True)
    else:
        open_app_window(login_url)
    if already_running:
        print("LaunchKit is already running - opened a new window.")
        return
    print(f"LaunchKit is running at http://127.0.0.1:{port}  (public page: /  admin: /admin)", flush=True)
    print("Close this window or press Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
