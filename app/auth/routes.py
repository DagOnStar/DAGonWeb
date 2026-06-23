from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from .forms import LoginForm, RegisterForm
from ..extensions import db
from ..models import Role, User

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
        if User.query.filter_by(email=form.email.data.lower()).first():
            flash("This email is already registered.", "warning")
        else:
            role = Role.query.filter_by(name="user").first()
            user = User(email=form.email.data.lower(), name=form.name.data, active=True, roles=[role] if role else [])
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()
            flash("Account created. Please sign in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form)

@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
