"""Privacy notice, cookie policy, accessibility statement and notice acceptance."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import audit, privacy, settings
from .auth import sso
from .auth.routes import safe_next
from .extensions import db

bp = Blueprint("legal", __name__)


def _context() -> dict:
    return {
        "controller": privacy.controller_name(),
        "controller_address": settings.get("controller_address"),
        "contact": privacy.privacy_contact(),
        "dpo": settings.get("dpo_email"),
        "authority": settings.get("supervisory_authority"),
        "version": privacy.current_policy_version(),
        "extra": settings.get("privacy_policy_extra"),
        "sso_providers": [provider.label for provider in sso.providers()],
        "llm_enabled": bool(settings.get("llm_enabled")),
        "llm_base_url": settings.get("llm_base_url"),
        "smtp_enabled": bool(settings.get("smtp_host")),
        "redis_enabled": settings.get("execution_backend") == "redis",
        "run_retention": int(settings.get("run_retention_days") or 0),
        "audit_retention": int(settings.get("audit_retention_days") or 0),
        "playground": bool(settings.get("guest_playground_enabled")),
        "storage_items": privacy.STORAGE_ITEMS,
        "categories": privacy.COOKIE_CATEGORIES,
        "third_party": privacy.THIRD_PARTY_ASSETS,
    }


@bp.get("/privacy")
def privacy_notice():
    return render_template("legal/privacy.html", **_context())


@bp.get("/cookies")
def cookies():
    return render_template("legal/cookies.html", **_context())


@bp.get("/accessibility")
def accessibility():
    contact = settings.get("accessibility_contact_email") or privacy.privacy_contact()
    return render_template(
        "legal/accessibility.html",
        contact=contact,
        enforcement=settings.get("accessibility_enforcement"),
        reviewed=settings.get("accessibility_statement_date"),
        controller=privacy.controller_name(),
    )


@bp.route("/privacy/accept", methods=["GET", "POST"])
@login_required
def accept():
    target = safe_next(request.values.get("next")) or url_for("workflows.list_workflows")
    if not privacy.needs_acceptance(current_user):
        return redirect(target)
    if request.method == "POST":
        if request.form.get("accept") != "1":
            flash("Please confirm that you have read the privacy notice, or delete your account from the account page.", "warning")
        else:
            privacy.accept_policy(current_user)
            audit.record("privacy.accepted", current_user.email, f"version {privacy.current_policy_version()}")
            db.session.commit()
            return redirect(target)
    return render_template("legal/accept.html", next=target, **_context())
