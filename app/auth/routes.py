import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from .forms import CompleteRegistrationForm, LoginForm, RegisterForm
from ..extensions import db
from ..models import RegistrationToken, Role, Setting, User

bp = Blueprint("auth", __name__)

@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("workflows.list_workflows"))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower()).first()
        if user and user.check_password(form.password.data) and user.active:
            login_user(user, remember=form.remember.data)
            return redirect(request.args.get("next") or url_for("workflows.list_workflows"))
        flash("Invalid credentials or disabled account.", "danger")
    return render_template("auth/login.html", form=form)

@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("workflows.list_workflows"))
    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.lower()
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
        role = Role.query.filter_by(name="user").first()
        user = User(email=record.email, name=record.email, active=True, roles=[role] if role else [])
        user.set_password(form.password.data)
        record.used_at = datetime.now(timezone.utc)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("Account verified.", "success")
        return redirect(url_for("workflows.list_workflows"))
    return render_template("auth/register_complete.html", form=form, email=record.email)

@bp.route("/logout")
@login_required
def logout():
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


def setting_value(key: str, default: str = "") -> str:
    setting = Setting.query.filter_by(key=key).first()
    return setting.value if setting else default


def send_registration_email(email: str, link: str) -> None:
    if current_app.config.get("TESTING"):
        current_app.extensions["dagonweb_last_registration"] = {"email": email, "link": link, "token": link.rsplit("token=", 1)[-1]}
        return
    host = os.getenv("DAGONWEB_SMTP_HOST", setting_value("smtp_host", "")).strip()
    port = int(os.getenv("DAGONWEB_SMTP_PORT", setting_value("smtp_port", "587")))
    sender = os.getenv("DAGONWEB_SMTP_FROM", setting_value("smtp_from", "noreply@localhost"))
    username = os.getenv("DAGONWEB_SMTP_USER", setting_value("smtp_user", ""))
    password = os.getenv("DAGONWEB_SMTP_PASSWORD", setting_value("smtp_password", ""))
    use_tls = os.getenv("DAGONWEB_SMTP_TLS", setting_value("smtp_tls", "true")).lower() not in {"0", "false", "no"}
    if not host:
        current_app.logger.warning("Registration link for %s: %s", email, link)
        return
    message = EmailMessage()
    message["From"] = sender
    message["To"] = email
    message["Subject"] = "Complete your DAGonWeb registration"
    message.set_content(f"Open this link to finish your DAGonWeb registration:\n\n{link}\n\nThe link expires in 24 hours.\n")
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        if use_tls:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)
