"""
Dashboard authentication -- users, password hashing, and sessions.

Deliberately stdlib-only (hashlib/secrets/sqlite3): no new dependencies,
no external identity provider, matching the single-server Windows
deployment this project targets. Passwords are stored as PBKDF2-HMAC-SHA256
hashes with a per-user random salt -- never in plain text, never
recoverable.

Kept in its own SQLite file (logs/dashboard_auth.db) rather than sharing
review_queue.db, so wiping/rebuilding the review queue during pipeline
work can never take the user accounts with it.

First-run bootstrap: an initial admin is seeded from DASHBOARD_ADMIN_USER
/ DASHBOARD_ADMIN_PASSWORD in .env. If those aren't set, a random
password is generated and printed to the server console ONCE at startup
-- so there is never a hardcoded default credential to forget about.
"""
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "dashboard_auth.db")

PBKDF2_ITERATIONS = 240_000
SESSION_COOKIE = "acd_session"
# Sliding expiry: every authenticated request pushes the expiry back out,
# so an active reviewer is never logged out mid-review, while an abandoned
# session still goes stale on its own.
SESSION_LIFETIME = timedelta(hours=12)

ROLE_ADMIN = "admin"
ROLE_REVIEWER = "reviewer"
ROLES = (ROLE_ADMIN, ROLE_REVIEWER)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'reviewer',
    display_name TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT,
    expires_at TEXT
);
"""


@contextmanager
def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(SCHEMA)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    """Returns (password_hash_hex, salt_hex)."""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return derived.hex(), salt.hex()


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    candidate, _ = hash_password(password, salt_hex)
    # compare_digest, not ==, so a wrong password can't be narrowed down
    # by timing how long the comparison took.
    return secrets.compare_digest(candidate, password_hash)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username: str, password: str, role: str = ROLE_REVIEWER,
                display_name: str = "") -> dict:
    username = username.strip().lower()
    if not username:
        raise ValueError("Username is required.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if role not in ROLES:
        raise ValueError(f"Role must be one of {ROLES}.")

    init_db()
    password_hash, salt = hash_password(password)
    with _connect() as conn:
        existing = conn.execute("SELECT username FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            raise ValueError(f"User '{username}' already exists.")
        conn.execute(
            """INSERT INTO users (username, password_hash, salt, role, display_name, active, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (username, password_hash, salt, role, display_name or username, _now().isoformat()),
        )
    return get_user(username)


def get_user(username: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [dict(r) for r in rows]


def set_password(username: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    init_db()
    password_hash, salt = hash_password(new_password)
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
            (password_hash, salt, username.strip().lower()),
        )


def set_active(username: str, active: bool) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET active = ? WHERE username = ?",
            (1 if active else 0, username.strip().lower()),
        )
    if not active:
        revoke_all_sessions(username)


def set_role(username: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Role must be one of {ROLES}.")
    init_db()
    with _connect() as conn:
        conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username.strip().lower()))


def count_admins(active_only: bool = True) -> int:
    init_db()
    with _connect() as conn:
        q = "SELECT COUNT(*) AS n FROM users WHERE role = ?"
        params = [ROLE_ADMIN]
        if active_only:
            q += " AND active = 1"
        return conn.execute(q, params).fetchone()["n"]


def authenticate(username: str, password: str) -> dict | None:
    """Returns the user dict on success, None on any failure (unknown user,
    wrong password, or deactivated account -- deliberately indistinguishable
    to the caller so the login page can't be used to enumerate accounts)."""
    user = get_user(username)
    if not user or not user["active"]:
        # Still run a hash so a nonexistent user doesn't return
        # noticeably faster than a real one with a wrong password.
        hash_password(password)
        return None
    if not verify_password(password, user["password_hash"], user["salt"]):
        return None
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE username = ?",
            (_now().isoformat(), user["username"]),
        )
    return get_user(user["username"])


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(username: str) -> str:
    init_db()
    token = secrets.token_urlsafe(32)
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, username, now.isoformat(), (now + SESSION_LIFETIME).isoformat()),
        )
    return token


def get_session_user(token: str | None) -> dict | None:
    """Validates a session token and returns its user, sliding the expiry
    forward. Returns None for missing/expired/revoked tokens or users who
    have since been deactivated."""
    if not token:
        return None
    init_db()
    now = _now()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not row:
            return None
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return None
        if expires_at <= now:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token = ?",
            ((now + SESSION_LIFETIME).isoformat(), token),
        )
        username = row["username"]

    user = get_user(username)
    if not user or not user["active"]:
        revoke_session(token)
        return None
    return user


def revoke_session(token: str | None) -> None:
    if not token:
        return
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def revoke_all_sessions(username: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE username = ?", (username.strip().lower(),))


def purge_expired_sessions() -> int:
    init_db()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now().isoformat(),))
        return cur.rowcount


# ---------------------------------------------------------------------------
# First-run bootstrap
# ---------------------------------------------------------------------------

def ensure_bootstrap_admin() -> str | None:
    """Called once at app startup. If no users exist at all, seeds an admin
    from DASHBOARD_ADMIN_USER / DASHBOARD_ADMIN_PASSWORD, or generates a
    random password and returns it so the caller can print it to the
    console. Returns None when users already exist (normal restart)."""
    init_db()
    if list_users():
        return None

    username = os.environ.get("DASHBOARD_ADMIN_USER", "admin").strip().lower() or "admin"
    password = os.environ.get("DASHBOARD_ADMIN_PASSWORD", "")
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True

    create_user(username, password, role=ROLE_ADMIN, display_name=username)
    return (
        f"\n{'=' * 68}\n"
        f"  FIRST RUN -- an admin account was created for this dashboard.\n"
        f"    username: {username}\n"
        f"    password: {password}\n"
        + ("  This password was randomly generated and is shown ONCE.\n"
           "  Log in and change it, or set DASHBOARD_ADMIN_PASSWORD in .env.\n"
           if generated else
           "  (from DASHBOARD_ADMIN_PASSWORD in .env)\n")
        + f"{'=' * 68}\n"
    )
