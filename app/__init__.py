from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click
from flask import Flask, redirect, send_from_directory, url_for
from flask_login import current_user
from .config import Config
from .dagon_ini import DEFAULT_DAGON_INI
from .extensions import db, migrate, login_manager, csrf
from .models import Role, Setting, User


def create_app(config_object: type[Config] = Config) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["SCRATCH_DIR"], exist_ok=True)
    dagon_ini_path = os.path.abspath(app.config["DAGON_INI_PATH"])
    os.makedirs(os.path.dirname(dagon_ini_path), exist_ok=True)
    if not os.path.exists(dagon_ini_path):
        with open(dagon_ini_path, "w", encoding="utf-8") as dagon_ini_file:
            dagon_ini_file.write(DEFAULT_DAGON_INI)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    login_manager.login_view = "auth.login"

    from .auth.routes import bp as auth_bp
    from .admin.routes import bp as admin_bp
    from .workflows.routes import bp as workflows_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(workflows_bp, url_prefix="/workflows")

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        return db.session.get(User, int(user_id))

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("workflows.list_workflows"))
        return redirect(url_for("auth.login"))

    @app.route("/service-worker.js")
    def service_worker():
        # Served from the root so the worker's scope covers the whole app.
        response = send_from_directory(os.path.join(app.static_folder, "js"), "service-worker.js", mimetype="application/javascript", max_age=0)
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
        return response

    try:
        dagonstar_version = version("dagonstar")
    except PackageNotFoundError:
        dagonstar_version = "unknown"

    @app.context_processor
    def inject_versions() -> dict[str, str]:
        return {"dagonstar_version": dagonstar_version}

    @app.cli.command("seed")
    def seed() -> None:
        seed_defaults(app)
        print("Seeded DAGonWeb defaults")

    @app.cli.command("export-examples")
    @click.argument("directory", default="examples")
    def export_examples(directory: str) -> None:
        """Write one DAGonStar JSON document per built-in template."""
        from .workflows.routes import template_document, workflow_template_options

        owner = User.query.join(User.roles).filter(Role.name == "admin").first()
        if owner is None:
            raise click.ClickException("Run `flask --app wsgi seed` first.")
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        for key, _label in workflow_template_options():
            path = target / f"{key}.json"
            path.write_text(json.dumps(template_document(key, owner.id), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"Wrote {path}")

    return app


def seed_defaults(app: Flask) -> None:
    admin = Role.query.filter_by(name="admin").first() or Role(name="admin", description="Administrators")
    user_role = Role.query.filter_by(name="user").first() or Role(name="user", description="Standard users")
    db.session.add_all([admin, user_role])
    db.session.flush()
    email = os.getenv("ADMIN_EMAIL", "admin@example.org")
    password = os.getenv("ADMIN_PASSWORD", "admin12345")
    name = os.getenv("ADMIN_NAME", "DAGonWeb Admin")
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email, name=name, active=True, roles=[admin, user_role])
        user.set_password(password)
        db.session.add(user)
    if not Setting.query.filter_by(key="scratch_dir").first():
        db.session.add(Setting(key="scratch_dir", value=app.config["SCRATCH_DIR"]))
    defaults = {
        "smtp_host": "",
        "smtp_port": "587",
        "smtp_from": "noreply@localhost",
        "smtp_user": "",
        "smtp_password": "",
        "smtp_tls": "true",
    }
    for key, value in defaults.items():
        if not Setting.query.filter_by(key=key).first():
            db.session.add(Setting(key=key, value=value))
    db.session.commit()
