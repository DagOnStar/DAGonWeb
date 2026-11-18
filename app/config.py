import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'dagonweb.sqlite'}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SCRATCH_DIR = os.getenv("SCRATCH_DIR", str(BASE_DIR / "scratch"))
    DAGON_INI_PATH = os.getenv("DAGON_INI_PATH", str(BASE_DIR / "instance" / "dagon.ini"))
    WTF_CSRF_TIME_LIMIT = None
    # Cookies: first-party, HttpOnly, SameSite=Lax; Secure behind HTTPS.
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    # Signed-in users must accept the current privacy notice version.
    PRIVACY_ACCEPTANCE_REQUIRED = True

class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    # Most tests sign in as seeded users who never saw the notice;
    # tests/test_compliance.py turns the check back on.
    PRIVACY_ACCEPTANCE_REQUIRED = False
