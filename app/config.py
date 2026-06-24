import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'dagonweb.sqlite'}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SCRATCH_DIR = os.getenv("SCRATCH_DIR", str(BASE_DIR / "scratch"))
    DAGON_INI_PATH = os.getenv("DAGON_INI_PATH", str(BASE_DIR / "instance" / "dagon.ini"))
    WTF_CSRF_TIME_LIMIT = None

class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
