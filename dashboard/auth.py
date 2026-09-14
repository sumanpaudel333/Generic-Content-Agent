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
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "dashboard_auth.db")

PBKDF2_ITERATIONS = 240_000
SESSION_COOKIE = "acd_session"

# A reference to a one-shot message, and a pending password-reset link. Both
# are cookies rather than URL parameters so that nothing a person should not
# see in a browser history, a proxy log or a Referer header ever appears in an
# address. See put_flash() and the set-password routes.
FLASH_COOKIE = "acd_flash"
RESET_COOKIE = "acd_reset"
FLASH_LIFETIME = timedelta(minutes=10)
# Sliding expiry: every authenticated request pushes the expiry back out,
# so an active reviewer is never logged out mid-review, while an abandoned
# session still goes stale on its own.
SESSION_LIFETIME = timedelta(hours=12)

ROLE_ADMIN = "admin"
ROLE_REVIEWER = "reviewer"
ROLES = (ROLE_ADMIN, ROLE_REVIEWER)

# ---------------------------------------------------------------------------
# What each role may do
#
# A reviewer reviews: they read the queue, edit a description, approve it,
# turn it down, send it to the product, and work the lead list. That is the
# whole job, and it is per-item work on things already in front of them.
#
# What they no longer do is start a job or send mail. Both reach outside this
# dashboard and neither can be taken back: a batch run costs an hour of the
# machine's time and cannot be stopped from the browser, and an email is gone
# the moment it is accepted. Those also have scheduled counterparts, so a
# reviewer pressing one is usually duplicating work that already happened
# overnight rather than fixing anything.
#
# Checking for leads is the exception, and it is deliberate. It only re-reads
# chats already stored here and adds what it finds to a list on the same page,
# which is the reviewer's own work rather than a job. The one part of it that
# does reach outside -- the alert email for a lead nobody has seen yet --
# stays behind send_mail, so a reviewer's scan finds the leads and leaves the
# mail for somebody who is allowed to send it. Nothing is lost by that: a lead
# that has not been alerted stays un-alerted, so the overnight job or an admin
# still sends it.
#
# Kept as a table rather than scattered role checks so that adding a role, or
# moving one capability, is one edit in one place -- and so a test can assert
# the whole matrix rather than hunting for the routes that forgot.
# ---------------------------------------------------------------------------
PERM_REVIEW = "review"              # approve, turn down, edit, publish, lead status
PERM_FIND_LEADS = "find_leads"      # re-scan stored chats for contact details
PERM_RUN_JOBS = "run_jobs"          # fetch a batch, run the analysis, sync leads
PERM_SEND_MAIL = "send_mail"        # email a report, send the morning lead list
PERM_VIEW_MAIL_LOG = "view_mail_log"  # the record of which emails went out
PERM_ADMINISTER = "administer"      # users, logs, activity, photo storage

PERMISSIONS: dict[str, frozenset] = {
    ROLE_ADMIN: frozenset({PERM_REVIEW, PERM_FIND_LEADS, PERM_RUN_JOBS,
                            PERM_SEND_MAIL, PERM_VIEW_MAIL_LOG, PERM_ADMINISTER}),
    ROLE_REVIEWER: frozenset({PERM_REVIEW, PERM_FIND_LEADS}),
}


def can(user: dict | None, permission: str) -> bool:
    """Whether this user may do that.

    Denies by default: no user, an unknown role, or a permission nobody has
    been granted all come back False, so a typo in a role name locks a door
    rather than opening one.
    """
    if not user:
        return False
    return permission in PERMISSIONS.get(user.get("role") or "", frozenset())

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
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,   -- the HASH; the token itself is never stored
    username TEXT NOT NULL,
    kind TEXT NOT NULL,            -- invite | reset
    created_at TEXT,
    expires_at TEXT,
    used_at TEXT,
    issued_by TEXT                 -- the admin who sent it, or "" for self-service
);
CREATE INDEX IF NOT EXISTS idx_resets_user ON password_resets(username);
CREATE TABLE IF NOT EXISTS flashes (
    id TEXT PRIMARY KEY,           -- random; the only thing the browser holds
    kind TEXT NOT NULL,            -- ok | err
    text TEXT NOT NULL,
    created_at TEXT
);
"""

# Added after the table already existed in the field, so an installed
# dashboard_auth.db picks them up without being rebuilt -- it holds the only
# copy of the accounts.
_MIGRATION_COLUMNS = [
    ("users", "email", "TEXT"),
]


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
        for table, column, col_type in _MIGRATION_COLUMNS:
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


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
# Email addresses
#
# Optional, but an account without one cannot be sent a sign-in link -- which
# is the difference between an admin resetting somebody's password over the
# phone and the person doing it themselves.
# ---------------------------------------------------------------------------

# Deliberately loose. This is not trying to decide whether an address is
# deliverable -- only the mail server knows that, and every clever regex
# rejects somebody's perfectly valid address. It catches the typo that has no
# "@" and the one with a space in it.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_email(email: str) -> str:
    """Normalised address, or "" for blank. Raises on something misshapen."""
    email = (email or "").strip().lower()
    if not email:
        return ""
    if not _EMAIL_SHAPE.match(email):
        raise ValueError(f"'{email}' does not look like an email address.")
    return email


def user_by_email(email: str) -> dict | None:
    """The account with this address, if any. Used by the forgot-password
    flow, which is why it does not care about case."""
    email = (email or "").strip().lower()
    if not email:
        return None
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username: str, password: str, role: str = ROLE_REVIEWER,
                display_name: str = "", email: str = "") -> dict:
    """Creates an account.

    Pass password="" to create one nobody can sign into yet -- the account gets
    a random password it is never told, and the caller is expected to issue an
    invite link. That is the better way in: an admin typing a password has to
    then get it to the person somehow, and "over the phone" and "in a chat
    message" are both worse than a link only they can use.
    """
    username = username.strip().lower()
    if not username:
        raise ValueError("Username is required.")
    if password and len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if role not in ROLES:
        raise ValueError(f"Role must be one of {ROLES}.")
    email = clean_email(email)

    init_db()
    # No password means no way in until an invite link is used. The stored
    # hash is of a value nobody has ever seen, rather than something empty
    # that a bug could later compare equal to.
    password_hash, salt = hash_password(password or secrets.token_urlsafe(32))
    with _connect() as conn:
        existing = conn.execute("SELECT username FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            raise ValueError(f"User '{username}' already exists.")
        if email and conn.execute("SELECT 1 FROM users WHERE lower(email) = ?", (email,)).fetchone():
            raise ValueError(f"Another account already uses {email}.")
        conn.execute(
            """INSERT INTO users (username, password_hash, salt, role, display_name,
                                   email, active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (username, password_hash, salt, role, display_name or username, email,
             _now().isoformat()),
        )
    return get_user(username)


def update_user(username: str, *, display_name: str | None = None,
                 email: str | None = None, role: str | None = None) -> dict:
    """Edits the parts of an account that are safe to change in one go.

    Password and active status are deliberately not here: each has its own
    consequence (sessions dropped, somebody locked out) and its own
    confirmation, and folding them into a general "save" makes those easy to
    trigger without meaning to.
    """
    username = username.strip().lower()
    user = get_user(username)
    if not user:
        raise ValueError(f"No such user '{username}'.")

    fields, values = [], []
    if display_name is not None:
        fields.append("display_name = ?")
        values.append(display_name.strip() or username)
    if email is not None:
        email = clean_email(email)
        if email:
            with _connect() as conn:
                clash = conn.execute(
                    "SELECT username FROM users WHERE lower(email) = ? AND username != ?",
                    (email, username)).fetchone()
            if clash:
                raise ValueError(f"{email} is already on {clash['username']}'s account.")
        fields.append("email = ?")
        values.append(email)
    if role is not None:
        if role not in ROLES:
            raise ValueError(f"Role must be one of {ROLES}.")
        # The last admin cannot demote themselves out of existence: there
        # would be nobody left who could put it back.
        if user["role"] == ROLE_ADMIN and role != ROLE_ADMIN and count_admins() <= 1:
            raise ValueError("This is the only admin left. Make somebody else an "
                              "admin first.")
        fields.append("role = ?")
        values.append(role)

    if not fields:
        return user
    init_db()
    with _connect() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE username = ?",
                     values + [username])
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


def set_password(username: str, new_password: str, *, keep_sessions: bool = False) -> None:
    """Sets a new password and signs the account out everywhere.

    Signing out matters most in the case this was written for: somebody asks
    for a reset because they think another person has their password. Leaving
    that person's existing session alive would make the reset pointless.

    `keep_sessions` is for the one case where the person changing it IS the
    person signed in -- being logged out of the tab you just used to change
    your own password reads as the change having failed.
    """
    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    init_db()
    password_hash, salt = hash_password(new_password)
    username = username.strip().lower()
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
            (password_hash, salt, username),
        )
        # Any outstanding link is void now: the password it would have set is
        # already set, and a link left alive is a second way in.
        conn.execute("DELETE FROM password_resets WHERE username = ? AND used_at IS NULL",
                     (username,))
    if not keep_sessions:
        revoke_all_sessions(username)


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


# ---------------------------------------------------------------------------
# Sign-in links
#
# One mechanism for two jobs: inviting somebody who has never signed in, and
# letting somebody who has forgotten their password set a new one. They differ
# only in wording and in how long the link lasts.
#
# The token is generated once, handed to the caller once, and never stored --
# only its SHA-256 is kept. Anyone reading the database later, including a
# backup of it, finds hashes they cannot turn back into working links. Same
# reasoning as the password hashes two functions up.
# ---------------------------------------------------------------------------
KIND_INVITE = "invite"
KIND_RESET = "reset"

# An invite is for somebody who may not be at their desk today; a reset is for
# somebody who asked for it a minute ago and is waiting.
INVITE_LIFETIME = timedelta(days=7)
RESET_LIFETIME = timedelta(hours=2)


def _token_hash(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def create_reset_token(username: str, *, kind: str = KIND_RESET,
                        issued_by: str = "") -> str:
    """Issues a link token and returns it. This is the only time it exists.

    Any earlier unused token for the same person stops working, so a second
    "send a link" press cannot leave two valid ways in.
    """
    username = username.strip().lower()
    if not get_user(username):
        raise ValueError(f"No such user '{username}'.")
    if kind not in (KIND_INVITE, KIND_RESET):
        raise ValueError("kind must be invite or reset.")

    raw = secrets.token_urlsafe(32)
    lifetime = INVITE_LIFETIME if kind == KIND_INVITE else RESET_LIFETIME
    now = _now()
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM password_resets WHERE username = ? AND used_at IS NULL",
                     (username,))
        conn.execute(
            """INSERT INTO password_resets
                   (token_hash, username, kind, created_at, expires_at, issued_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (_token_hash(raw), username, kind, now.isoformat(),
             (now + lifetime).isoformat(), issued_by),
        )
    return raw


def check_reset_token(raw: str) -> dict | None:
    """The pending link, or None if it is unknown, used or expired.

    Read-only: the page that asks somebody to type a new password calls this,
    and a link should not be burnt by looking at it.
    """
    if not raw:
        return None
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM password_resets WHERE token_hash = ?", (_token_hash(raw),)
        ).fetchone()
    if not row or row["used_at"]:
        return None
    try:
        if datetime.fromisoformat(row["expires_at"]) < _now():
            return None
    except (TypeError, ValueError):
        return None
    return dict(row)


def consume_reset_token(raw: str, new_password: str) -> dict | None:
    """Sets the password and burns the link. None if the link was no good.

    Everything happens together on purpose: the token is marked used in the
    same breath as the password changing, so a link cannot be replayed, and a
    failed password rule cannot spend it.
    """
    pending = check_reset_token(raw)
    if not pending:
        return None
    set_password(pending["username"], new_password)   # raises if too short
    with _connect() as conn:
        conn.execute("UPDATE password_resets SET used_at = ? WHERE token_hash = ?",
                     (_now().isoformat(), _token_hash(raw)))
    return pending


def pending_invite(username: str) -> dict | None:
    """An unused, unexpired link for this person, if one is outstanding.

    Shown on the People page so an admin can see that somebody was invited and
    has not been in yet, rather than wondering whether the email went.
    """
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM password_resets WHERE username = ? AND used_at IS NULL "
            "ORDER BY created_at DESC", (username.strip().lower(),)).fetchall()
    now = _now()
    for row in rows:
        try:
            if datetime.fromisoformat(row["expires_at"]) > now:
                return dict(row)
        except (TypeError, ValueError):
            continue
    return None


def delete_user(username: str) -> dict:
    """Removes an account outright, with its sessions and any pending links.

    Disabling is almost always the better move and is one button along: it
    keeps the username reserved, so nobody can later be created with the same
    name and inherit the look of somebody else's history. Deleting is for an
    account created by mistake, or somebody who was never really here.

    What it does NOT remove is what they did. The activity log records the
    person's name as text in a different database, so their approvals and
    rejections stay readable afterwards -- which is the point of an activity
    log, and would be worth very little if leaving the company erased it.

    Returns the deleted row, so the caller can say what went.
    """
    username = username.strip().lower()
    user = get_user(username)
    if not user:
        raise ValueError(f"No such user '{username}'.")
    if user["role"] == ROLE_ADMIN and user["active"] and count_admins() <= 1:
        raise ValueError("This is the only admin left. Make somebody else an admin "
                          "before deleting this one.")
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
        conn.execute("DELETE FROM password_resets WHERE username = ?", (username,))
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
    return user


# ---------------------------------------------------------------------------
# Flash messages
#
# "Published", "Deleted jsmith", "Give them this link instead: ..." -- the
# one-line message shown after an action. These used to ride in the query
# string of the redirect, which put them everywhere a URL goes: the address
# bar, browser history, IIS's request log, and the Referer header of whatever
# the next page linked to. Some carried a name or an email address; one
# carried a live password-reset link. And an Odoo traceback in one of them
# took the page down outright, being longer than IIS accepts.
#
# Now the text is stored here and the browser gets a random id in a cookie.
# The id means nothing on its own, it is used once, and it is gone in minutes.
# That is stronger than encrypting the text into the URL: an encrypted value
# is still long, still logged, and still readable by anyone holding the key.
# ---------------------------------------------------------------------------

def put_flash(kind: str, text: str) -> str:
    """Stores a message and returns the id to hand the browser."""
    kind = "err" if kind == "err" else "ok"
    flash_id = secrets.token_urlsafe(18)
    now = _now()
    init_db()
    with _connect() as conn:
        # Anything nobody came back for, cleared on the way past.
        conn.execute("DELETE FROM flashes WHERE created_at < ?",
                     ((now - FLASH_LIFETIME).isoformat(),))
        conn.execute("INSERT INTO flashes (id, kind, text, created_at) VALUES (?, ?, ?, ?)",
                     (flash_id, kind, str(text or ""), now.isoformat()))
    return flash_id


def take_flash(flash_id: str | None) -> tuple[str, str] | None:
    """(kind, text) for this id, deleted as it is read. None if there is none.

    Deleted on read so a page that refreshes itself -- the review queue does,
    while a fetch is running -- shows the message once rather than on every
    reload, which is what the query-string version did.
    """
    if not flash_id:
        return None
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT kind, text, created_at FROM flashes WHERE id = ?",
                           (flash_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM flashes WHERE id = ?", (flash_id,))
    try:
        if datetime.fromisoformat(row["created_at"]) < _now() - FLASH_LIFETIME:
            return None
    except (TypeError, ValueError):
        return None
    return row["kind"], row["text"]


def count_admins(active_only: bool = True) -> int:
    init_db()
    with _connect() as conn:
        q = "SELECT COUNT(*) AS n FROM users WHERE role = ?"
        params = [ROLE_ADMIN]
        if active_only:
            q += " AND active = 1"
        return conn.execute(q, params).fetchone()["n"]


def resolve_login(identifier: str) -> dict | None:
    """The account for a username OR an email address.

    People remember one or the other, and being told "wrong username" when you
    typed a perfectly good email address is a poor way to spend somebody's
    afternoon. Username is tried first so it stays authoritative: if an
    account's username happens to look like somebody else's email address, the
    username wins, which is deterministic and cannot be arranged by a person
    editing their own profile (they cannot -- only an admin sets these).
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    return get_user(identifier) or user_by_email(identifier)


def authenticate(identifier: str, password: str) -> dict | None:
    """Returns the user dict on success, None on any failure (unknown account,
    wrong password, or deactivated account -- deliberately indistinguishable
    to the caller so the login page can't be used to enumerate accounts).

    `identifier` is a username or an email address; either signs you in.
    """
    user = resolve_login(identifier)
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
