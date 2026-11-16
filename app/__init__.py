from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click
from flask import Flask, abort, redirect, send_file, send_from_directory, url_for
from flask_login import current_user
from .config import Config
from .dagon_ini import DEFAULT_DAGON_INI
from .extensions import db, migrate, login_manager, csrf
from .models import DEFAULT_USER_PERMISSIONS, PERMISSIONS, Role, Setting, User


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
    from .agents.routes import bp as assistant_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(workflows_bp, url_prefix="/workflows")
    app.register_blueprint(assistant_bp, url_prefix="/assistant")

    @app.errorhandler(403)
    def forbidden(_error):
        from flask import render_template

        return render_template("errors/403.html"), 403

    @app.route("/branding/logo")
    def branding_logo():
        from .branding import LOGO_TYPES, logo_path

        path = logo_path()
        if not path:
            abort(404)
        response = send_file(path, mimetype=LOGO_TYPES[path.suffix.lower()], max_age=3600)
        # An uploaded SVG is only ever an image: no scripts, no navigation.
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

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
    def inject_versions() -> dict:
        from .branding import branding

        try:
            values = branding()
        except Exception:  # database not migrated yet
            db.session.rollback()
            values = {"site_name": "DAGonWeb", "tagline": "", "footer_text": "", "default_theme": "auto", "brand_color": "#2457d6", "navbar_color": "#0f1b33", "logo_url": url_for("static", filename="icons/icon.svg")}
        return {"dagonstar_version": dagonstar_version, "branding": values}

    @app.cli.command("worker")
    @click.option("--burst", is_flag=True, help="Exit when the queue is empty.")
    @click.option("--simple", is_flag=True, help="Run jobs in the worker process instead of forking (needed on macOS).")
    def worker(burst: bool, simple: bool) -> None:
        """Process queued workflow runs from Redis (execution backend ``redis``)."""
        from rq import SimpleWorker, Worker

        from .executor.dispatch import redis_connection, redis_queue

        connection = redis_connection()
        queue = redis_queue(connection)
        worker_class = SimpleWorker if simple else Worker
        print(f"DAGonWeb worker listening on '{queue.name}' at {connection.connection_pool.connection_kwargs.get('host', 'redis')}")
        worker_class([queue], connection=connection).work(burst=burst)

    @app.cli.command("cleanup-runs")
    @click.option("--days", type=int, default=None, help="Override the configured retention period.")
    def cleanup_runs_command(days: int | None) -> None:
        """Delete finished runs older than the retention period."""
        from . import settings
        from .maintenance import cleanup_runs

        retention = days if days is not None else int(settings.get("run_retention_days") or 0)
        if retention <= 0:
            print("Run retention is disabled (0 days).")
            return
        print(f"Removed {cleanup_runs(retention)} run(s) older than {retention} days.")

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
    user_role = Role.query.filter_by(name="user").first()
    if user_role is None:
        user_role = Role(name="user", description="Standard users")
        user_role.permissions = DEFAULT_USER_PERMISSIONS
    admin.permissions = list(PERMISSIONS)
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
    db.session.commit()
