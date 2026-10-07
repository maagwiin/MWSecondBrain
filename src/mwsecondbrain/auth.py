"""Single-user Argon2id authentication with revocable opaque sessions."""

import hashlib
import secrets
import time

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from .db import Database

COOKIE_NAME = "mwsb_session"
IDLE_SECONDS = 12 * 3600
ABSOLUTE_SECONDS = 7 * 24 * 3600
THROTTLE_SECONDS = 15 * 60
MAX_THROTTLE_RECORDS = 4096


class InvalidCredentials(Exception):
    pass


class Throttled(Exception):
    pass


class Auth:
    def __init__(self, database: Database, clock=time.time):
        self.database = database
        self.clock = clock
        self.hasher = PasswordHasher(type=Type.ID, time_cost=3, memory_cost=65536, parallelism=4)
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    def set_password(self, password: str, *, require_uninitialized=False):
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise ValueError("Password must contain between 12 and 1024 characters")
        password_hash = self.hasher.hash(password)
        with self.database.connect(immediate=True) as connection:
            if require_uninitialized and connection.execute("SELECT 1 FROM metadata WHERE key='password_hash'").fetchone():
                raise ValueError("Password already initialized; use reset-password")
            connection.execute("INSERT INTO metadata(key,value) VALUES ('password_hash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (password_hash,))
            connection.execute("DELETE FROM sessions")
            connection.execute("DELETE FROM login_attempts")

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def login(self, password: str, ip: str):
        now = self.clock()
        valid = False
        limited = False
        session = None
        # Serialize verification with counting so parallel requests cannot exceed the limit.
        with self.database.connect(immediate=True) as connection:
            connection.execute("DELETE FROM login_attempts WHERE started<=?", (now - THROTTLE_SECONDS,))
            connection.execute("DELETE FROM sessions WHERE created<=? OR touched<=?", (now - ABSOLUTE_SECONDS, now - IDLE_SECONDS))
            record = connection.execute("SELECT failures FROM login_attempts WHERE ip=?", (ip,)).fetchone()
            if record and record[0] >= 5:
                limited = True
            else:
                row = connection.execute("SELECT value FROM metadata WHERE key='password_hash'").fetchone()
                try:
                    verified = self.hasher.verify(row[0] if row else self.dummy_hash, password)
                    valid = bool(row and verified)
                except (InvalidHashError, VerificationError):
                    valid = False
                if valid:
                    connection.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
                    token = secrets.token_urlsafe(32)
                    csrf = secrets.token_urlsafe(32)
                    connection.execute("INSERT INTO sessions VALUES (?,?,?,?)", (self.token_hash(token), csrf, now, now))
                    # Bound session storage even if an attacker knows the password.
                    connection.execute("DELETE FROM sessions WHERE token_hash NOT IN (SELECT token_hash FROM sessions ORDER BY created DESC LIMIT 128)")
                    session = (token, csrf)
                else:
                    connection.execute("INSERT INTO login_attempts VALUES (?,1,?) ON CONFLICT(ip) DO UPDATE SET failures=failures+1", (ip, now))
                    connection.execute("DELETE FROM login_attempts WHERE ip NOT IN (SELECT ip FROM login_attempts ORDER BY started DESC LIMIT ?)", (MAX_THROTTLE_RECORDS,))
        if limited:
            raise Throttled()
        if not valid:
            raise InvalidCredentials()
        return session

    def session(self, token: str | None):
        if not token or len(token) > 128:
            return None
        now = self.clock()
        with self.database.connect(immediate=True) as connection:
            token_hash = self.token_hash(token)
            row = connection.execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if not row:
                return None
            if now - row["created"] >= ABSOLUTE_SECONDS or now - row["touched"] >= IDLE_SECONDS:
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
                return None
            connection.execute("UPDATE sessions SET touched=? WHERE token_hash=?", (now, token_hash))
            return dict(row)

    def logout(self, token: str):
        with self.database.connect(immediate=True) as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (self.token_hash(token),))
