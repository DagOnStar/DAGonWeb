from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from .forms import CompleteRegistrationForm, LoginForm, RegisterForm
from . import sso
from .. import audit, settings
from ..extensions import db
from ..mail import send_mail
from ..models import RegistrationToken, Role, User

bp = Blueprint("auth", __name__)


def safe_next(target: str | None) -> str:
    """Only follow same-site relative redirects after sign-in."""
    default = url_for("workflows.list_workflows")
    if not target:
        return default
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return default
    return target


def finish_login(user: User, remember: bool = False, method: str = "local") -> None:
    login_user(user, remember=remember)
    user.last_login_at = datetime.now(timezone.utc)
    audit.record("auth.login", user.email, method, user_id=user.id)
    db.session.commit()


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("workflows.list_workflows"))
    form = LoginForm()
    local_enabled = settings.get("local_login_enabled")
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower()).first()
        if user and user.check_password(form.password.data) and user.active and (local_enabled or user.is_admin):
            finish_login(user, remember=form.remember.data)
            return redirect(safe_next(request.args.get("next")))
        audit.commit("auth.login_failed", form.email.data.lower()[:255])
        flash("Invalid credentials or disabled account.", "danger")
    return render_template("auth/login.html", form=form, providers=sso.providers(), local_enabled=local_enabled, registration_enabled=settings.get("registration_enabled"))


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("workflows.list_workflows"))
    if not settings.get("registration_enabled"):
        flash("Self-registration is disabled. Ask an administrator for an account.", "warning")
        return redirect(url_for("auth.login"))
    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.lower()
        if not settings.domain_allowed(email):
            form.email.errors.append("Registration is limited to the institution's email domains.")
            return render_template("auth/register.html", form=form)
        if not User.query.filter_by(email=email).first():
            RegistrationToken.query.filter_by(email=email, used_at=None).update({"used_at": datetime.now(timezone.utc)})
            record, token = RegistrationToken.issue(email, datetime.now(timezone.utc) + timedelta(hours=24))
            db.session.add(record)
            db.session.commit()
            link = url_for("auth.complete_registration", token=token, _external=True)
            send_registration_email(email, link)
        flash("If that email can register, a verification link has been sent.", "info")
        return redirect(url_for("auth.register_sent", email=email))
    return render_template("auth/register.html", form=form)


@bp.get("/register/sent")
def register_sent():
    return render_template("auth/register_sent.html", email=request.args.get("email", ""))


def default_role() -> list[Role]:
    role = Role.query.filter_by(name="user").first()
    return [role] if role else []


@bp.route("/register/complete", methods=["GET", "POST"])
def complete_registration():
    if current_user.is_authenticated:
        return redirect(url_for("workflows.list_workflows"))
    token = request.args.get("token", "") or request.form.get("token", "")
    record = load_registration_token(token)
    if not record:
        flash("Registration link is invalid or expired.", "danger")
        return redirect(url_for("auth.register"))
    form = CompleteRegistrationForm(token=token)
    if form.validate_on_submit():
        if User.query.filter_by(email=record.email).first():
            record.used_at = datetime.now(timezone.utc)
            db.session.commit()
            flash("Account already exists. Please sign in.", "warning")
            return redirect(url_for("auth.login"))
        user = User(email=record.email, name=record.email, active=True, roles=default_role())
        user.set_password(form.password.data)
        record.used_at = datetime.now(timezone.utc)
        db.session.add(user)
        db.session.flush()
        audit.record("auth.register", user.email, "local", user_id=user.id)
        db.session.commit()
        finish_login(user)
        flash("Account verified.", "success")
        return redirect(url_for("workflows.list_workflows"))
    return render_template("auth/register_complete.html", form=form, email=record.email)


@bp.get("/login/sso/<provider_key>")
def sso_start(provider_key: str):
    item = sso.provider(provider_key)
    if not item:
        abort(404)
    try:
        return redirect(sso.authorization_url(item, url_for("auth.sso_callback", provider_key=item.key, _external=True)))
    except Exception as exc:  # network or configuration problems are user-facing
        current_app.logger.warning("SSO discovery failed for %s: %s", provider_key, exc)
        flash(f"{item.label} sign-in is not available right now.", "danger")
        return redirect(url_for("auth.login"))


@bp.get("/login/sso/<provider_key>/callback")
def sso_callback(provider_key: str):
    item = sso.provider(provider_key)
    if not item:
        abort(404)
    if request.args.get("error"):
        flash(f"{item.label} sign-in was cancelled or refused.", "warning")
        return redirect(url_for("auth.login"))
    try:
        identity = sso.complete(item, request.args.get("code", ""), request.args.get("state", ""))
        user = resolve_sso_user(item.key, identity)
    except ValueError as exc:
        audit.commit("auth.sso_failed", provider_key, str(exc))
        flash(str(exc), "danger")
        return redirect(url_for("auth.login"))
    except Exception as exc:
        current_app.logger.warning("SSO sign-in failed for %s: %s", provider_key, exc)
        flash(f"{item.label} sign-in failed.", "danger")
        return redirect(url_for("auth.login"))
    finish_login(user, method=item.key)
    return redirect(url_for("workflows.list_workflows"))


def resolve_sso_user(provider_key: str, identity: dict) -> User:
    """Find, link or create the account for verified SSO identity claims."""
    subject, email = identity["subject"], identity["email"]
    if not subject:
        raise ValueError("The identity provider returned no subject.")
    user = User.query.filter_by(auth_provider=provider_key, external_id=subject).first()
    if user is None:
        existing = User.query.filter_by(email=email).first()
        if existing is not None:
            if not identity.get("email_verified") or existing.external_id:
                raise ValueError("An account with this email already exists. Sign in with your password, or ask an administrator to link it.")
            existing.auth_provider, existing.external_id = provider_key, subject
            audit.record("auth.sso_linked", existing.email, provider_key, user_id=existing.id)
            user = existing
        else:
            if not settings.get("sso_auto_create"):
                raise ValueError("No account exists for this identity. Ask an administrator to create one.")
            if not settings.domain_allowed(email):
                raise ValueError("Sign-in is limited to the institution's email domains.")
            user = User(email=email, name=identity.get("name") or email, active=True, roles=default_role(), auth_provider=provider_key, external_id=subject)
            user.set_unusable_password()
            db.session.add(user)
            db.session.flush()
            audit.record("auth.register", email, provider_key, user_id=user.id)
    if not user.active:
        raise ValueError("This account is disabled.")
    return user


@bp.route("/logout")
@login_required
def logout():
    audit.record("auth.logout", current_user.email)
    db.session.commit()
    logout_user()
    return redirect(url_for("auth.login"))


def load_registration_token(token: str) -> RegistrationToken | None:
    if not token:
        return None
    return RegistrationToken.query.filter(
        RegistrationToken.token_hash == RegistrationToken.hash_token(token),
        RegistrationToken.used_at.is_(None),
        RegistrationToken.expires_at > datetime.now(timezone.utc),
    ).first()


def send_registration_email(email: str, link: str) -> None:
    if current_app.config.get("TESTING"):
        current_app.extensions["dagonweb_last_registration"] = {"email": email, "link": link, "token": link.rsplit("token=", 1)[-1]}
        return
    site = settings.get("site_name") or "DAGonWeb"
    if not send_mail(email, f"Complete your {site} registration", f"Open this link to finish your {site} registration:\n\n{link}\n\nThe link expires in 24 hours.\n"):
        current_app.logger.warning("Registration link for %s: %s", email, link)
