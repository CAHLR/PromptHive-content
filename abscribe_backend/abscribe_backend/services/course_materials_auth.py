"""Provisioned instructor accounts for the standalone service; no public signup.

Existing initialized Flask-Security sessions are also accepted by the API. This
store supplies working signed-session authentication when the legacy app's
Mongo/Security integration is disabled.
"""
import argparse
import getpass
import json
import os
import time
from pathlib import Path

from flask import current_app, session
from werkzeug.security import check_password_hash, generate_password_hash

from .course_materials import MaterialError, MaterialStore, source_snapshot
from .course_materials_config import load_config


def ensure_accounts(store):
    with store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS instructors (id TEXT PRIMARY KEY, password_hash TEXT NOT NULL, courses TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)")
        db.execute("CREATE TABLE IF NOT EXISTS login_limits (ip TEXT PRIMARY KEY, failures INTEGER NOT NULL, blocked_until REAL NOT NULL)")


def account(store, user_id):
    ensure_accounts(store)
    with store.connect() as db:
        row = db.execute("SELECT id,courses,active FROM instructors WHERE id=?", (user_id,)).fetchone()
    return {"id": row["id"], "courses": json.loads(row["courses"]), "active": bool(row["active"])} if row else None


def session_account(store):
    user_id = session.get("course_materials_instructor")
    return account(store, user_id) if user_id else None


def authenticate(store, email, password, ip):
    ensure_accounts(store)
    with store.connect() as db:
        blocked = db.execute("SELECT * FROM login_limits WHERE ip=?", (ip,)).fetchone()
        if blocked and blocked["blocked_until"] > time.time():
            raise MaterialError("Too many sign-in attempts. Try again in 15 minutes.", 429)
        row = db.execute("SELECT * FROM instructors WHERE id=? AND active=1", (email.lower().strip(),)).fetchone()
        valid = row and check_password_hash(row["password_hash"], password)
        if not valid:
            failures = (blocked["failures"] if blocked and blocked["blocked_until"] > time.time() - 900 else 0) + 1
            db.execute("INSERT OR REPLACE INTO login_limits VALUES (?,?,?)", (ip, failures, time.time() + 900 if failures >= 5 else time.time()))
        else:
            db.execute("DELETE FROM login_limits WHERE ip=?", (ip,))
    if not valid:
        raise MaterialError("Email or password is incorrect.", 401)
    session.clear()
    session["course_materials_instructor"] = row["id"]
    session.permanent = True
    return account(store, row["id"])


def approval_authorized(store, record):
    approval = record.get("approval", {})
    identity = approval.get("approvedBy")
    if identity == "local-instructor":
        return os.environ.get("COURSE_MATERIALS_LOCAL_DEV") == "1"
    user = account(store, identity)
    if user:
        return user["active"] and record["courseName"] in user["courses"]
    # Legacy Flask-Security identities must still be active instructors at worker
    # time. The worker needs an explicit Mongo connection for this integration.
    uri = os.environ.get("COURSE_MATERIALS_MONGO_URI")
    if not uri:
        return False
    import mongoengine
    from abscribe_backend.models.authentication_models.user import User
    mongoengine.connect(host=uri)
    actor = User.objects(fs_uniquifier=identity).first()
    scopes = json.loads(os.environ.get("COURSE_MATERIALS_INSTRUCTOR_COURSES", "{}"))
    return bool(actor and actor.active and (actor.has_role("admin") or actor.has_role("instructor") and record["courseName"] in scopes.get(identity, [])))


def main():
    load_config()
    parser = argparse.ArgumentParser(description="Provision/revoke a Course Materials instructor; passwords are never command-line arguments.")
    parser.add_argument("email")
    parser.add_argument("--course", action="append", default=[])
    parser.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    store = MaterialStore(os.environ.get("COURSE_MATERIALS_DATA_DIR") or Path(__file__).resolve().parents[2] / "instance/course-materials")
    ensure_accounts(store)
    if args.revoke:
        with store.connect() as db:
            db.execute("UPDATE instructors SET active=0 WHERE id=?", (args.email.lower().strip(),))
        print("Instructor access revoked.")
        return
    valid_courses = {c["name"] for c in source_snapshot()["courses"]}
    if not args.course or not set(args.course) <= valid_courses:
        raise SystemExit("Supply --course for each exact authoritative course name.")
    password = getpass.getpass("New instructor password (at least 12 characters): ")
    if len(password) < 12 or password != getpass.getpass("Confirm password: "):
        raise SystemExit("Password length/confirmation failed.")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO instructors VALUES (?,?,?,1)", (args.email.lower().strip(), generate_password_hash(password), json.dumps(args.course)))
    print("Instructor account saved with explicit course permissions.")


if __name__ == "__main__":
    main()
