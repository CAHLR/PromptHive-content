#!/usr/bin/env python3
"""Start backend + frontend and open the PromptHive UI for manual difficulty-filter testing.

Usage:
    python3 run_difficulty_ui.py

Press Ctrl+C to stop both servers.
"""

from __future__ import annotations

# PromptHive/email.py shadows stdlib "email" when Python adds this repo to sys.path.
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0]).resolve() == _root:
    sys.path.pop(0)

import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = _root
BACKEND_DIR = ROOT / "abscribe_backend"
FRONTEND_DIR = ROOT / "frontend"

BACKEND_PORT = 8080
FRONTEND_PORT = 5173
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
# Vite binds to localhost (IPv6); use localhost for frontend checks and browser URL.
FRONTEND_URL = f"http://localhost:{FRONTEND_PORT}/#/"
FRONTEND_HEALTH_URL = f"http://localhost:{FRONTEND_PORT}/"

ENV_LOCAL = FRONTEND_DIR / ".env.local"
PROCS: list[subprocess.Popen] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def load_backend_env() -> None:
    for envfile in (BACKEND_DIR / ".env", BACKEND_DIR / "abscribe_backend" / ".env"):
        if not envfile.is_file():
            continue
        log(f"Loading {envfile}")
        for line in envfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            os.environ.setdefault(key, value)


def kill_port(port: int) -> None:
    if not shutil.which("lsof"):
        return
    try:
        out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True)
    except subprocess.CalledProcessError:
        return
    for pid in out.split():
        log(f"Stopping stale process on port {port} (PID {pid})")
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(0.5)


def wait_for_url(url: str, *, timeout: float = 90.0, label: str = "") -> bool:
    """Wait until a server accepts HTTP connections (any status code counts as up)."""
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            with urlopen(url, timeout=2) as resp:
                log(f"{label or url} ready (attempt {attempt}, HTTP {resp.status})")
                return True
        except HTTPError as exc:
            # Flask is up even if the route errors (e.g. MongoDB not configured).
            log(f"{label or url} ready (attempt {attempt}, HTTP {exc.code})")
            return True
        except (URLError, OSError):
            pass
        time.sleep(1)
    return False


def start_backend() -> subprocess.Popen:
    env = os.environ.copy()
    env["FLASK_APP"] = "abscribe_backend.app:app"
    env["FLASK_RUN_HOST"] = "0.0.0.0"
    env["FLASK_RUN_PORT"] = str(BACKEND_PORT)
    env["PYTHONPATH"] = str(BACKEND_DIR) + os.pathsep + env.get("PYTHONPATH", "")

    if shutil.which("poetry") and (BACKEND_DIR / "pyproject.toml").is_file():
        cmd = ["poetry", "run", "flask", "run"]
    else:
        cmd = [sys.executable, "-m", "flask", "run"]

    log_file = ROOT / ".run_difficulty_ui_backend.log"
    log(f"Starting backend: {' '.join(cmd)} (port {BACKEND_PORT})")
    log(f"  log → {log_file}")
    return subprocess.Popen(
        cmd,
        cwd=BACKEND_DIR,
        env=env,
        stdout=log_file.open("w"),
        stderr=subprocess.STDOUT,
    )


def write_frontend_env_local() -> None:
    content = (
        f"VITE_REACT_APP_API_BASE_URL={BACKEND_URL}\n"
        f"VITE_REACT_APP_API_PROXY_URL={BACKEND_URL}\n"
    )
    ENV_LOCAL.write_text(content, encoding="utf-8")
    log(f"Wrote {ENV_LOCAL} → API calls go to {BACKEND_URL}")


def ensure_frontend_deps() -> None:
    vite_bin = FRONTEND_DIR / "node_modules" / ".bin" / "vite"
    if vite_bin.is_file():
        return
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found — install Node.js to run the frontend.")
    log("Frontend dependencies missing — running npm install (first time only, may take a few minutes)...")
    subprocess.run([npm, "install"], cwd=FRONTEND_DIR, check=True)
    if not vite_bin.is_file():
        raise RuntimeError("npm install finished but vite was not found in node_modules/.bin/")


def start_frontend() -> subprocess.Popen:
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found — install Node.js to run the frontend.")

    log_file = ROOT / ".run_difficulty_ui_frontend.log"
    log("Starting frontend: npm start")
    log(f"  log → {log_file}")
    return subprocess.Popen(
        [npm, "start"],
        cwd=FRONTEND_DIR,
        stdout=log_file.open("w"),
        stderr=subprocess.STDOUT,
    )


def cleanup() -> None:
    for proc in PROCS:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    kill_port(BACKEND_PORT)
    kill_port(FRONTEND_PORT)


def on_signal(signum, _frame) -> None:
    log(f"\nReceived signal {signum}, shutting down...")
    cleanup()
    sys.exit(0)


def main() -> int:
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    log("=" * 70)
    log("PromptHive difficulty-filter UI launcher")
    log("=" * 70)

    if not BACKEND_DIR.is_dir() or not FRONTEND_DIR.is_dir():
        log("ERROR: expected abscribe_backend/ and frontend/ next to this script.")
        return 1

    load_backend_env()
    if not os.environ.get("OPENAI_API_KEY"):
        log("")
        log("WARNING: OPENAI_API_KEY is not set.")
        log(f"  Add it to {BACKEND_DIR}/.env before generating questions.")
        log("")

    kill_port(BACKEND_PORT)
    kill_port(FRONTEND_PORT)

    backend = start_backend()
    PROCS.append(backend)

    if not wait_for_url(f"{BACKEND_URL}/recipes/", label="Backend"):
        log("ERROR: backend did not start. Tail of log:")
        log_file = ROOT / ".run_difficulty_ui_backend.log"
        if log_file.is_file():
            log(log_file.read_text(encoding="utf-8")[-2000:])
        cleanup()
        return 1

    write_frontend_env_local()
    ensure_frontend_deps()
    frontend = start_frontend()
    PROCS.append(frontend)

    frontend_ready = False
    deadline = time.time() + 120.0
    while time.time() < deadline:
        if frontend.poll() is not None:
            log("ERROR: frontend exited early. Log tail:")
            log_file = ROOT / ".run_difficulty_ui_frontend.log"
            if log_file.is_file():
                log(log_file.read_text(encoding="utf-8")[-2000:])
            cleanup()
            return 1
        if wait_for_url(FRONTEND_HEALTH_URL, timeout=2.0, label="Frontend"):
            frontend_ready = True
            break
        time.sleep(2)

    if not frontend_ready:
        log("ERROR: frontend did not start within timeout.")
        cleanup()
        return 1

    log("")
    log("Opening browser...")
    webbrowser.open(FRONTEND_URL)

    log("")
    log("=" * 70)
    log("HOW TO TEST THE DIFFICULTY FILTER")
    log("=" * 70)
    log(f"  UI:      {FRONTEND_URL}")
    log(f"  Backend: {BACKEND_URL}")
    log("")
    log("  1. In the app, open the GENERATE QUESTIONS dropdown (PromptEditor panel).")
    log("  2. Click 'Difficulty filter: …' to set the slider (Easy / Medium / Hard).")
    log("  3. Click Generate.")
    log("  4. Each step row should show a difficulty chip (label + logit).")
    log("  5. Steps outside your range should be filtered out.")
    log("")
    log("Press Ctrl+C here to stop both servers.")
    log("=" * 70)

    try:
        while True:
            for proc in PROCS:
                if proc.poll() is not None:
                    log(f"Process exited with code {proc.returncode}")
                    cleanup()
                    return proc.returncode or 1
            time.sleep(1)
    except KeyboardInterrupt:
        on_signal(signal.SIGINT, None)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
