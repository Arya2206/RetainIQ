import os
import secrets
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")


def _load_secret_key():
    """Use SECRET_KEY from the environment; otherwise generate one once and
    persist it in instance/secret.key so sessions survive restarts and no
    secret is ever hard-coded in source control."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    path = os.path.join(INSTANCE_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip()


def _database_uri():
    # Set DATABASE_URL=mysql+pymysql://user:pass@host/dbname to run on MySQL
    # (the schema is plain SQLAlchemy, so no code changes are needed).
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    path = os.environ.get("RETAINIQ_DB") or os.path.join(INSTANCE_DIR, "retainiq.db")
    return "sqlite:///" + path


class Config:
    APP_NAME = "RetainIQ"
    APP_VERSION = "2.0"
    SECRET_KEY = _load_secret_key()
    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024      # 10 MB upload cap
    MAX_UPLOAD_ROWS = 5000

    # Session / cookie hardening
    PERMANENT_SESSION_LIFETIME = timedelta(hours=2)   # idle timeout
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"

    # Brute-force protection
    MAX_FAILED_LOGINS = 5
    LOCKOUT_MINUTES = 15

    # Anyone may self-register as Employee; every other role needs this code.
    STAFF_ACCESS_CODE = os.environ.get("STAFF_ACCESS_CODE", "RETAINIQ2026")


if not os.environ.get("STAFF_ACCESS_CODE"):
    print("  [RetainIQ] WARNING: using the default demo STAFF_ACCESS_CODE. "
          "Set the STAFF_ACCESS_CODE environment variable before any real deployment.")
