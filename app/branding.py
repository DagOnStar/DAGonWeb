"""Site branding: name, tagline, colors and an uploaded logo."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import current_app, url_for
from werkzeug.datastructures import FileStorage

from . import settings
from .extensions import db
from .models import Setting

LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".svg": "image/svg+xml"}
LOGO_SIGNATURES = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",), ".webp": (b"RIFF",)}
MAX_LOGO_BYTES = 512 * 1024


def branding_dir() -> Path:
    path = Path(current_app.instance_path) / "branding"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logo_path() -> Path | None:
    stored = Setting.query.filter_by(key="logo_file").first()
    if not stored or not stored.value:
        return None
    path = (branding_dir() / stored.value).resolve()
    return path if path.parent == branding_dir().resolve() and path.is_file() else None


def save_logo(uploaded: FileStorage) -> None:
    suffix = Path(uploaded.filename or "").suffix.lower()
    if suffix not in LOGO_TYPES:
        raise ValueError(f"Use one of: {', '.join(sorted(LOGO_TYPES))}.")
    content = uploaded.read(MAX_LOGO_BYTES + 1)
    if len(content) > MAX_LOGO_BYTES:
        raise ValueError("The logo must be smaller than 512 KB.")
    if suffix == ".svg":
        head = content[:2048].lower()
        if b"<svg" not in head or b"<script" in content.lower():
            raise ValueError("The SVG logo is not valid or contains scripts.")
    elif not any(content.startswith(signature) for signature in LOGO_SIGNATURES[suffix]):
        raise ValueError("The file content does not match its extension.")
    remove_logo(commit=False)
    name = f"logo{suffix}"
    (branding_dir() / name).write_bytes(content)
    stored = Setting.query.filter_by(key="logo_file").first()
    if stored:
        stored.value = name
    else:
        db.session.add(Setting(key="logo_file", value=name))
    db.session.commit()


def remove_logo(commit: bool = True) -> None:
    path = logo_path()
    if path:
        path.unlink(missing_ok=True)
    stored = Setting.query.filter_by(key="logo_file").first()
    if stored:
        db.session.delete(stored)
    if commit:
        db.session.commit()


def branding() -> dict[str, Any]:
    """Values the base template needs on every page."""
    path = logo_path()
    return {
        "site_name": settings.get("site_name") or "DAGonWeb",
        "tagline": settings.get("tagline") or "",
        "footer_text": settings.get("footer_text") or "",
        "default_theme": settings.get("default_theme") or "auto",
        "brand_color": settings.get("brand_color") or "#2457d6",
        "navbar_color": settings.get("navbar_color") or "#0f1b33",
        "logo_url": url_for("branding_logo", v=int(path.stat().st_mtime)) if path else url_for("static", filename="icons/icon.svg"),
    }
